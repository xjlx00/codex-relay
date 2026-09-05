"""Four-person Responses gateway and private administration UI."""
import asyncio
import contextlib
import json
import time
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI,Request,HTTPException,Query
from fastapi.responses import JSONResponse,StreamingResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,ConfigDict
from .config import Settings,validate,setup_logging
from .rpc import AppServer,NotReady,RpcError
from .store import Store,BudgetError
from .scheduler import Scheduler,QueueFull
from .bridge import Bridge,BridgeError
from .translate import normalize,InputError,uid,sse
from .monitor import UpstreamMonitor

class UserPatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str|None=Field(default=None,min_length=1,max_length=40)
    budget:int|None=Field(default=None,ge=0,le=10**12,strict=True)
    active:bool|None=None
class Reconcile(BaseModel):
    charge:int=Field(ge=0,le=10**12,strict=True)
class BulkBudget(BaseModel):
    model_config=ConfigDict(extra='forbid')
    budget:int=Field(ge=0,le=10**12,strict=True)

def create_app(settings=None,rpc=None,store=None):
    settings=settings or Settings(); rpc=rpc or AppServer(settings)
    db=store; monitor=None; scheduler=Scheduler(settings.concurrent,settings.queue_limit)
    bridge=Bridge(rpc,settings); upstream_cache={'at':0,'value':None}
    @asynccontextmanager
    async def lifespan(app):
        nonlocal db,monitor
        if db is None:
            validate(settings); db=Store(settings.db)
        db.recover(); await rpc.start()
        monitor=UpstreamMonitor(rpc,db)
        monitor_task=asyncio.create_task(monitor.run())
        async def janitor():
            while True:
                await asyncio.sleep(30)
                await bridge.cleanup()
        task=asyncio.create_task(janitor())
        try:yield
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await monitor_task
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await task
            await bridge.close(); await rpc.stop(); db.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.bridge=bridge; app.state.scheduler=scheduler
    assets=Path(__file__).parent/'assets'
    allowed_models={m['slug'] for m in json.loads((Path(__file__).parent/'model_catalog.json').read_text(encoding='utf-8'))['models']}
    app.mount('/assets',StaticFiles(directory=assets),name='assets')

    @app.middleware('http')
    async def safety(request,call_next):
        # Nginx enforces the same bound; reject oversized local requests too.
        try:
            if int(request.headers.get('content-length','0'))>settings.max_body:
                return JSONResponse({'error':{'code':'body_too_large','message':'Request exceeds 2 MiB'}},413)
        except ValueError:return JSONResponse({'error':{'code':'bad_content_length'}},400)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def auth(request,role=None):
        value=request.headers.get('authorization','')
        if not value.startswith('Bearer '):raise HTTPException(401,'unauthorized')
        user=db.authenticate(value[7:])
        if not user:raise HTTPException(401,'unauthorized')
        if role and user['role']!=role:raise HTTPException(403,'forbidden')
        return user

    @app.exception_handler(HTTPException)
    async def http_error(request,e):
        return JSONResponse({'error':{'code':str(e.detail),'message':str(e.detail),'type':'relay_error'}},e.status_code)

    @app.get('/')
    async def index():return FileResponse(assets/'index.html')
    @app.get('/healthz')
    async def health():return {'ok':rpc.alive(),'version':'0.2.0'}

    @app.get('/v1/models')
    async def models(request:Request):
        auth(request)
        try:
            data=[]; cursor=None
            for _ in range(20):
                result=await rpc.call('model/list',{'limit':100,**({'cursor':cursor} if cursor else {})})
                data += [{'id':m.get('model') or m['id'],'object':'model','created':0,'owned_by':'openai'} for m in result.get('data',[]) if not m.get('hidden') and (m.get('model') or m['id']) in allowed_models]
                cursor=result.get('nextCursor')
                if not cursor:break
            return {'object':'list','data':data}
        except Exception:raise HTTPException(503,'models_unavailable')

    @app.get('/api/me')
    async def me(request:Request):
        u=auth(request)
        return {'user':u,'usage':db.usage(u['id']) if u['role']=='user' else None,
            'requests':db.recent(u['id']) if u['role']=='user' else db.recent()}
    @app.get('/v1/usage')
    @app.get('/usage')
    async def usage(request:Request):
        u=auth(request,'user'); return db.usage(u['id'])
    @app.get('/api/admin/users')
    async def users(request:Request):
        auth(request,'admin'); return [db.usage(u['id']) for u in db.users()]
    @app.post('/api/admin/usage/reset')
    async def reset_usage(request:Request):
        auth(request,'admin')
        return {'users':db.reset_all_usage(),'history_preserved':True,'holds_preserved':True}
    @app.patch('/api/admin/budget/all')
    async def bulk_budget(body:BulkBudget,request:Request):
        auth(request,'admin'); db.set_all_budgets(body.budget)
        return {'users':[db.usage(u['id']) for u in db.users()]}
    @app.get('/api/admin/statistics')
    async def statistics(request:Request,start:int=Query(ge=0),end:int=Query(ge=0,le=32503680000)):
        auth(request,'admin')
        if start>=end:raise HTTPException(400,'invalid_time_range')
        return db.statistics(start,end)
    @app.get('/api/admin/upstream/monitor')
    async def monitor_status(request:Request):
        auth(request,'admin'); return monitor.report()
    @app.post('/api/admin/upstream/sample')
    async def sample_upstream(request:Request):
        auth(request,'admin')
        # Coalesce manual requests and avoid excessive official quota polling.
        if time.time()-(monitor.state().get('last_attempt') or 0)<20:return monitor.report()
        return await monitor.sample()
    @app.patch('/api/admin/users/{user_id}')
    async def patch(user_id:str,values:UserPatch,request:Request):
        auth(request,'admin')
        if user_id not in {u['id'] for u in db.users()}:raise HTTPException(404,'user_not_found')
        update=values.model_dump(exclude_none=True)
        db.update_user(user_id,update)
        if update.get('active') is False:
            for session in list(bridge.sessions.values()):
                if session.owner==user_id:await bridge.drop(session)
        return db.usage(user_id)
    @app.post('/api/admin/users/{user_id}/rotate-key')
    async def rotate(user_id:str,request:Request):
        auth(request,'admin')
        try:key=db.rotate(user_id)
        except KeyError:raise HTTPException(404,'user_not_found')
        return {'key':key,'notice':'Shown once. The previous key is revoked.'}
    @app.post('/api/admin/requests/{request_id}/reconcile')
    async def reconcile(request_id:str,body:Reconcile,request:Request):
        auth(request,'admin')
        if not db.reconcile(request_id,body.charge):raise HTTPException(409,'not_unresolved')
        return {'ok':True}

    @app.get('/api/admin/upstream')
    async def upstream(request:Request):
        auth(request,'admin')
        if time.monotonic()-upstream_cache['at']<20:return upstream_cache['value']
        try:
            account=(await rpc.call('account/read',{})).get('account')
            data={'logged_in':bool(account),'account':None,'rate_limits':None}
            if account:
                data['account']={k:account.get(k) for k in ('type','email','planType')}
                try:data['rate_limits']=await rpc.call('account/rateLimits/read',{})
                except Exception:data['rate_limits_error']='unavailable'
            upstream_cache.update(at=time.monotonic(),value=data)
            return data
        except Exception:raise HTTPException(503,'app_server_unavailable')
    @app.post('/api/admin/login')
    async def login(request:Request):
        auth(request,'admin')
        try:
            result=await rpc.call('account/login/start',{'type':'chatgptDeviceCode'})
            upstream_cache['at']=0
            return {k:result.get(k) for k in ('loginId','verificationUrl','userCode')}
        except Exception:raise HTTPException(502,'login_start_failed')

    async def read_json(request):
        chunks=bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks)>settings.max_body:raise HTTPException(413,'body_too_large')
        try:return json.loads(chunks)
        except (ValueError,UnicodeDecodeError):raise HTTPException(400,'invalid_json')

    @app.post('/v1/responses')
    async def responses(request:Request):
        user=auth(request,'user')
        body=await read_json(request)
        try:normalized=normalize(body,settings.model)
        except InputError as e:raise HTTPException(400,str(e))
        if normalized['model'] not in allowed_models:raise HTTPException(400,'model_not_supported')
        rid=uid('resp')
        # Input-size admission estimate + bounded output reserve; final charges use official usage.
        reserve=len(json.dumps([normalized['items'],normalized['dynamic']],ensure_ascii=False).encode())+len(normalized['instructions'].encode())+settings.output_reserve
        try:db.reserve(rid,user['id'],normalized['model'],reserve)
        except BudgetError as e:raise HTTPException(429,str(e))
        queue=asyncio.Queue(256); final=None; error=None; dispatched=False
        def mark_dispatched():
            nonlocal dispatched
            dispatched=True
        async def push(event):await queue.put(event)
        async def work():
            nonlocal final,error,dispatched
            try:
                async with scheduler.slot(user['id'],settings.queue_timeout):
                    # A queued user may have been disabled or key-revoked in the meantime.
                    auth(request,'user')
                    db.running(rid)
                    # Establish account readiness before any model call; no user-visible credential data.
                    account=(await rpc.call('account/read',{})).get('account')
                    if not account:raise BridgeError('authentication_required',503)
                    final=await bridge.segment(user['id'],normalized,rid,push if normalized['stream'] else None,
                        on_dispatch=mark_dispatched,on_usage=lambda u,prior:db.checkpoint(rid,u,prior))
                    db.finish(rid,final['status'],final.get('usage'),(final.get('error') or {}).get('code'))
            except asyncio.CancelledError:
                db.finish(rid,'cancelled',error='client_disconnected',dispatched=dispatched); raise
            except (InputError,BridgeError,QueueFull,asyncio.TimeoutError,HTTPException,NotReady,RpcError) as e:
                if isinstance(e,InputError):status=400; code=str(e)
                elif isinstance(e,BridgeError):status=e.status; code=e.code
                elif isinstance(e,HTTPException):status=e.status_code; code=str(e.detail)
                elif isinstance(e,(QueueFull,asyncio.TimeoutError)):status=429; code='queue_timeout_or_full'
                else:status=502; code='upstream_protocol_error'
                error=(status,code); db.finish(rid,'failed',error=code,dispatched=dispatched)
            except Exception:
                error=(500,'internal_error'); db.finish(rid,'failed',error='internal_error',dispatched=dispatched)
        if normalized['stream']:
            async def stream():
                task=asyncio.create_task(work())
                try:
                    yield b': connected\n\n'
                    while not task.done() or not queue.empty():
                        try:ev=await asyncio.wait_for(queue.get(),1)
                        except asyncio.TimeoutError:
                            if not task.done():yield b': keepalive\n\n'
                            continue
                        yield sse(ev)
                    await task
                    if error:
                        yield sse({'type':'response.failed','response':{'id':rid,'object':'response','status':'failed',
                           'output':[],'error':{'code':error[1],'message':error[1]},'usage':None}})
                finally:
                    if not task.done():task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):await task
            return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Request-ID':rid,'X-Accel-Buffering':'no'})
        task=asyncio.create_task(work())
        try:
            while not task.done():
                await asyncio.sleep(.1)
                if await request.is_disconnected():task.cancel(); break
            await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await task
            raise
        if error:return JSONResponse({'error':{'code':error[1],'message':error[1],'type':'relay_error'}},error[0])
        return JSONResponse(final,headers={'X-Request-ID':rid})

    @app.post('/v1/responses/{response_id}/cancel')
    async def cancel(response_id:str,request:Request):
        u=auth(request,'user')
        try:await bridge.interrupt(u['id'],response_id)
        except BridgeError as e:raise HTTPException(e.status,e.code)
        return {'id':response_id,'object':'response','status':'cancelled'}
    return app

def main():
    import uvicorn
    setup_logging(); settings=Settings()
    uvicorn.run(create_app(settings),host=settings.host,port=settings.port,workers=1,access_log=False,proxy_headers=True,forwarded_allow_ips='127.0.0.1')
if __name__=='__main__':main()
