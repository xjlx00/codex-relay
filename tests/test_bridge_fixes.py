import asyncio
import copy
import json

import pytest

from cr.bridge import Bridge,BridgeError
from cr.rpc import RpcError
from cr.translate import InputError,normalize
from test_bridge_gateway import FakeRPC,settings,history_store


TOOLS=[{'type':'function','name':'client_tool'}]


class ProtocolRPC(FakeRPC):
    def __init__(self):
        super().__init__()
        self.deltas=[]; self.final='Authoritative final'; self.tool_results=[]

    async def produce(self,tid,turn):
        if self.mode=='tool':
            result=await self.handler({'threadId':tid,'turnId':turn,'tool':'relay_tool_0','arguments':{'x':1}})
            self.tool_results.append(result)
        for delta in self.deltas:
            self.emit('item/agentMessage/delta',tid,turnId=turn,itemId='text',delta=delta)
        self.emit('item/completed',tid,turnId=turn,item={
            'id':'text','type':'agentMessage','text':self.final,'phase':'final_answer'})
        self.emit('turn/completed',tid,turn={'id':turn,'status':'completed'})


async def pending(bridge,rid='pending'):
    return await bridge.segment('user1',normalize({'input':'Use the tool','tools':TOOLS},'m'),rid)


def continuation(result,message=None):
    items=[{'type':'function_call_output','call_id':result['output'][0]['call_id'],'output':'RESULT'}]
    if message:items.append({'role':'user','content':message})
    return normalize({'input':items,'tools':TOOLS},'m')


def compact_request(db,result):
    return normalize({'input':db.load_history('user1',result['id'])['history']+
        continuation(result)['items']+[{'role':'user','content':'Create a checkpoint.'}],'tools':[]},'m')


@pytest.mark.parametrize('deltas',[[],['Authoritative '],['Authoritative ','final']])
async def test_final_text_matches_stream_json_and_history(deltas,settings,history_store):
    rpc=ProtocolRPC(); rpc.deltas=deltas; bridge=Bridge(rpc,settings,history_store); events=[]
    async def emit(event):events.append(event)
    try:
        result=await bridge.segment('user1',normalize({'input':'hello'},'m'),'answer',on_delta=emit)
        message=result['output'][0]
        assert message['content'][0]['text']==rpc.final and message['phase']=='final_answer'
        assert ''.join(e['delta'] for e in events if e['type']=='response.output_text.delta')==rpc.final
        assert next(e['text'] for e in events if e['type']=='response.output_text.done')==rpc.final
        assert history_store.load_history('user1','answer')['history'][-1]==message
        assert events[-1]['response']==result
        assert sum(e['type']=='response.output_item.added' for e in events)==1
    finally:await bridge.close(); await rpc.stop()


