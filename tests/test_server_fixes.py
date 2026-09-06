import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from cr.config import Settings
from cr.monitor import UpstreamMonitor
from cr.server import create_app
from cr.store import Store
from test_bridge_gateway import FakeRPC


class ControlledRPC(FakeRPC):
    def __init__(self):
        super().__init__()
        self.gate_method=None; self.entered=asyncio.Event(); self.release=asyncio.Event()
        self.generating=asyncio.Event(); self.tool_results=[]

    async def call(self,method,params,**kwargs):
        if method==self.gate_method:
            self.entered.set(); await self.release.wait()
        result=await super().call(method,params,**kwargs)
        if method=='turn/start':self.generating.set()
        return result

    async def produce(self,tid,turn):
        if self.mode!='tool':return await super().produce(tid,turn)
        self.emit('thread/tokenUsage/updated',tid,turnId=turn,
            tokenUsage={'total':{'inputTokens':100,'outputTokens':10,'cachedInputTokens':20}})
        result=await self.handler({'threadId':tid,'turnId':turn,'tool':'relay_tool_0','arguments':{'x':1}})
        self.tool_results.append(result)
        if result['success']:
            self.emit('item/agentMessage/delta',tid,turnId=turn,itemId='one',delta='Hello')
            self.emit('turn/completed',tid,turn={'id':turn,'status':'completed'})


@pytest.fixture
async def gateway(tmp_path,monkeypatch):
    async def quiet_monitor(self):await asyncio.Event().wait()
    monkeypatch.setattr(UpstreamMonitor,'run',quiet_monitor)
    db=Store(tmp_path/'server.sqlite'); keys=db.bootstrap(500000000); rpc=ControlledRPC()
    app=create_app(Settings(work_dir=str(tmp_path),turn_timeout=5),rpc,db)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            yield app,db,rpc,client,keys


def headers(keys,owner='user1'):return {'Authorization':'Bearer '+keys[owner]}


def endpoint(app,path):return next(route.endpoint for route in app.routes if route.path==path)


def body_request(keys,body,disconnected=None):
    sent=False
    async def receive():
        nonlocal sent
        if not sent:sent=True; return {'type':'http.request','body':json.dumps(body).encode(),'more_body':False}
        if disconnected and disconnected.is_set():return {'type':'http.disconnect'}
        await asyncio.Event().wait()
    return Request({'type':'http','method':'POST','path':'/v1/responses',
        'headers':[(b'authorization',('Bearer '+keys['user1']).encode())]},receive)


async def until(predicate):
    async def wait():
        while not predicate():await asyncio.sleep(0)
    await asyncio.wait_for(wait(),2)


async def events(response):
    data=b''.join([chunk async for chunk in response.body_iterator]).decode()
    return [json.loads(line[6:]) for line in data.splitlines() if line.startswith('data: ')]


async def test_admin_concurrency_validation_permissions_and_saved_limits(gateway,tmp_path):
    app,db,rpc,client,keys=gateway
    assert app.state.scheduler.stats()=={'running':0,'limit':6,'waiting':0}
    assert all(user['concurrent_limit']==1 for user in db.users())
    for invalid in (0,7,True,False,1.0,'2',None):
        response=await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'concurrent_limit':invalid})
        assert response.status_code==422,invalid
    assert (await client.patch('/api/admin/users/user1',headers=headers(keys),json={'concurrent_limit':2})).status_code==403
    assert (await client.patch('/api/admin/users/missing',headers=headers(keys,'admin'),json={'concurrent_limit':2})).status_code==404
    before=db.usage('user1')
    response=await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'concurrent_limit':3})
    assert response.status_code==200
    assert response.json()['concurrency']=={'running':0,'limit':3,'waiting':0}
    for key in ('used','held','budget'):assert db.usage('user1')[key]==before[key]
    assert (await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'name':'Renamed'})).status_code==200
    reopened=Store(tmp_path/'server.sqlite')
    restarted=create_app(Settings(work_dir=str(tmp_path)),ControlledRPC(),reopened)
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.scheduler.stats('user1')['limit']==3
        assert reopened.authenticate(keys['user1'])['id']=='user1'


