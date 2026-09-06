import asyncio
from datetime import datetime,timezone,timedelta
import pytest
from cr.store import Store,BudgetError
from cr.scheduler import Scheduler,QueueFull

@pytest.fixture
def ledger(tmp_path):
    db=Store(tmp_path/'usage.sqlite3'); keys=db.bootstrap(1000)
    yield db,keys
    db.close()

def test_keys_separation_and_rotation(ledger):
    db,keys=ledger
    assert len(set(keys.values()))==5
    assert db.authenticate(keys['admin'])['role']=='admin'
    assert db.authenticate('wrong') is None
    new=db.rotate('user1')
    assert db.authenticate(keys['user1']) is None
    assert db.authenticate(new)['id']=='user1'
    db.update_user('user1',{'active':False})
    assert db.authenticate(new) is None

def test_reservations_prevent_parallel_budget_oversubscription(ledger):
    db,_=ledger
    db.reserve('r1','user1','model',700)
    with pytest.raises(BudgetError):db.reserve('r2','user1','model',400)
    assert db.usage('user1')['held']==700
    assert db.usage('user2')['remaining']==1000
    db.finish('r1','cancelled',dispatched=False)
    assert db.usage('user1')['remaining']==1000

def test_cached_tokens_not_double_counted_and_finish_idempotent(ledger):
    db,_=ledger; db.reserve('r','user1','model',500); db.running('r')
    usage={'input_tokens':200,'output_tokens':30,'input_tokens_details':{'cached_tokens':170}}
    assert db.checkpoint('r',usage)
    assert db.usage('user1')['used']==230
    db.finish('r','completed',usage)
    db.finish('r','completed',{'input_tokens':900})
    u=db.usage('user1'); assert (u['used'],u['held'],u['cached_tokens'])==(230,0,170)

def test_restart_preserves_unknown_usage_and_reconciliation(ledger):
    db,_=ledger
    db.reserve('queued','user1','model',100); db.reserve('running','user1','model',400)
    db.running('running'); db.checkpoint('running',{'input_tokens':200,'output_tokens':10})
    db.recover(); u=db.usage('user1')
    assert (u['used'],u['held'],u['unresolved'])==(210,190,1)
    assert db.reconcile('running',250)
    assert not db.reconcile('running',0)
    assert db.usage('user1')['used']==250 and db.usage('user1')['held']==0

def test_budget_survives_monday_later_weeks_and_restart(tmp_path,monkeypatch):
    tz=timezone(timedelta(hours=8))
    start=int(datetime(2026,9,7,tzinfo=tz).timestamp())
    monkeypatch.setattr('cr.store.time.time',lambda:start-1)
    path=tmp_path/'rollover.sqlite'; db=Store(path);db.bootstrap(1000)
    db.reserve('done','user1','m',100);db.running('done')
    db.finish('done','completed',{'input_tokens':700,'output_tokens':100})
    db.reserve('pending','user1','m',150)
    # Old weekly rows and new rows both keep counting after the boundary.
    with db.db:db.db.execute('UPDATE requests SET period=?',(start-604800,))
    for now in (start,start+8*604800):
        monkeypatch.setattr('cr.store.time.time',lambda:now)
        db.close();db=Store(path)
        u=db.usage('user1')
        assert (u['used'],u['held'],u['remaining'])==(800,150,50)
        assert u['resets_at'] is None and u['reset_policy']=='manual'
        with pytest.raises(BudgetError):db.reserve('blocked','user1','m',51)
    db.reset_all_usage()
    assert db.usage('user1')['used']==0 and db.usage('user1')['held']==150
    assert db.statistics(0,start+1)['users'][0]['total_tokens']==800
    db.finish('pending','cancelled',dispatched=False)
    db.reserve('new','user1','m',100);db.running('new')
    db.finish('new','completed',{'input_tokens':90,'output_tokens':10})
    db.close();db=Store(path)
    assert db.usage('user1')['used']==100
    db.close()

def test_delayed_official_usage_releases_prior_hold(ledger):
    db,_=ledger
    db.reserve('first','user1','model',200); db.running('first'); db.finish('first','completed')
    assert db.usage('user1')['held']==200
    db.reserve('next','user1','model',200); db.running('next')
    usage={'input_tokens':220,'output_tokens':20}
    db.checkpoint('next',usage,['first']); db.finish('next','completed',usage)
    u=db.usage('user1')
    assert (u['used'],u['held'],u['unresolved'])==(240,0,0)

async def test_cancelled_waiter_does_not_consume_capacity():
    s=Scheduler(1,4); acquired=asyncio.Event(); release=asyncio.Event()
    async def hold():
        async with s.slot('one',1):acquired.set(); await release.wait()
    holder=asyncio.create_task(hold()); await acquired.wait()
    async def wait():
        async with s.slot('two',2):pass
    waiter=asyncio.create_task(wait()); await asyncio.sleep(0); waiter.cancel()
    with pytest.raises(asyncio.CancelledError):await waiter
    release.set(); await holder
    assert not s.active and not s.waiting
    async with s.slot('three',1):assert s.active=={'three'}

async def test_fair_queue_and_per_user_serialization():
    s=Scheduler(2,16); active=set(); order=[]; gate=asyncio.Event(); started=asyncio.Event()
    async def job(user,index):
        async with s.slot(user,2):
            assert user not in active; active.add(user); assert len(active)<=2
            order.append((user,index))
            if len(order)==2:started.set()
            await gate.wait(); await asyncio.sleep(.002); active.remove(user)
    tasks=[asyncio.create_task(job(u,i)) for i,u in enumerate(['a','a','a','b','b','b','c','d'])]
    await started.wait(); gate.set(); await asyncio.gather(*tasks)
    assert {u for u,_ in order[2:4]}=={'c','a'} or {u for u,_ in order[2:4]}=={'a','b'} or 'c' in [u for u,_ in order[2:5]]
    assert [u for u,_ in order].index('d')<6

async def test_queue_is_bounded_and_timeout_releases_waiter():
    s=Scheduler(1,1)
    async with s.slot('a',1):
        async def queued():
            async with s.slot('b',.03):pass
        task=asyncio.create_task(queued()); await asyncio.sleep(.005)
        with pytest.raises(QueueFull):
            async with s.slot('c',1):pass
        with pytest.raises(asyncio.TimeoutError):await task
    assert not s.active and not s.waiting