async def test_reasoning_summary_streams_before_completion_without_private_content(settings,history_store):
    rpc=ProtocolRPC(); bridge=Bridge(rpc,settings,history_store); events=[]
    first_delta=asyncio.Event(); finish=asyncio.Event()
    async def produce(tid,turn):
        rpc.emit('item/reasoning/textDelta',tid,turnId=turn,itemId='rs',contentIndex=0,delta='PRIVATE SECRET')
        rpc.emit('item/reasoning/summaryTextDelta',tid,turnId=turn,itemId='rs',summaryIndex=0,delta='Public ')
        await finish.wait()
        rpc.emit('item/reasoning/summaryPartAdded',tid,turnId=turn,itemId='rs',summaryIndex=1)
        rpc.emit('item/reasoning/summaryTextDelta',tid,turnId=turn,itemId='rs',summaryIndex=1,delta='Next')
        rpc.emit('item/completed',tid,turnId=turn,item={
            'id':'rs','type':'reasoning','summary':['Public summary','Next'],'content':['PRIVATE SECRET']})
        rpc.emit('rawResponseItem/completed',tid,turnId=turn,item={
            'id':'rs','type':'reasoning','summary':[{'type':'summary_text','text':'Public summary'},
            {'type':'summary_text','text':'Next'}],'encrypted_content':'opaque','content':'PRIVATE SECRET'})
        rpc.emit('turn/completed',tid,turn={'id':turn,'status':'completed'})
    rpc.produce=produce
    async def emit(event):
        events.append(event)
        if event['type']=='response.reasoning_summary_text.delta':first_delta.set()
    task=asyncio.create_task(bridge.segment('user1',normalize({'input':'think','reasoning':{'summary':'auto'}},'m'),'r',on_delta=emit))
    try:
        await asyncio.wait_for(first_delta.wait(),1)
        assert not task.done()
        finish.set(); result=await task
        assert len(result['output'])==1 and result['output'][0]['encrypted_content']=='opaque'
        deltas=[e for e in events if e['type']=='response.reasoning_summary_text.delta']
        assert ''.join(e['delta'] for e in deltas if e['summary_index']==0)=='Public summary'
        assert sum(e['type']=='response.output_item.done' for e in events)==1
        assert 'PRIVATE SECRET' not in json.dumps(events)
        assert 'PRIVATE SECRET' not in json.dumps(history_store.load_history('user1','r'))
    finally:
        task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await bridge.close(); await rpc.stop()


async def test_compaction_uses_independent_thread_and_does_not_execute_old_tool(settings,history_store):
    rpc=ProtocolRPC(); rpc.mode='tool'; bridge=Bridge(rpc,settings,history_store)
    try:
        result=await pending(bridge); request=compact_request(history_store,result)
        old=bridge.calls[result['output'][0]['call_id']]
        rpc.mode='text'
        answer=await bridge.segment('user1',request,'summary')
        assert answer['output'][0]['content'][0]['text']==rpc.final
        assert old.cancel_reason=='context_replaced' and not bridge.calls
        assert all(not r['success'] for r in rpc.tool_results)
        starts=[p for m,p in rpc.calls if m=='thread/start']
        assert len(starts)==2 and starts[-1]['dynamicTools']==[]
        injected=[p['items'] for m,p in rpc.calls if m=='thread/inject_items'][-1]
        assert [i['type'] for i in injected]==['message','function_call','function_call_output']
        rpc.mode='tool'
        next_turn=await bridge.segment('user1',normalize({'input':'Continue with a tool','tools':TOOLS,
            'previous_response_id':'summary'},'m'),'continued')
        assert next_turn['output'][0]['type']=='function_call'
    finally:await bridge.close(); await rpc.stop()


@pytest.mark.parametrize('invalid',['foreign','changed_history','missing_result'])
async def test_compaction_cannot_bypass_ownership_or_pending_history(invalid,settings,history_store):
    rpc=ProtocolRPC(); rpc.mode='tool'; bridge=Bridge(rpc,settings,history_store)
    try:
        result=await pending(bridge); request=compact_request(history_store,result)
        session=bridge.calls[result['output'][0]['call_id']]
        if invalid=='changed_history':request['items'][0]['content'][0]['text']='forged history'
        if invalid=='missing_result':request['items'].pop(-2)
        with pytest.raises((InputError,BridgeError)):
            await bridge.segment('user2' if invalid=='foreign' else 'user1',request,'bad')
        assert session.thread in bridge.sessions and not session.busy
        assert not session.pending[result['output'][0]['call_id']]['future'].done()
        assert sum(m=='turn/start' for m,p in rpc.calls)==1
    finally:await bridge.close(); await rpc.stop()