async def test_admin_raise_releases_queue_and_global_six_still_applies(gateway):
    app,db,rpc,client,keys=gateway; rpc.mode='hang'; requests=[]
    for user,limit in (('user1',3),('user2',2)):
        assert (await client.patch('/api/admin/users/'+user,headers=headers(keys,'admin'),json={'concurrent_limit':limit})).status_code==200
    for user in ('user1','user1','user1','user2','user2','user3','user4'):
        requests.append(asyncio.create_task(client.post('/v1/responses',headers=headers(keys,user),json={'input':'hello'})))
    await until(lambda:app.state.scheduler.stats()['waiting']==1 and len(app.state.bridge.current)==6)
    assert app.state.scheduler.stats()=={'running':6,'limit':6,'waiting':1}
    assert app.state.scheduler.stats('user1')['running']==3
    first=next(rid for rid,job in app.state.jobs.items() if job['owner']=='user1')
    assert (await client.post('/v1/responses/'+first+'/cancel',headers=headers(keys))).status_code==200
    await until(lambda:app.state.scheduler.stats('user4')['running']==1)
    assert app.state.scheduler.stats()['running']==6
    for rid,job in list(app.state.jobs.items()):
        await client.post('/v1/responses/'+rid+'/cancel',headers=headers(keys,job['owner']))
    responses=await asyncio.gather(*requests)
    assert all(response.json()['status']=='cancelled' for response in responses)
    assert app.state.scheduler.stats()=={'running':0,'limit':6,'waiting':0}


async def test_admin_increase_and_decrease_apply_to_live_queue(gateway):
    app,db,rpc,client,keys=gateway; rpc.mode='hang'
    tasks=[asyncio.create_task(client.post('/v1/responses',headers=headers(keys),json={'input':'hello'})) for _ in range(3)]
    await until(lambda:app.state.scheduler.stats('user1')['waiting']==2)
    response=await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'concurrent_limit':2})
    assert response.json()['concurrency']=={'running':2,'limit':2,'waiting':1}
    await until(lambda:len(app.state.bridge.current)==2)
    response=await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'concurrent_limit':1})
    assert response.json()['concurrency']=={'running':2,'limit':1,'waiting':1}
    active=next(iter(app.state.bridge.current))
    await client.post('/v1/responses/'+active+'/cancel',headers=headers(keys))
    assert app.state.scheduler.stats('user1')=={'running':1,'limit':1,'waiting':1}
    for rid in list(app.state.jobs):await client.post('/v1/responses/'+rid+'/cancel',headers=headers(keys))
    await asyncio.gather(*tasks)
    assert not app.state.jobs and app.state.scheduler.stats()['running']==0


@pytest.mark.parametrize('stage',['queued','account/read','thread/start','running'])
@pytest.mark.parametrize('action',['cancel','disable'])
async def test_cancel_and_disable_cover_all_active_stages(gateway,stage,action):
    app,db,rpc,client,keys=gateway; rpc.mode='hang'; holder=None
    if stage=='queued':
        holder=app.state.scheduler.slot('user1',1); await holder.__aenter__()
    elif stage!='running':rpc.gate_method=stage
    response=await endpoint(app,'/v1/responses')(body_request(keys,{'input':'hello','stream':True}))
    rid=response.headers['x-request-id']
    if stage=='queued':await until(lambda:app.state.scheduler.stats('user1')['waiting']==1)
    elif stage=='running':await asyncio.wait_for(rpc.generating.wait(),1)
    else:await asyncio.wait_for(rpc.entered.wait(),1)
    if action=='cancel':result=await client.post('/v1/responses/'+rid+'/cancel',headers=headers(keys))
    else:result=await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'active':False})
    assert result.status_code==200
    rpc.release.set()
    output=await events(response)
    assert output[-1]['type']=='response.failed' and output[-1]['response']['status']=='cancelled'
    assert output[-1]['response']['error']['code']==('cancelled' if action=='cancel' else 'user_disabled')
    assert [item['sequence_number'] for item in output]==list(range(len(output)))
    assert not app.state.jobs and not app.state.bridge.current and not app.state.bridge.sessions
    assert app.state.scheduler.stats('user1')['waiting']==0
    if holder:await holder.__aexit__(None,None,None)
    assert app.state.scheduler.stats()['running']==0
    turns=[params for method,params in rpc.calls if method=='turn/start']
    assert len(turns)==(1 if stage=='running' else 0)
    row=db.recent('user1')[0]
    if stage=='running':assert row['status']=='unknown'
    else:assert row['status']=='cancelled' and row['reserved']==0 and db.usage('user1')['held']==0


