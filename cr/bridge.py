"""Ownership-aware Responses bridge. Pending dynamic tools resume the same turn."""
import asyncio
import contextlib
import copy
import json
import time
from dataclasses import dataclass,field
from .translate import uid,InputError,usage_delta,normalize,tool_content
from .store import HistoryError
from .rpc import NotReady,RpcError

class BridgeError(Exception):
    def __init__(self,code,status=502):self.code=code; self.status=status; super().__init__(code)

def comparable_history(items):
    """Ignore response-only decoration when recognizing a client's history replay."""
    items=normalize({'input':items},'history')['items'] if items else []
    for item in items:
        item.pop('id',None); item.pop('status',None)
        if item['type']=='message':
            for part in item['content']:part.pop('annotations',None)
    return items

def history_suffix(history,items):
    if len(items)>=len(history) and comparable_history(items[:len(history)])==comparable_history(history):
        return items[len(history):]
    return items

def upstream_failure(error):
    """Classify documented CodexErrorInfo without exposing upstream messages."""
    info=(error or {}).get('codexErrorInfo')
    if info in ('usageLimitExceeded','rateLimitExceeded'):return 'upstream_rate_limited'
    if info=='serverOverloaded':return 'upstream_overloaded'
    if isinstance(info,dict):
        for kind in ('httpConnectionFailed','responseStreamConnectionFailed','responseStreamDisconnected','responseTooManyFailedAttempts'):
            if (info.get(kind) or {}).get('httpStatusCode')==429:return 'upstream_rate_limited'
    return None

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
    busy:bool=False
    cancel_reason:str|None=None