async def test_failed_compaction_keeps_explicit_history_replayable_and_unknown_usage(settings,history_store):
    rpc=ProtocolRPC(); rpc.mode='tool'; bridge=Bridge(rpc,settings,history_store); db=history_store
    try:
        db.reserve('pending','user1','m',1000); db.running('pending')
        result=await pending(bridge); db.finish('pending',result['status'],result['usage'])
        request=compact_request(db,result); rpc.mode='text'; original_call=rpc.call
        async def fail(method,params,**kw):
            if method=='turn/start':raise RpcError(-32000,'unavailable')
            return await original_call(method,params,**kw)
        rpc.call=fail
        with pytest.raises(RpcError):await bridge.segment('user1',request,'failed')
        assert not bridge.sessions and not bridge.calls
        assert next(r for r in db.recent('user1') if r['id']=='pending')['status']=='unknown'
        assert db.load_history('user1','pending')['pending']
        rpc.call=original_call
        result=await bridge.segment('user1',request,'retry')
        assert result['status']=='completed' and all(not r['success'] for r in rpc.tool_results)
    finally:await bridge.close(); await rpc.stop()


async def test_continuation_is_claimed_before_injection_await(settings,history_store):
    rpc=ProtocolRPC(); rpc.mode='tool'; bridge=Bridge(rpc,settings,history_store)
    entered=asyncio.Event(); release=asyncio.Event(); task=None
    try:
        result=await pending(bridge); request=continuation(result,'New input'); call=rpc.call
        async def block(method,params,**kw):
            if method=='thread/inject_items':entered.set(); await release.wait()
            return await call(method,params,**kw)
        rpc.call=block
        task=asyncio.create_task(bridge.segment('user1',request,'first'))
        await asyncio.wait_for(entered.wait(),1)
        with pytest.raises(BridgeError) as error:await bridge.segment('user1',request,'second')
        assert (error.value.code,error.value.status)==('continuation_busy',409)
        release.set(); assert (await task)['status']=='completed'
        assert len(rpc.tool_results)==1 and rpc.tool_results[0]['success']
    finally:
        if task:task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await bridge.close(); await rpc.stop()


async def test_creating_session_counts_towards_sixteen_limit(settings,history_store):
    rpc=ProtocolRPC(); rpc.mode='tool'; bridge=Bridge(rpc,settings,history_store)
    entered=asyncio.Event(); release=asyncio.Event(); task=None
    try:
        for i in range(15):await pending(bridge,f'pending{i}')
        call=rpc.call
        async def block(method,params,**kw):
            if method=='thread/start':entered.set(); await release.wait()
            return await call(method,params,**kw)
        rpc.call=block
        task=asyncio.create_task(pending(bridge,'sixteen'))
        await asyncio.wait_for(entered.wait(),1)
        with pytest.raises(BridgeError) as error:await pending(bridge,'seventeen')
        assert error.value.code=='session_capacity'
        release.set(); await task
        assert len(bridge.sessions)==16 and not bridge.creating
    finally:
        if task:task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await bridge.close(); await rpc.stop()


@pytest.mark.parametrize('cancel',[False,True])
async def test_failed_or_cancelled_creation_releases_capacity(cancel,settings,history_store):
    settings.max_live_sessions_per_user=1
    rpc=ProtocolRPC(); bridge=Bridge(rpc,settings,history_store); entered=asyncio.Event()
    call=rpc.call
    async def fail(method,params,**kw):
        if method=='thread/start':
            entered.set()
            if cancel:await asyncio.Event().wait()
            raise RpcError(-32000,'thread creation failed')
        return await call(method,params,**kw)
    rpc.call=fail
    task=asyncio.create_task(bridge.segment('user1',normalize({'input':'hello'},'m'),'failed'))
    try:
        await asyncio.wait_for(entered.wait(),1)
        if cancel:task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RpcError):await task
        assert not bridge.creating and not bridge.sessions
        rpc.call=call
        assert (await bridge.segment('user1',normalize({'input':'hello'},'m'),'retry'))['status']=='completed'
    finally:
        task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await bridge.close(); await rpc.stop()