async def test_cancel_before_worker_first_step_releases_reserve_and_emits_terminal(gateway):
    app,db,rpc,client,keys=gateway
    response=await endpoint(app,'/v1/responses')(body_request(keys,{'input':'hello','stream':True}))
    rid=response.headers['x-request-id']
    # Directly invoke cancellation before yielding control to the new work task.
    result=await endpoint(app,'/v1/responses/{response_id}/cancel')(rid,body_request(keys,{}))
    assert result['status']=='cancelled' and not rpc.calls
    assert not app.state.jobs and db.usage('user1')['held']==0
    output=await events(response)
    assert len(output)==1 and output[0]['sequence_number']==0
    assert output[0]['response']['status']=='cancelled'


@pytest.mark.parametrize('action',['cancel','disable'])
async def test_running_nonstream_returns_cancelled_json(gateway,action):
    app,db,rpc,client,keys=gateway; rpc.mode='hang'
    task=asyncio.create_task(client.post('/v1/responses',headers=headers(keys),json={'input':'hello'}))
    await asyncio.wait_for(rpc.generating.wait(),1); rid=next(iter(app.state.jobs))
    if action=='cancel':await client.post('/v1/responses/'+rid+'/cancel',headers=headers(keys))
    else:await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'active':False})
    response=await asyncio.wait_for(task,1)
    assert response.status_code==200 and response.json()['status']=='cancelled'
    assert response.json()['error']['code']==('cancelled' if action=='cancel' else 'user_disabled')
    assert not app.state.jobs and app.state.scheduler.stats()['running']==0


@pytest.mark.parametrize('action',['cancel','disable'])
async def test_pending_tool_cancel_is_owner_scoped_and_never_resumes(gateway,action):
    app,db,rpc,client,keys=gateway; rpc.mode='tool'
    response=await client.post('/v1/responses',headers=headers(keys),json={
        'input':'use tool','tools':[{'type':'function','name':'client_tool'}]})
    data=response.json(); rid=data['id']; cid=data['output'][0]['call_id']
    assert not app.state.jobs and app.state.scheduler.stats()['running']==0
    assert app.state.bridge.sessions
    assert (await client.post('/v1/responses/'+rid+'/cancel',headers=headers(keys,'user2'))).status_code==404
    assert app.state.bridge.sessions
    if action=='cancel':result=await client.post('/v1/responses/'+rid+'/cancel',headers=headers(keys))
    else:result=await client.patch('/api/admin/users/user1',headers=headers(keys,'admin'),json={'active':False})
    assert result.status_code==200
    await until(lambda:bool(rpc.tool_results))
    assert not rpc.tool_results[0]['success']
    assert not app.state.bridge.sessions and not app.state.bridge.calls
    resume=await client.post('/v1/responses',headers=headers(keys),json={
        'input':[{'type':'function_call_output','call_id':cid,'output':'RESULT'}],
        'tools':[{'type':'function','name':'client_tool'}]})
    assert resume.status_code==(409 if action=='cancel' else 401)
    assert len([1 for method,_ in rpc.calls if method=='turn/start'])==1
    assert db.usage('user1')['used']==110 and db.usage('user1')['held']==0


