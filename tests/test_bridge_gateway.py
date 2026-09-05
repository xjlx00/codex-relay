import asyncio,copy,json
import httpx,pytest
from cr.rpc import Tap
from cr.config import Settings
from cr.bridge import Bridge,BridgeError
from cr.server import create_app
from cr.store import Store
from cr.translate import normalize,InputError

class FakeRPC:
    def __init__(self):
        self.generation=1; self._taps=set(); self.n=0; self.calls=[]; self.handler=None
        self.tasks=set(); self.logged_in=True; self.mode='text'
    def alive(self):return True
    async def start(self):pass
    async def stop(self):
        for t in self.tasks:t.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
    def subscribe(self,tid):
        tap=Tap(self,tid); self._taps.add(tap); return tap
    def emit(self,method,tid,**params):
        for tap in self._taps:tap.push({'method':method,'params':{'threadId':tid,**params}})
    async def produce(self,tid,turn):
        await asyncio.sleep(0)
        if self.mode=='hang':return
        if self.mode=='reasoning':
            self.emit('rawResponseItem/completed',tid,turnId=turn,item={'id':'rs1','type':'reasoning','summary':[{'type':'summary_text','text':'Public summary'}],'encrypted_content':'opaque','content':'private must not pass through'})
        self.emit('thread/tokenUsage/updated',tid,turnId=turn,tokenUsage={'total':{'inputTokens':100,'outputTokens':10,'cachedInputTokens':20}})
        if self.mode=='tool':
            result=await self.handler({'threadId':tid,'turnId':turn,'tool':'relay_tool_0','arguments':{'x':1}})
            assert result=={'contentItems':[{'type':'inputText','text':'RESULT'}],'success':True}
            self.emit('thread/tokenUsage/updated',tid,turnId=turn,tokenUsage={'total':{'inputTokens':200,'outputTokens':20,'cachedInputTokens':40}})
        self.emit('item/agentMessage/delta',tid,turnId=turn,itemId='item1',delta='Hello')
        self.emit('turn/completed',tid,turn={'id':turn,'status':'completed'})
    async def call(self,method,params,**kwargs):
        self.calls.append((method,copy.deepcopy(params)))
        if method=='account/read':return {'account':{'type':'chatgpt'} if self.logged_in else None}
        if method=='model/list':return {'data':[{'id':'model1','model':'model1'}],'nextCursor':None}
        if method=='thread/start':self.n+=1; return {'thread':{'id':f'thread{self.n}'}}
        if method=='turn/start':
            tid=params['threadId']; turn='turn'+tid
            task=asyncio.create_task(self.produce(tid,turn)); self.tasks.add(task)
            return {'turn':{'id':turn}}
        return {}

@pytest.fixture
def settings(tmp_path):return Settings(work_dir=str(tmp_path),turn_timeout=2)

async def test_tool_continuation_ownership_and_incremental_accounting(settings):
    rpc=FakeRPC(); rpc.mode='tool'; b=Bridge(rpc,settings)
    req=normalize({'input':'use tool','tools':[{'type':'function','name':'client_tool'}]},'model1')
    try:
        r1=await b.segment('user1',req,'r1')
        assert r1['usage']['total_tokens']==110
        cid=r1['output'][0]['call_id']; assert r1['output'][0]['name']=='client_tool'
        continuation=normalize({'input':[{'type':'function_call_output','call_id':cid,'output':'RESULT'}],
            'tools':[{'type':'function','name':'client_tool'}]},'model1')
        with pytest.raises(BridgeError):await b.segment('user2',continuation,'stolen')
        assert cid in b.calls
        r2=await b.segment('user1',continuation,'r2')
        assert r2['usage']['total_tokens']==110 and r2['output'][0]['content'][0]['text']=='Hello'
        assert [x['type'] for x in b.responses['r2']['history']]==['message','function_call','function_call_output','message']
        assert len([c for c in rpc.calls if c[0]=='turn/start'])==1
        assert not b.sessions and not b.calls
    finally:await b.close(); await rpc.stop()

