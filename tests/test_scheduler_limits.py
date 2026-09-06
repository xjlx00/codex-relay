import asyncio
from contextlib import AsyncExitStack

import pytest

from cr.config import Settings
from cr.scheduler import QueueFull,Scheduler


async def test_four_default_users_run_four_requests():
    scheduler=Scheduler(); tasks=[]
    async def queued(uid):
        async with scheduler.slot(uid,1):
            pytest.fail('A second request must wait at the default user limit')
    async with AsyncExitStack() as stack:
        for uid in ('a','b','c','d'):
            await stack.enter_async_context(scheduler.slot(uid,1))
            tasks.append(asyncio.create_task(queued(uid)))
        await asyncio.sleep(0)
        assert Settings().concurrent==6
        assert scheduler.stats()=={'running':4,'limit':6,'waiting':4}
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    assert scheduler.stats()=={'running':0,'limit':6,'waiting':0}


async def test_six_global_slots_and_individual_limits():
    scheduler=Scheduler(); scheduler.set_limit('a',3); scheduler.set_limit('b',2)
    entered=asyncio.Event(); release=asyncio.Event()
    async def waiting_user():
        async with scheduler.slot('d',1):
            entered.set(); await release.wait()
    async with AsyncExitStack() as stack:
        for uid in ('a','a','a','b','b','c'):
            await stack.enter_async_context(scheduler.slot(uid,1))
        task=asyncio.create_task(waiting_user()); await asyncio.sleep(0)
        assert scheduler.stats()=={'running':6,'limit':6,'waiting':1}
        assert scheduler.stats('a')=={'running':3,'limit':3,'waiting':0}
        assert scheduler.stats('b')['running']==2
        assert not entered.is_set()
    await asyncio.wait_for(entered.wait(),1)
    release.set(); await task
    assert not scheduler.running and not scheduler.waiting


async def test_increase_immediately_releases_queued_request():
    scheduler=Scheduler(); entered=asyncio.Event(); release=asyncio.Event()
    async def second():
        async with scheduler.slot('a',1):
            entered.set(); await release.wait()
    async with scheduler.slot('a',1):
        task=asyncio.create_task(second()); await asyncio.sleep(0)
        assert scheduler.stats('a')['waiting']==1
        scheduler.set_limit('a',2)
        assert scheduler.stats('a')=={'running':2,'limit':2,'waiting':0}
        await asyncio.wait_for(entered.wait(),1)
        release.set(); await task
    assert not scheduler.running


async def test_decrease_preserves_running_requests_and_blocks_new_grants():
    scheduler=Scheduler(); scheduler.set_limit('a',3)
    slots=[scheduler.slot('a',1) for _ in range(3)]
    for slot in slots:await slot.__aenter__()
    scheduler.set_limit('a',1)
    assert scheduler.stats('a')['running']==3
    entered=asyncio.Event()
    async def fourth():
        async with scheduler.slot('a',1):entered.set()
    task=asyncio.create_task(fourth()); await asyncio.sleep(0)
    for slot in slots[:2]:
        await slot.__aexit__(None,None,None); await asyncio.sleep(0)
        assert not entered.is_set()
    assert scheduler.stats('a')=={'running':1,'limit':1,'waiting':1}
    await slots[2].__aexit__(None,None,None)
    await asyncio.wait_for(task,1)
    assert entered.is_set() and not scheduler.running


async def test_full_queue_does_not_block_eligible_user_from_empty_slot():
    scheduler=Scheduler(2,1)
    async def second_a():
        async with scheduler.slot('a',1):pass
    async with scheduler.slot('a',1):
        task=asyncio.create_task(second_a()); await asyncio.sleep(0)
        assert scheduler.stats()['waiting']==1
        async with scheduler.slot('b',1):
            assert scheduler.stats()['running']==2
            with pytest.raises(QueueFull):
                async with scheduler.slot('c',1):pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    assert not scheduler.running and not scheduler.waiting


async def test_waiting_users_take_turns():
    scheduler=Scheduler(1); order=[]
    async def job(uid):
        async with scheduler.slot(uid,1):order.append(uid)
    async with scheduler.slot('holder',1):
        tasks=[asyncio.create_task(job(uid)) for uid in ('a','a','b','b','c')]
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)
    assert order==['a','b','c','a','b']


async def test_cancellation_after_grant_returns_exactly_one_slot():
    scheduler=Scheduler(2); scheduler.set_limit('a',2)
    async def pending():
        async with scheduler.slot('a',1):
            pytest.fail('Cancelled request should not enter its body')
    async with scheduler.slot('a',1):
        async with scheduler.slot('b',1):
            task=asyncio.create_task(pending()); await asyncio.sleep(0)
        assert scheduler.stats('a')['running']==2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert scheduler.stats('a')['running']==1
    assert not scheduler.running and not scheduler.waiting


async def test_error_releases_slot_and_timeout_releases_waiter():
    scheduler=Scheduler()
    with pytest.raises(RuntimeError):
        async with scheduler.slot('a',1):raise RuntimeError('worker failed')
    async with scheduler.slot('a',1):
        with pytest.raises(asyncio.TimeoutError):
            async with scheduler.slot('a',0.01):pass
        assert scheduler.stats('a')=={'running':1,'limit':1,'waiting':0}
    assert not scheduler.running and not scheduler.waiting


@pytest.mark.parametrize('limit',[True,False,1.0,'1',None,0,7])
def test_invalid_user_limits_rejected(limit):
    scheduler=Scheduler()
    with pytest.raises(ValueError):scheduler.set_limit('a',limit)
    assert scheduler.stats('a')['limit']==1