@pytest.mark.parametrize('stream',[False,True])
async def test_client_disconnect_before_dispatch_releases_slot_and_reserve(gateway,stream):
    app,db,rpc,client,keys=gateway; rpc.gate_method='account/read'; disconnected=asyncio.Event()
    request=body_request(keys,{'input':'hello','stream':stream},disconnected)
    if stream:
        response=await endpoint(app,'/v1/responses')(request)
        iterator=response.body_iterator; assert await anext(iterator)==b': connected\n\n'
        await asyncio.wait_for(rpc.entered.wait(),1)
        await iterator.aclose()
    else:
        task=asyncio.create_task(endpoint(app,'/v1/responses')(request))
        await asyncio.wait_for(rpc.entered.wait(),1); disconnected.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,1)
    assert not app.state.jobs and app.state.scheduler.stats()['running']==0
    assert db.usage('user1')['held']==0 and not app.state.bridge.sessions
    assert not any(method=='turn/start' for method,_ in rpc.calls)


async def test_cancel_under_stream_backpressure_has_contiguous_terminal_sequence(gateway,monkeypatch):
    app,db,rpc,client,keys=gateway; blocked=asyncio.Event()
    async def flood(owner,request,rid,push,**kwargs):
        for index in range(300):
            if index==256:blocked.set()
            await push({'type':'response.output_text.delta','delta':'x','sequence_number':index})
    monkeypatch.setattr(app.state.bridge,'segment',flood)
    response=await endpoint(app,'/v1/responses')(body_request(keys,{'input':'hello','stream':True}))
    await asyncio.wait_for(blocked.wait(),1)
    await client.post('/v1/responses/'+response.headers['x-request-id']+'/cancel',headers=headers(keys))
    output=await events(response)
    assert len(output)==257
    assert [item['sequence_number'] for item in output]==list(range(257))
    assert output[-1]['type']=='response.failed' and output[-1]['response']['status']=='cancelled'
    assert db.usage('user1')['held']==0


async def test_successful_stream_has_one_completed_terminal_and_correct_accounting(gateway):
    app,db,rpc,client,keys=gateway
    response=await client.post('/v1/responses',headers=headers(keys),json={'input':'hello','stream':True})
    output=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert [item['sequence_number'] for item in output]==list(range(len(output)))
    assert [item['type'] for item in output if item['type'] in ('response.completed','response.failed')]==['response.completed']
    assert output[-1]['response']['status']=='completed'
    assert db.recent('user1')[0]['status']=='completed'
    assert db.usage('user1')['used']==110 and db.usage('user1')['held']==0


@pytest.mark.parametrize('stage',['queue','account/read','thread/start'])
async def test_queue_and_upstream_timeouts_have_distinct_http_errors(gateway,monkeypatch,stage):
    app,db,rpc,client,keys=gateway; holder=None
    if stage=='queue':
        app.state.bridge.settings.queue_timeout=.01
        holder=app.state.scheduler.slot('user1',1); await holder.__aenter__()
    else:
        original=rpc.call
        async def timeout(method,params,**kwargs):
            if method==stage:raise asyncio.TimeoutError()
            return await original(method,params,**kwargs)
        monkeypatch.setattr(rpc,'call',timeout)
    try:
        response=await client.post('/v1/responses',headers=headers(keys),json={'input':'hello'})
        assert response.status_code==(429 if stage=='queue' else 504)
        assert response.json()['error']['code']==('queue_timeout' if stage=='queue' else 'upstream_timeout')
        assert db.usage('user1')['held']==0 and not app.state.jobs
        assert not any(method=='turn/start' for method,_ in rpc.calls)
    finally:
        if holder:await holder.__aexit__(None,None,None)
    assert app.state.scheduler.stats()['running']==0