async def test_cancel_pending_tool_and_cancel_during_injection(settings,history_store):
    rpc=ProtocolRPC(); rpc.mode='tool'; bridge=Bridge(rpc,settings,history_store)
    entered=asyncio.Event(); release=asyncio.Event(); task=None
    try:
        result=await pending(bridge); cid=result['output'][0]['call_id']
        session=bridge.calls[cid]
        with pytest.raises(BridgeError) as error:await bridge.interrupt('user2','pending')
        assert error.value.status==404
        await bridge.interrupt('user1','pending')
        assert session.cancel_reason=='cancelled' and cid not in bridge.calls
        result=await pending(bridge,'pending2'); call=rpc.call
        async def block(method,params,**kw):
            if method=='thread/inject_items':entered.set(); await release.wait()
            return await call(method,params,**kw)
        rpc.call=block
        task=asyncio.create_task(bridge.segment('user1',continuation(result,'new'),'continued'))
        await asyncio.wait_for(entered.wait(),1)
        await bridge.interrupt('user1','pending2',reason='user_disabled')
        release.set()
        with pytest.raises(BridgeError) as error:await task
        assert error.value.code=='user_disabled' and not bridge.calls
        assert all(not r['success'] for r in rpc.tool_results)
    finally:
        if task:task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await bridge.close(); await rpc.stop()


@pytest.mark.parametrize('info,terminal,code',[
    ('usageLimitExceeded',False,'upstream_rate_limited'),
    ('rateLimitExceeded',False,'upstream_rate_limited'),
    ({'httpConnectionFailed':{'httpStatusCode':429}},False,'upstream_rate_limited'),
    ({'responseTooManyFailedAttempts':{'httpStatusCode':429}},True,'upstream_rate_limited'),
    ('serverOverloaded',True,'upstream_overloaded'),
])
async def test_upstream_limits_keep_stable_code_without_sensitive_messages(info,terminal,code,settings,history_store):
    rpc=ProtocolRPC(); bridge=Bridge(rpc,settings,history_store); events=[]
    async def produce(tid,turn):
        error={'message':'PRIVATE UPSTREAM ACCOUNT MESSAGE','additionalDetails':'PRIVATE DETAILS','codexErrorInfo':info}
        if not terminal:rpc.emit('error',tid,turnId=turn,willRetry=False,error=error)
        rpc.emit('turn/completed',tid,turn={'id':turn,'items':[],'status':'failed','error':error if terminal else None})
    rpc.produce=produce
    async def emit(event):events.append(event)
    try:
        result=await bridge.segment('user1',normalize({'input':'hello'},'m'),'limited',on_delta=emit)
        assert result['status']=='failed' and result['error']=={'code':code,'message':code}
        assert events[-1]['response']==result
        assert 'PRIVATE' not in json.dumps(events) and 'retryAfter' not in json.dumps(events)
        assert sum(m=='turn/start' for m,p in rpc.calls)==1
    finally:await bridge.close(); await rpc.stop()


async def test_retryable_upstream_limit_can_recover_without_relay_retry(settings,history_store):
    rpc=ProtocolRPC(); bridge=Bridge(rpc,settings,history_store)
    async def produce(tid,turn):
        rpc.emit('error',tid,turnId=turn,willRetry=True,error={'message':'private','codexErrorInfo':'rateLimitExceeded'})
        rpc.emit('item/completed',tid,turnId=turn,item={'id':'text','type':'agentMessage','text':'Recovered'})
        rpc.emit('turn/completed',tid,turn={'id':turn,'items':[],'status':'completed','error':None})
    rpc.produce=produce
    try:
        result=await bridge.segment('user1',normalize({'input':'hello'},'m'),'recovered')
        assert result['status']=='completed' and result['error'] is None
        assert result['output'][0]['content'][0]['text']=='Recovered'
        assert sum(m=='turn/start' for m,p in rpc.calls)==1
    finally:await bridge.close(); await rpc.stop()