async def test_previous_response_preserves_roles_and_history(settings):
    rpc=FakeRPC(); b=Bridge(rpc,settings)
    await b.segment('u',normalize({'input':'first'},'m'),'first')
    await b.segment('u',normalize({'input':'second','previous_response_id':'first'},'m'),'second')
    injected=[p for m,p in rpc.calls if m=='thread/inject_items'][-1]['items']
    assert [x['role'] for x in injected]==['user','assistant']
    assert injected[0]['content'][0]['text']=='first'
    with pytest.raises(BridgeError):await b.segment('other',normalize({'input':'second','previous_response_id':'first'},'m'),'bad')
    await b.close(); await rpc.stop()

async def test_interruption_wakes_waiter(settings):
    rpc=FakeRPC(); rpc.mode='hang'; b=Bridge(rpc,settings)
    task=asyncio.create_task(b.segment('u',normalize({'input':'hello'},'m'),'r'))
    while 'r' not in b.current:await asyncio.sleep(0)
    await b.interrupt('u','r')
    with pytest.raises(BridgeError):await asyncio.wait_for(task,.5)
    assert not b.sessions
    await rpc.stop()

async def test_reasoning_preserves_only_public_summary_and_encrypted_history(settings):
    rpc=FakeRPC(); rpc.mode='reasoning'; b=Bridge(rpc,settings)
    result=await b.segment('u',normalize({'input':'hello'},'m'),'r')
    reasoning=result['output'][0]
    assert reasoning['encrypted_content']=='opaque' and 'content' not in reasoning
    assert reasoning['summary'][0]['text']=='Public summary'
    assert result['output'][1]['content'][0]['text']=='Hello'
    await rpc.stop()

@pytest.mark.parametrize('body',[
    {'input':'x','tools':[{'type':'web_search'}]},
    {'input':[{'role':'user','content':[{'type':'input_image','image_url':'file:///etc/passwd'}]}]},
    {'input':'x','max_output_tokens':100},
    {'input':'x','tools':[{'type':'function','name':'a'},{'type':'function','name':'a'}]},
])
def test_unsafe_or_unsupported_inputs_are_rejected(body):
    with pytest.raises(InputError):normalize(body,'m')

async def test_http_auth_budget_streaming_and_no_usage_leak(tmp_path,settings):
    db=Store(tmp_path/'http.sqlite3'); keys=db.bootstrap(500000000); rpc=FakeRPC()
    app=create_app(settings,rpc,db)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            h=lambda name:{'Authorization':'Bearer '+keys[name]}
            assert (await client.get('/api/me')).status_code==401
            assert (await client.get('/api/admin/users',headers=h('user1'))).status_code==403
            assert (await client.post('/v1/responses',headers=h('admin'),json={'input':'x'})).status_code==403
            response=await client.post('/v1/responses',headers=h('user1'),json={'input':'x','stream':True})
            assert response.status_code==200 and 'response.completed' in response.text
            assert 'sequence_number' in response.text
            assert db.usage('user1')['used']==110 and db.usage('user1')['held']==0
            assert (await client.get('/api/me',headers=h('user2'))).json()['requests']==[]
            assert 'key_hash' not in (await client.get('/api/admin/users',headers=h('admin'))).text
            response=await client.post('/v1/responses',headers=h('user1'),json={'input':'x','previous_response_id':'missing'})
            assert response.status_code==404 and db.usage('user1')['held']==0
            rpc.logged_in=False
            response=await client.post('/v1/responses',headers=h('user2'),json={'input':'x'})
            assert response.status_code==503 and db.usage('user2')['held']==0
            db.update_user('user3',{'budget':1})
            assert (await client.post('/v1/responses',headers=h('user3'),json={'input':'x'})).status_code==429
