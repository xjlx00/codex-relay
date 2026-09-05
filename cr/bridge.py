"""Ownership-aware Responses bridge. Pending dynamic tools resume the same turn."""
import asyncio
import contextlib
import copy
import json
import time
from dataclasses import dataclass,field
from .translate import uid,InputError,usage_delta
from .rpc import NotReady

class BridgeError(Exception):
    def __init__(self,code,status=502):self.code=code; self.status=status; super().__init__(code)

@dataclass
class Session:
    owner:str
    thread:str
    generation:int
    request:dict
    tap:object
    turn:str=''
    touched:float=field(default_factory=time.monotonic)
    pending:dict=field(default_factory=dict)
    total:dict|None=None
    accounted:dict=field(default_factory=dict)
    usage:dict|None=None
    response:dict|None=None
    ended:bool=False
    unaccounted:list=field(default_factory=list)

class Bridge:
    def __init__(self,rpc,settings):
        self.rpc=rpc; self.settings=settings; self.sessions={}; self.calls={}; self.responses={}
        self.current={}; self.rpc.handler=self.tool_call

    async def tool_call(self,params):
        session=self.sessions.get(params.get('threadId'))
        denied={'contentItems':[{'type':'inputText','text':'Tool unavailable'}],'success':False}
        if not session or session.ended:return denied
        if session.turn and params.get('turnId')!=session.turn:return denied
        info=session.request['names'].get(params.get('tool'))
        if not info:return denied
        call_id=uid('call'); fut=asyncio.get_running_loop().create_future()
        args=params.get('arguments',{})
        if isinstance(args,str):
            try:args=json.loads(args)
            except ValueError:return denied
        if info['custom']:
            item={'id':uid('ctc'),'type':'custom_tool_call','call_id':call_id,'name':info['name'],'input':args.get('input','')}
        else:item={'id':uid('fc'),'type':'function_call','call_id':call_id,'name':info['name'],'arguments':json.dumps(args,ensure_ascii=False),'status':'completed'}
        if info['namespace']:item['namespace']=info['namespace']
        session.pending[call_id]={'future':fut,'item':item,'emitted':False}
        self.calls[call_id]=session
        session.tap.push({'method':'relay/tool','params':{'threadId':session.thread,'turnId':params.get('turnId'),'call_id':call_id}})
        try:return await asyncio.wait_for(fut,self.settings.session_ttl)
        except asyncio.TimeoutError:
            await self.drop(session)
            return denied
        except asyncio.CancelledError:return denied

    async def drop(self,session):
        if session.thread not in self.sessions:return
        self.sessions.pop(session.thread,None)
        session.tap.fail()
        for cid,p in session.pending.items():
            self.calls.pop(cid,None)
            if not p['future'].done():p['future'].cancel()
        if session.turn and not session.ended and self.rpc.alive() and session.generation==self.rpc.generation:
            with contextlib.suppress(Exception):
                await self.rpc.call('turn/interrupt',{'threadId':session.thread,'turnId':session.turn},timeout=8)
        session.tap.close()
        if self.rpc.alive() and session.generation==self.rpc.generation:
            with contextlib.suppress(Exception):await self.rpc.call('thread/unsubscribe',{'threadId':session.thread},timeout=5)

    async def cleanup(self):
        now=time.monotonic()
        for session in list(self.sessions.values()):
            if session not in self.current.values() and (session.generation!=self.rpc.generation or now-session.touched>self.settings.session_ttl):
                await self.drop(session)
        for rid,value in list(self.responses.items()):
            if now-value['time']>self.settings.session_ttl:self.responses.pop(rid,None)
        while len(self.responses)>self.settings.max_sessions:self.responses.pop(next(iter(self.responses)))

    async def prepare(self,owner,request):
        await self.cleanup()
        outputs=[x for x in request['items'] if x.get('type') in ('function_call_output','custom_tool_call_output')]
        live=[self.calls[x['call_id']] for x in outputs if x['call_id'] in self.calls]
        if any(s.owner!=owner for s in live):raise BridgeError('continuation_not_found',404)
        if live:
            session=live[-1]
            if any(s is not session for s in live):raise InputError('Tool results belong to different conversations')
            if session.generation!=self.rpc.generation:raise BridgeError('continuation_lost',409)
            if request['model']!=session.request['model'] or request['dynamic']!=session.request['dynamic']:
                raise InputError('Model and tools must remain unchanged while tools are pending')
            if request['instructions']!=session.request['instructions']:raise InputError('Instructions changed during pending tool execution')
            pending_outputs=[x for x in outputs if x['call_id'] in session.pending]
            if len({x['call_id'] for x in pending_outputs})!=len(pending_outputs):raise InputError('Duplicate tool output')
            # Continuations must provide all tool calls already delivered to the client.
            expected={cid for cid,p in session.pending.items() if p['emitted'] and not p['future'].done()}
            if {x['call_id'] for x in pending_outputs}!=expected:raise InputError('Provide exactly the outstanding tool results')
            return session,pending_outputs
        if any(x['call_id'].startswith('call_') for x in outputs) and not any(x.get('type') in ('function_call','custom_tool_call') for x in request['items']):
            raise BridgeError('continuation_lost',409)
        if request['previous']:
            prev=self.responses.get(request['previous'])
            if not prev or prev['owner']!=owner:raise BridgeError('previous_response_not_found',404)
            request=copy.deepcopy(request); request['items']=prev['history']+request['items']
        if len(self.sessions)>=self.settings.max_sessions:raise BridgeError('session_capacity',429)
        await self.rpc.start()
        res=await self.rpc.call('thread/start',{'model':request['model'],'cwd':self.settings.work_dir,
          'ephemeral':True,'environments':[],'approvalPolicy':'never','sandbox':'read-only',
          'developerInstructions':request['instructions'],'dynamicTools':request['dynamic'],
          'experimentalRawEvents':True})
        tid=res['thread']['id']; tap=self.rpc.subscribe(tid)
        session=Session(owner,tid,self.rpc.generation,request,tap); self.sessions[tid]=session
        return session,[]

    async def interrupt(self,owner,rid):
        session=self.current.get(rid)
        if not session or session.owner!=owner:raise BridgeError('response_not_found',404)
        await self.drop(session)

    async def segment(self,owner,request,rid,on_delta=None,on_dispatch=None,on_usage=None):
        session,outputs=await self.prepare(owner,request)
        incoming_metadata=request['metadata']
        if outputs:session.request['items'].extend(copy.deepcopy(outputs))
        request=session.request
        self.current[rid]=session; session.touched=time.monotonic(); session.usage=None
        baseline=dict(session.accounted); text_items={}; ordered=[]; failure=None; paused=False
        seq=0
        response={'id':rid,'object':'response','created_at':int(time.time()),'status':'in_progress',
          'model':request['model'],'output':[],'usage':None,'error':None,'metadata':incoming_metadata}
        session.response=response
        async def emit(typ,**fields):
            nonlocal seq
            event={'type':typ,'sequence_number':seq,**fields}; seq+=1
            if on_delta:await on_delta(event)
        try:
            await emit('response.created',response=copy.deepcopy(response))
            await emit('response.in_progress',response=copy.deepcopy(response))
            if outputs:
                if on_dispatch:on_dispatch()
                for item in outputs:
                    p=session.pending[item['call_id']]
                    p['future'].set_result({'contentItems':[{'type':'inputText','text':item['output']}],'success':True})
                    self.calls.pop(item['call_id'],None)
                    del session.pending[item['call_id']]
            else:
                items=copy.deepcopy(request['items']); user_input=[]
                if items and items[-1].get('type')=='message' and items[-1].get('role')=='user':
                    last=items.pop()
                    for c in last['content']:
                        if c['type']=='input_image':user_input.append({'type':'image','url':c['image_url']})
                        else:user_input.append({'type':'text','text':c['text']})
                if items:await self.rpc.call('thread/inject_items',{'threadId':session.thread,'items':items})
                params={'threadId':session.thread,'input':user_input,'model':request['model'],
                   'sandboxPolicy':{'type':'readOnly'},
                   'approvalPolicy':'never'}
                if request['effort']:params['effort']=request['effort']
                if request['summary']:params['summary']=request['summary']
                if request['schema']:params['outputSchema']=request['schema']
                if request['service_tier']:params['serviceTierForTurn']=request['service_tier']
                if on_dispatch:on_dispatch()
                res=await self.rpc.call('turn/start',params,timeout=120); session.turn=res['turn']['id']
            deadline=time.monotonic()+self.settings.turn_timeout
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise BridgeError('turn_timeout',504)
                unreported=any(not p['emitted'] for p in session.pending.values())
                try:msg=await session.tap.get(min(.08 if unreported else 15,remaining))
                except asyncio.TimeoutError:
                    if unreported:paused=True; break
                    if not self.rpc.alive():raise BridgeError('app_server_unavailable')
                    continue
                method=msg['method']; p=msg.get('params',{})
                if method=='relay/disconnected':raise BridgeError('app_server_unavailable')
                if p.get('turnId') and p['turnId']!=session.turn:continue
                if method in ('thread/tokenUsage/updated','turn/tokenUsage/updated'):
                    total=p.get('tokenUsage',{}).get('total')
                    if isinstance(total,dict):
                        session.total=total; session.usage=usage_delta(total,baseline)
                        if on_usage and not on_usage(session.usage,session.unaccounted):raise BridgeError('budget_exhausted',429)
                        session.unaccounted=[]
                elif method=='item/agentMessage/delta':
                    iid=p.get('itemId') or 'message'; delta=p.get('delta','')
                    if iid not in text_items:
                        item={'id':uid('msg'),'type':'message','role':'assistant','status':'in_progress',
                              'content':[{'type':'output_text','text':'','annotations':[]}]}
                        text_items[iid]=item; ordered.append(item)
                        await emit('response.output_item.added',output_index=len(ordered)-1,item=copy.deepcopy(item))
                        await emit('response.content_part.added',item_id=item['id'],output_index=len(ordered)-1,content_index=0,part=copy.deepcopy(item['content'][0]))
                    item=text_items[iid]; item['content'][0]['text']+=delta
                    await emit('response.output_text.delta',item_id=item['id'],output_index=ordered.index(item),content_index=0,delta=delta)
                elif method=='rawResponseItem/completed' and p.get('item',{}).get('type')=='reasoning':
                    raw=p['item']
                    # Forward public summaries and opaque encrypted history, never raw reasoning text.
                    item={k:copy.deepcopy(raw[k]) for k in ('id','type','summary','encrypted_content') if k in raw}
                    item.setdefault('id',uid('rs')); item.setdefault('summary',[])
                    index=len(ordered); ordered.append(item)
                    await emit('response.output_item.added',output_index=index,item=copy.deepcopy(item))
                    await emit('response.output_item.done',output_index=index,item=copy.deepcopy(item))
                elif method=='item/completed':
                    item=p.get('item',{})
                    if item.get('type')=='agentMessage' and item.get('id') in text_items:
                        target=text_items[item['id']]
                        if item.get('phase'):target['phase']=item['phase']
                elif method=='turn/completed':
                    turn=p.get('turn',{})
                    if turn.get('id')!=session.turn:continue
                    session.ended=True
                    if turn.get('status')!='completed':failure='upstream_'+str(turn.get('status','failed'))
                    break
                elif method=='error':
                    # Wait for the terminal turn event; retryable errors can recover upstream.
                    if not p.get('willRetry',False):failure='upstream_error'
                elif method in ('item/started',):
                    typ=p.get('item',{}).get('type')
                    if typ in ('commandExecution','fileChange','mcpToolCall','collabToolCall','imageView'):
                        raise BridgeError('unexpected_server_tool')
            for item in ordered:
                if item['type']!='message':continue
                index=ordered.index(item); item['status']='completed'
                await emit('response.output_text.done',item_id=item['id'],output_index=index,content_index=0,text=item['content'][0]['text'])
                await emit('response.content_part.done',item_id=item['id'],output_index=index,content_index=0,part=item['content'][0])
                await emit('response.output_item.done',output_index=index,item=copy.deepcopy(item))
            if paused:
                for cid,p in session.pending.items():
                    if p['emitted']:continue
                    p['emitted']=True; item=p['item']; index=len(ordered); ordered.append(item)
                    initial=copy.deepcopy(item)
                    field='input' if item['type']=='custom_tool_call' else 'arguments'
                    initial[field]=''
                    await emit('response.output_item.added',output_index=index,item=initial)
                    typ='custom_tool_call_input' if field=='input' else 'function_call_arguments'
                    await emit('response.'+typ+'.delta',item_id=item['id'],output_index=index,delta=item[field])
                    await emit('response.'+typ+'.done',item_id=item['id'],output_index=index,**{field:item[field]})
                    await emit('response.output_item.done',output_index=index,item=copy.deepcopy(item))
            response.update(status='failed' if failure else 'completed',output=ordered,usage=session.usage,
                error={'code':failure,'message':failure} if failure else None)
            if paused and session.usage is None:session.unaccounted.append(rid)
            if session.total is not None:session.accounted=dict(session.total)
            session.request['items'].extend(copy.deepcopy(ordered))
            self.responses[rid]={'owner':owner,'time':time.monotonic(),'history':copy.deepcopy(session.request['items'])}
            await emit('response.failed' if failure else 'response.completed',response=copy.deepcopy(response))
            return response
        except BaseException:
            await self.drop(session); raise
        finally:
            self.current.pop(rid,None); session.touched=time.monotonic()
            if not paused:await self.drop(session)

    async def close(self):
        for s in list(self.sessions.values()):await self.drop(s)