class Bridge:
    def __init__(self,rpc,settings,store):
        self.rpc=rpc; self.settings=settings; self.store=store; self.sessions={}; self.calls={}
        self.current={}; self.creating={}; self.rpc.handler=self.tool_call

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
        try:return await asyncio.wait_for(fut,self.settings.tool_wait_timeout)
        except asyncio.TimeoutError:
            await self.drop(session,'tool_wait_timeout')
            return denied
        except asyncio.CancelledError:return denied

    async def drop(self,session,reason=None):
        if reason and not session.cancel_reason:session.cancel_reason=reason
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
            if not session.busy and (session.generation!=self.rpc.generation or now-session.touched>self.settings.tool_wait_timeout):
                await self.drop(session)
        self.store.cleanup_history()

    def resolve(self,owner,request):
        """Resolve owned history before admission so the full context is reserved."""
        request=copy.deepcopy(request)
        if request['previous']:
            try:previous=self.store.load_history(owner,request['previous'])
            except HistoryError as e:raise BridgeError(e.code,e.status) from e
            request['items']=previous['history']+history_suffix(previous['history'],request['items'])
            request['_previous_pending']=previous['pending']
        live=[self.calls[x['call_id']] for x in request['items']
              if x.get('type') in ('function_call_output','custom_tool_call_output') and x['call_id'] in self.calls]
        if any(s.owner!=owner for s in live):raise BridgeError('continuation_not_found',404)
        if live:
            session=live[-1]
            if any(s is not session for s in live):raise InputError('Tool results belong to different conversations')
            request['_full_tool_history']=(len(request['items'])>=len(session.request['items']) and
                comparable_history(request['items'][:len(session.request['items'])])==comparable_history(session.request['items']))
            request['items']=copy.deepcopy(session.request['items'])+history_suffix(session.request['items'],request['items'])
            request['_tool_thread']=session.thread
        request['_resolved']=True
        return request

    async def prepare(self,owner,request):
        if not request.get('_resolved'):request=self.resolve(owner,request)
        outputs=[x for x in request['items'] if x.get('type') in ('function_call_output','custom_tool_call_output')]
        live=[self.calls[x['call_id']] for x in outputs if x['call_id'] in self.calls]
        if any(s.owner!=owner for s in live):raise BridgeError('continuation_not_found',404)
        if live:
            session=live[-1]
            if any(s is not session for s in live):raise InputError('Tool results belong to different conversations')
            if session.busy:raise BridgeError('continuation_busy',409)
            # Claim before cleanup/injection can yield, including validation failures.
            session.busy=True
            try:
                await self.cleanup()
                return await self.prepare_continuation(owner,request,session)
            except BaseException:
                session.busy=False
                raise
        previous=self.sessions.get(request.get('_tool_thread'))
        if previous and previous.owner==owner and previous.busy:raise BridgeError('continuation_busy',409)
        if request.get('_previous_pending') or request.get('_tool_thread'):
            raise BridgeError('continuation_lost',409)
        await self.cleanup()
        return await self.new_session(owner,request)

    async def prepare_continuation(self,owner,request,session):
        if session.cancel_reason:raise BridgeError(session.cancel_reason,409)
        if request['previous'] and request['previous']!=session.response['id']:
            raise InputError('Tool results and previous_response_id belong to different responses')
        if session.generation!=self.rpc.generation:raise BridgeError('continuation_lost',409)
        # Local compaction replays a complete owned history with paired results
        # and a new user instruction, but deliberately omits dynamic tools.
        # Retire the waiting turn without executing its tools again. Unknown
        # prior usage remains in the ledger for reconciliation.
        independent=(not request['dynamic'] and request.get('_full_tool_history') and
            request['items'][-1].get('type')=='message' and request['items'][-1].get('role')=='user')
        if independent:
            self.check_tool_history(request['items'],complete=True)
            expected={cid for cid,p in session.pending.items() if p['emitted'] and not p['future'].done()}
            remaining=history_suffix(session.request['items'],request['items'])
            supplied={x['call_id'] for x in remaining if x.get('type') in ('function_call_output','custom_tool_call_output')}
            if supplied!=expected:raise InputError('Provide exactly the outstanding tool results')
            await self.drop(session,'context_replaced')
            session.busy=False
            request=copy.deepcopy(request)
            for key in ('_previous_pending','_tool_thread','_full_tool_history'):request.pop(key,None)
            request['previous']=None
            return await self.new_session(owner,request)
        if any(request[k]!=session.request[k] for k in ('model','dynamic','names')):
            raise InputError('Model and tools must remain unchanged while tools are pending')
        if request['instructions']!=session.request['instructions']:raise InputError('Instructions changed during pending tool execution')
        if any(request.get(k)!=session.request.get(k) for k in ('effort','summary','schema','service_tier','verbosity')):
            raise InputError('Generation settings changed during pending tool execution')
        remaining=history_suffix(session.request['items'],request['items'])
        pending_outputs=[]; new_messages=[]
        for item in remaining:
            if item.get('type') in ('function_call_output','custom_tool_call_output') and item.get('call_id') in session.pending:
                pending_outputs.append(item)
            elif item.get('type')=='message' and item.get('role')=='user':new_messages.append(item)
            else:raise InputError('Tool continuation contains changed history or unexpected items')
        if len({x['call_id'] for x in pending_outputs})!=len(pending_outputs):raise InputError('Duplicate tool output')
        # Continuations must provide all tool calls already delivered to the client.
        expected={cid for cid,p in session.pending.items() if p['emitted'] and not p['future'].done()}
        if {x['call_id'] for x in pending_outputs}!=expected:raise InputError('Provide exactly the outstanding tool results')
        if new_messages:
            # Inject all new user input before releasing any tool future. A known
            # protocol rejection leaves the original pending turn available to retry.
            session.touched=time.monotonic()
            try:await self.rpc.call('thread/inject_items',{'threadId':session.thread,'items':new_messages},timeout=8)
            except RpcError as e:raise InputError('New messages could not be injected; retry tool results without new messages') from e
            except BaseException:
                await self.drop(session); raise
        if session.cancel_reason:raise BridgeError(session.cancel_reason,409)
        return session,remaining

    def check_tool_history(self,items,complete=False):
        calls=set(); completed=set()
        for item in items:
            if item.get('type') in ('function_call','custom_tool_call'):
                if item['call_id'] in calls:raise InputError('Duplicate tool call')
                calls.add(item['call_id'])
            elif item.get('type') in ('function_call_output','custom_tool_call_output'):
                if item['call_id'] not in calls:raise BridgeError('continuation_lost',409)
                if item['call_id'] in completed:raise InputError('Duplicate tool output')
                completed.add(item['call_id'])
        if complete and calls!=completed:raise InputError('Provide exactly the outstanding tool results')

    async def new_session(self,owner,request):
        self.check_tool_history(request['items'],complete=True)
        if sum(s.owner==owner for s in self.sessions.values())+self.creating.get(owner,0)>=self.settings.max_live_sessions_per_user:
            raise BridgeError('session_capacity',429)
        self.creating[owner]=self.creating.get(owner,0)+1
        try:
            await self.rpc.start()
            res=await self.rpc.call('thread/start',{'model':request['model'],'cwd':self.settings.work_dir,
              'ephemeral':True,'environments':[],'approvalPolicy':'never','sandbox':'read-only',
              'developerInstructions':request['instructions'],'dynamicTools':request['dynamic'],
              'experimentalRawEvents':True,
              **({'config':{'model_verbosity':request['verbosity']}} if request.get('verbosity') else {})})
            tid=res['thread']['id']; tap=self.rpc.subscribe(tid)
            session=Session(owner,tid,self.rpc.generation,request,tap,busy=True); self.sessions[tid]=session
            return session,[]
        finally:
            self.creating[owner]-=1
            if not self.creating[owner]:self.creating.pop(owner)

    async def interrupt(self,owner,rid,reason='cancelled'):
        session=self.current.get(rid) or next((s for s in self.sessions.values()
            if s.owner==owner and s.response and s.response['id']==rid),None)
        if not session or session.owner!=owner:raise BridgeError('response_not_found',404)
        await self.drop(session,reason)

    async def segment(self,owner,request,rid,on_delta=None,on_dispatch=None,on_usage=None):
        session,continuation=await self.prepare(owner,request)
        outputs=[x for x in continuation if x.get('type') in ('function_call_output','custom_tool_call_output')]
        incoming_metadata=request['metadata']
        if continuation:session.request['items'].extend(copy.deepcopy(continuation))
        request=session.request
        self.current[rid]=session; session.touched=time.monotonic(); session.usage=None
        baseline=dict(session.accounted); text_items={}; reasoning_items={}; ordered=[]; failure=None; paused=False
        seq=0
        response={'id':rid,'object':'response','created_at':int(time.time()),'status':'in_progress',
          'model':request['model'],'output':[],'usage':None,'error':None,'metadata':incoming_metadata}
        session.response=response
        async def emit(typ,**fields):
            nonlocal seq
            event={'type':typ,'sequence_number':seq,**fields}; seq+=1
            if on_delta:await on_delta(event)
        async def message_item(iid):
            if iid not in text_items:
                item={'id':uid('msg'),'type':'message','role':'assistant','status':'in_progress',
                      'content':[{'type':'output_text','text':'','annotations':[]}]}
                text_items[iid]=item; ordered.append(item)
                await emit('response.output_item.added',output_index=len(ordered)-1,item=copy.deepcopy(item))
                await emit('response.content_part.added',item_id=item['id'],output_index=len(ordered)-1,content_index=0,part=copy.deepcopy(item['content'][0]))
            return text_items[iid]
        async def summary_item(iid,index=None):
            if iid not in reasoning_items:
                item={'id':iid,'type':'reasoning','summary':[]}
                reasoning_items[iid]=item; ordered.append(item)
                await emit('response.output_item.added',output_index=len(ordered)-1,item=copy.deepcopy(item))
            item=reasoning_items[iid]
            while index is not None and len(item['summary'])<=index:
                part={'type':'summary_text','text':''}; item['summary'].append(part)
                await emit('response.reasoning_summary_part.added',item_id=item['id'],output_index=ordered.index(item),
                    summary_index=len(item['summary'])-1,part=copy.deepcopy(part))
            return item
        async def completed_summary(raw):
            item=await summary_item(raw.get('id') or uid('rs'))
            # App-server ThreadItem uses strings; raw ResponseItem uses typed parts.
            for index,part in enumerate(raw.get('summary',[])):
                text=part if isinstance(part,str) else part['text']
                await summary_item(item['id'],index)
                old=item['summary'][index]['text']
                if text.startswith(old) and text!=old:
                    await emit('response.reasoning_summary_text.delta',item_id=item['id'],output_index=ordered.index(item),
                        summary_index=index,delta=text[len(old):])
                item['summary'][index]['text']=text
            if 'encrypted_content' in raw:item['encrypted_content']=raw['encrypted_content']
        try:
            await emit('response.created',response=copy.deepcopy(response))
            await emit('response.in_progress',response=copy.deepcopy(response))
            if outputs:
                if session.cancel_reason:raise BridgeError(session.cancel_reason,409)
                if on_dispatch:on_dispatch()
                for item in outputs:
                    p=session.pending[item['call_id']]
                    p['future'].set_result({'contentItems':tool_content(item['output']),'success':True})
                    self.calls.pop(item['call_id'],None)
                    del session.pending[item['call_id']]
            else:
                items=copy.deepcopy(request['items']); user_input=[]
                if items and items[-1].get('type')=='message' and items[-1].get('role')=='user':
                    last=items.pop()
                    for c in last['content']:
                        if c['type']=='input_image':
                            user_input.append({'type':'image','url':c['image_url'],
                                **({'detail':c['detail']} if c.get('detail') is not None else {})})
                        else:user_input.append({'type':'text','text':c['text']})
                if items:await self.rpc.call('thread/inject_items',{'threadId':session.thread,'items':items})
                params={'threadId':session.thread,'input':user_input,'model':request['model'],
                   'sandboxPolicy':{'type':'readOnly'},
                   'approvalPolicy':'never'}
                if request['effort']:params['effort']=request['effort']
                if request['summary']:params['summary']=request['summary']
                if request['schema']:params['outputSchema']=request['schema']
                if request['service_tier']:params['serviceTierForTurn']=request['service_tier']
                if session.cancel_reason:raise BridgeError(session.cancel_reason,409)
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
                if method=='relay/disconnected':raise BridgeError(session.cancel_reason or 'app_server_unavailable',409 if session.cancel_reason else 502)
                if p.get('turnId') and p['turnId']!=session.turn:continue
                if method in ('thread/tokenUsage/updated','turn/tokenUsage/updated'):
                    total=p.get('tokenUsage',{}).get('total')
                    if isinstance(total,dict):
                        session.total=total; session.usage=usage_delta(total,baseline)
                        if on_usage and not on_usage(session.usage,session.unaccounted):raise BridgeError('budget_exhausted',429)
                        session.unaccounted=[]
                elif method=='item/agentMessage/delta':
                    iid=p.get('itemId') or 'message'; delta=p.get('delta','')
                    item=await message_item(iid); item['content'][0]['text']+=delta
                    await emit('response.output_text.delta',item_id=item['id'],output_index=ordered.index(item),content_index=0,delta=delta)
                elif method in ('item/reasoning/summaryTextDelta','item/reasoning/summaryPartAdded'):
                    index=p['summaryIndex']; item=await summary_item(p['itemId'],index)
                    if method=='item/reasoning/summaryTextDelta':
                        item['summary'][index]['text']+=p['delta']
                        await emit('response.reasoning_summary_text.delta',item_id=item['id'],output_index=ordered.index(item),
                            summary_index=index,delta=p['delta'])
                elif method=='rawResponseItem/completed' and p.get('item',{}).get('type')=='reasoning':
                    await completed_summary(p['item'])
                elif method=='item/completed':
                    item=p.get('item',{})
                    if item.get('type')=='agentMessage':
                        target=await message_item(item.get('id') or 'message')
                        if 'text' in item:
                            old=target['content'][0]['text']; final=item['text']
                            target['content'][0]['text']=final
                            if final.startswith(old) and final!=old:
                                await emit('response.output_text.delta',item_id=target['id'],output_index=ordered.index(target),
                                    content_index=0,delta=final[len(old):])
                        if item.get('phase'):target['phase']=item['phase']
                    elif item.get('type')=='reasoning':await completed_summary(item)
                elif method=='turn/completed':
                    turn=p.get('turn',{})
                    if turn.get('id')!=session.turn:continue
                    session.ended=True
                    if turn.get('status')!='completed':
                        failure=upstream_failure(turn.get('error')) or failure or 'upstream_'+str(turn.get('status','failed'))
                    break
                elif method=='error':
                    # Wait for the terminal turn event; retryable errors can recover upstream.
                    if not p.get('willRetry',False):failure=upstream_failure(p.get('error')) or failure or 'upstream_error'
                elif method in ('item/started',):
                    typ=p.get('item',{}).get('type')
                    if typ in ('commandExecution','fileChange','mcpToolCall','collabToolCall','imageView'):
                        raise BridgeError('unexpected_server_tool')
            for item in ordered:
                index=ordered.index(item); item['status']='completed'
                if item['type']=='message':
                    await emit('response.output_text.done',item_id=item['id'],output_index=index,content_index=0,text=item['content'][0]['text'])
                    await emit('response.content_part.done',item_id=item['id'],output_index=index,content_index=0,part=item['content'][0])
                elif item['type']=='reasoning':
                    for summary_index,part in enumerate(item['summary']):
                        await emit('response.reasoning_summary_text.done',item_id=item['id'],output_index=index,summary_index=summary_index,text=part['text'])
                        await emit('response.reasoning_summary_part.done',item_id=item['id'],output_index=index,summary_index=summary_index,part=copy.deepcopy(part))
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
            if not failure:
                try:self.store.save_history(owner,rid,session.request['items'],self.settings.history_ttl,
                    self.settings.history_max_bytes_per_user,pending=paused)
                except HistoryError as e:raise BridgeError(e.code,e.status) from e
            await emit('response.failed' if failure else 'response.completed',response=copy.deepcopy(response))
            return response
        except BaseException:
            await self.drop(session); raise
        finally:
            self.current.pop(rid,None); session.touched=time.monotonic(); session.busy=False
            if not paused:await self.drop(session)

    async def close(self):
        for s in list(self.sessions.values()):await self.drop(s)
