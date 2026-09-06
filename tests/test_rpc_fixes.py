import asyncio
import json
import sys

import pytest

from cr.config import Settings
from cr.rpc import AppServer,NotReady,RpcError


class FakeProcess:
    def __init__(self):
        self.stdout=asyncio.StreamReader(limit=64)
        self.stderr=asyncio.StreamReader(limit=64)
        self.stdin=self; self.returncode=None; self.exited=asyncio.Event()
        self.writes=[]; self.killed=False

    def write(self,data):self.writes.append(json.loads(data))
    async def drain(self):pass
    def terminate(self):
        self.returncode=0; self.stdout.feed_eof(); self.stderr.feed_eof(); self.exited.set()
    def kill(self):self.killed=True; self.terminate()
    async def wait(self):await self.exited.wait(); return self.returncode


async def test_stderr_long_lines_drain_without_retaining_content():
    server=AppServer(Settings()); stream=asyncio.StreamReader(limit=16)
    stream.feed_data(b'private user content'*10000); stream.feed_eof()
    await asyncio.wait_for(server._drain(stream),1)
    assert await stream.read()==b''


async def test_child_output_flood_is_drained_and_protocol_failure_exits():
    server=AppServer(Settings())
    proc=await asyncio.create_subprocess_exec(sys.executable,'-c',
        "import sys,time;sys.stderr.write('x'*300000);sys.stderr.flush();"
        "sys.stdout.write('{invalid json}\\n');sys.stdout.flush();"
        "sys.stdout.write('x'*300000);sys.stdout.flush();time.sleep(10)",
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,limit=128)
    server.proc=proc; server.ready=True
    server._reader=asyncio.create_task(server._read(proc))
    server._stderr=asyncio.create_task(server._drain(proc.stderr))
    try:
        await asyncio.wait_for(asyncio.shield(server._reader),3)
        await asyncio.wait_for(proc.wait(),3)
        assert not server.alive()
    finally:await server.stop()


async def test_tap_overflow_is_terminal_and_unsubscribes():
    server=AppServer(Settings()); tap=server.subscribe('one')
    message={'method':'item/agentMessage/delta','params':{'threadId':'one','delta':'text'}}
    for _ in range(tap.q.maxsize+1):tap.push(message)
    assert tap.closed and not server._taps and tap.q.qsize()==1
    for _ in range(10):tap.push(message)
    assert (await tap.get(1))['method']=='relay/disconnected'
    assert (await tap.get(1))['method']=='relay/disconnected'
    assert tap.q.empty()


async def test_closing_tap_wakes_waiter_and_close_is_idempotent():
    server=AppServer(Settings()); tap=server.subscribe('one')
    task=asyncio.create_task(tap.get(10)); await asyncio.sleep(0)
    tap.close(); tap.close()
    assert (await asyncio.wait_for(task,1))['method']=='relay/disconnected'
    assert not server._taps


async def test_tap_timeout_and_cancel_leave_no_queue_waiters():
    server=AppServer(Settings()); tap=server.subscribe('one')
    with pytest.raises(asyncio.TimeoutError):await tap.get(0)
    task=asyncio.create_task(tap.get(10)); await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    message={'method':'test','params':{'threadId':'one'}}
    tap.push(message)
    assert await tap.get(1)==message
    tap.close()


@pytest.mark.parametrize('line',[b'{invalid json}\n',b'x'*1000+b'\n'])
async def test_bad_stdout_fails_waiters_and_reaps_process(line):
    server=AppServer(Settings()); proc=FakeProcess(); server.proc=proc; server.ready=True
    pending=asyncio.get_running_loop().create_future(); server._pending[1]=pending
    tap=server.subscribe('one'); proc.stdout.feed_data(line)
    await asyncio.wait_for(server._read(proc),1)
    with pytest.raises(NotReady):await pending
    assert proc.killed and not server.alive()
    assert not server._pending and not server._taps
    assert (await tap.get(1))['method']=='relay/disconnected'
    await server.stop()
    assert server.proc is None


async def test_stop_cleans_handler_tasks_pipes_and_subscriptions():
    server=AppServer(Settings()); proc=FakeProcess(); server.proc=proc; server.ready=True
    server._reader=asyncio.create_task(server._read(proc))
    server._stderr=asyncio.create_task(server._drain(proc.stderr))
    handler=asyncio.create_task(asyncio.Event().wait()); server._tasks.add(handler)
    server.subscribe('one')
    await server.stop(); await server.stop()
    assert handler.cancelled() and not server._tasks
    assert not server._pending and not server._taps
    assert server.proc is None and server._reader is None and server._stderr is None


async def test_failed_initialization_cleans_up_and_can_restart(monkeypatch,tmp_path):
    server=AppServer(Settings(work_dir=str(tmp_path))); processes=[]; attempts=[]
    async def spawn(*args,**kwargs):
        proc=FakeProcess(); processes.append(proc); return proc
    async def initialize(method,params,timeout):
        assert method=='initialize' and params['clientInfo']['name']=='codex_relay'
        attempts.append(method)
        if len(attempts)==1:raise RpcError(-1,'initialization failed')
        return {}
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(server,'_call',initialize)
    with pytest.raises(RpcError):await server.start()
    assert processes[0].returncode==0 and server.proc is None
    assert server._reader is None and server._stderr is None
    await server.start()
    assert server.alive() and server.generation==2
    assert processes[1].writes==[{'method':'initialized','params':{}}]
    await server.stop()


async def test_cancelled_initialization_reaps_child(monkeypatch,tmp_path):
    server=AppServer(Settings(work_dir=str(tmp_path))); proc=FakeProcess(); initializing=asyncio.Event()
    async def spawn(*args,**kwargs):return proc
    async def initialize(*args):initializing.set(); await asyncio.Event().wait()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(server,'_call',initialize)
    task=asyncio.create_task(server.start()); await initializing.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert proc.returncode==0 and server.proc is None
    assert server._reader is None and server._stderr is None and not server._tasks


async def test_rpc_cancel_racing_result_does_not_return_success():
    server=AppServer(Settings()); proc=FakeProcess(); server.proc=proc
    task=asyncio.create_task(server._call('test',{},10)); await asyncio.sleep(0)
    pending=next(iter(server._pending.values())); pending.set_result({'ok':True}); task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not server._pending
    await server.stop()


async def test_rpc_timeout_removes_pending_request():
    server=AppServer(Settings()); server.proc=FakeProcess()
    with pytest.raises(asyncio.TimeoutError):await server._call('test',{},0)
    assert not server._pending
    await server.stop()
