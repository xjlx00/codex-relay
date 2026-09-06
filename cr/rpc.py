"""Supervised official app-server stdio. No upstream HTTP implementation."""
import asyncio
import json
import os
import contextlib
from pathlib import Path
from .config import DISABLED_FEATURES

class RpcError(RuntimeError):
    def __init__(self,code,message):
        super().__init__(message); self.code=code

class NotReady(RuntimeError):pass

class AppServer:
    def __init__(self,settings):
        self.settings=settings; self.proc=None; self.generation=0; self.ready=False
        self._pending={}; self._taps=set(); self._tasks=set()
        self._reader=None; self._stderr=None; self._counter=0
        self._lock=asyncio.Lock(); self._write_lock=asyncio.Lock(); self.handler=None

    def alive(self):return self.ready and self.proc is not None and self.proc.returncode is None

    async def start(self):
        async with self._lock:
            if self.alive():return
            await self.stop(); s=self.settings
            env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR','SYSTEMROOT','WINDIR','TEMP','TMP')}
            env.update(CODEX_HOME=s.codex_home,RUST_LOG='error')
            args=['-c','sandbox_mode="read-only"','-c','approval_policy="never"','-c','web_search="disabled"',
                  '-c','features.skip_host_skill_discovery=true','-c','tools.update_plan.enabled=false',
                  '-c','tools.experimental_request_user_input.enabled=false']
            args+=['-c','orchestrator.skills.enabled=false','-c','orchestrator.mcp.enabled=false',
                   '-c','skills.include_instructions=false',
                   '-c','model_catalog_json='+json.dumps(str(Path(__file__).parent/'model_catalog.json'))]
            for feature in DISABLED_FEATURES:args+=['-c',f'features.{feature}=false']
            args+=list(s.extra_args)
            self.proc=await asyncio.create_subprocess_exec(s.binary,*args,stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,cwd=s.work_dir,
                limit=s.history_max_bytes_per_user+s.max_body+1024*1024)
            self.generation+=1
            self._reader=asyncio.create_task(self._read(self.proc))
            self._stderr=asyncio.create_task(self._drain(self.proc.stderr))
            try:
                await self._call('initialize',{'clientInfo':{'name':'codex_relay','title':'Codex Relay','version':'0.3.0'},
                              'capabilities':{'experimentalApi':True}},30)
                await self._write({'method':'initialized','params':{}}); self.ready=True
            except BaseException:
                await self.stop(); raise

    async def stop(self):
        self._fail()
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):self.proc.terminate()
            try:await asyncio.wait_for(self.proc.wait(),5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):self.proc.kill()
                await self.proc.wait()
        current=asyncio.current_task()
        for task in [self._reader,self._stderr,*list(self._tasks)]:
            if task and task is not current:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError,Exception):await task
        self._tasks.clear(); self._reader=None; self._stderr=None; self.proc=None

    def _fail(self):
        self.ready=False
        for fut in self._pending.values():
            if not fut.done():fut.set_exception(NotReady('app_server_unavailable'))
        self._pending.clear()
        for tap in list(self._taps):tap.fail()
        for task in list(self._tasks):
            if task is not asyncio.current_task():task.cancel()

    async def _drain(self,stream):
        # stderr can include user content; drain it without persisting it.
        while await stream.read(65536):pass

    async def _read(self,proc):
        try:
            while line:=await proc.stdout.readline():
                msg=json.loads(line)
                if 'id' in msg and ('result' in msg or 'error' in msg):
                    fut=self._pending.pop(msg['id'],None)
                    if fut and not fut.done():
                        if msg.get('error'):
                            e=msg['error']; fut.set_exception(RpcError(e.get('code'),e.get('message','RPC failed')))
                        else:fut.set_result(msg.get('result'))
                elif 'id' in msg and 'method' in msg:
                    task=asyncio.create_task(self._handle(msg)); self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif 'method' in msg:
                    for tap in list(self._taps):tap.push(msg)
        except asyncio.CancelledError:raise
        except Exception:
            if proc is self.proc:
                self._fail()
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):proc.kill()
            # Drain remaining pipe bytes so wait() cannot deadlock after a
            # malformed or oversized JSON line stopped normal processing.
            await self._drain(proc.stdout)
        finally:
            if proc is self.proc:self._fail()

    async def _handle(self,msg):
        try:
            method=msg['method']
            if method=='item/tool/call' and self.handler:result=await self.handler(msg.get('params',{}))
            elif method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):result={'decision':'decline'}
            elif method=='item/permissions/requestApproval':result={'permissions':{},'scope':'turn'}
            elif method=='item/tool/requestUserInput':result={'answers':{}}
            else:
                await self._write({'id':msg['id'],'error':{'code':-32601,'message':'Unsupported server request'}}); return
            await self._write({'id':msg['id'],'result':result})
        except (asyncio.CancelledError,NotReady,ConnectionError):pass
        except Exception:
            with contextlib.suppress(Exception):
                await self._write({'id':msg['id'],'error':{'code':-32000,'message':'Tool bridge failed'}})

    async def _write(self,msg):
        async with self._write_lock:
            if not self.proc or self.proc.returncode is not None:raise NotReady('app_server_unavailable')
            self.proc.stdin.write((json.dumps(msg,ensure_ascii=False)+'\n').encode()); await self.proc.stdin.drain()

    async def _call(self,method,params,timeout):
        self._counter+=1; rid=self._counter
        fut=asyncio.get_running_loop().create_future(); self._pending[rid]=fut
        try:
            await self._write({'id':rid,'method':method,'params':params})
            done,_=await asyncio.wait((fut,),timeout=timeout)
            if not done:raise asyncio.TimeoutError()
            return fut.result()
        finally:
            self._pending.pop(rid,None)
            if not fut.done():fut.cancel()
            elif not fut.cancelled():fut.exception()

    async def call(self,method,params,timeout=60):
        if not self.alive():await self.start()
        return await self._call(method,params,timeout)

    def subscribe(self,thread_id=None):
        tap=Tap(self,thread_id); self._taps.add(tap); return tap

class Tap:
    def __init__(self,server,thread_id):
        self.server=server; self.thread_id=thread_id; self.q=asyncio.Queue(2048); self.closed=False
    def push(self,msg):
        if self.closed:return
        if self.thread_id and msg.get('params',{}).get('threadId')!=self.thread_id:return
        try:self.q.put_nowait(msg)
        except asyncio.QueueFull:self.fail()
    def fail(self):
        if self.closed:return
        self.closed=True; self.server._taps.discard(self)
        while not self.q.empty():self.q.get_nowait()
        self.q.put_nowait({'method':'relay/disconnected','params':{}})
    async def get(self,timeout):
        if self.closed and self.q.empty():return {'method':'relay/disconnected','params':{}}
        waiter=asyncio.create_task(self.q.get())
        try:
            done,_=await asyncio.wait((waiter,),timeout=timeout)
            if not done:raise asyncio.TimeoutError()
            return waiter.result()
        finally:
            if not waiter.done():
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):await waiter
    def close(self):self.fail()
