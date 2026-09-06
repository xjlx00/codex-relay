"""Real HTTP gateway/app-server capacity check against a localhost-only model.

python verify_capacity.py /path/to/codex --output CAPACITY_REPORT.json
For a standalone app-server executable, also pass --standalone.
Temporary credentials are generated locally; no subscription account is used.
"""
import argparse
import asyncio
import base64
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import re
import socket
import struct
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zlib

import httpx
import uvicorn

from cr.config import Settings
from cr.rpc import AppServer
from cr.server import create_app
from cr.store import Store


def process_rss(pid):
    """Current resident bytes, not virtual size or a cgroup peak."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak', 'working', 'quota_peak_paged',
                'quota_paged', 'quota_peak_nonpaged', 'quota_nonpaged', 'pagefile', 'peak_pagefile')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        read = ctypes.WinDLL('psapi').GetProcessMemoryInfo
        read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle: return None
        try:
            data = Counters(); data.cb = ctypes.sizeof(data)
            return data.working if read(handle, ctypes.byref(data), data.cb) else None
        finally: kernel.CloseHandle(handle)
    try:
        return int(Path(f'/proc/{pid}/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        return None


class FixtureRPC(AppServer):
    async def call(self, method, params, **kwargs):
        if method == 'account/read': return {'account': {'type': 'chatgpt'}}
        if method == 'account/rateLimits/read': return {}
        return await super().call(method, params, **kwargs)


async def verify(binary, standalone):
    # Incompressible RGB pixels exercise image transport and decoding; padding a
    # tiny PNG's metadata would not represent the same image workload.
    def image_fixture():
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        width, height = 1024, 683
        pixels = random.Random(0).randbytes(width * height * 3)
        rows = b''.join(b'\0' + pixels[start:start + width * 3] for start in range(0, len(pixels), width * 3))
        return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))

    png = image_fixture()
    image_url = 'data:image/png;base64,' + base64.b64encode(png).decode()
    history_context = ('Historical context for capacity validation. ' * 2000)[:64 * 1024]
    lock = threading.Lock()
    captured = []
    stages = {}
    failures = []
    active = 0
    maximum = 0

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            nonlocal active, maximum
            counted = False
            try:
                body_bytes = int(self.headers['Content-Length'])
                body = json.loads(self.rfile.read(body_bytes))
                markers = set(re.findall(r'CAPACITY_(DEFAULT|SIX)_USER([1-4])_([0-9]+)', json.dumps(body['input'])))
                assert len(markers) == 1, 'Missing or mixed request identity at model'
                phase, user, index = markers.pop()
                marker = f'CAPACITY_{phase}_USER{user}_{index}'
                stage = stages[phase]
                if phase == 'SIX':
                    parts = [part for item in body['input'] for part in item.get('content', [])]
                    assert sum(part.get('type') == 'input_image' for part in parts) == 1, 'Image missing at model'
                    assert any(history_context in part.get('text', '') for part in parts), 'History missing at model'
                with lock:
                    assert marker not in captured, 'Duplicate model dispatch'
                    captured.append(marker)
                    active += 1; counted = True
                    maximum = max(maximum, active)
                    stage['maximum'] = max(stage['maximum'], active)
                    stage['arrived'].append(marker)
                    stage['model_body_bytes'].append(body_bytes)
                assert stage['release'].wait(45), 'Model barrier timed out'
                item = {'id': 'msg_' + marker, 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'phase': 'final_answer',
                        'content': [{'type': 'output_text', 'text': marker, 'annotations': []}]}
                response = {'id': 'resp_' + marker, 'object': 'response', 'created_at': 1,
                            'model': body['model'], 'status': 'completed', 'output': [item],
                            'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110}}
                events = [
                    {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'content': []}},
                    {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0,
                     'content_index': 0, 'delta': marker},
                    {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                    {'type': 'response.completed', 'response': response},
                ]
                raw = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n'
                              for event in events).encode()
                # Finish/decrement under the same lock so a newly dispatched
                # response cannot count an already fully written response twice.
                with lock:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers(); self.wfile.write(raw); self.wfile.flush()
                    active -= 1; counted = False
            except Exception as exc:
                with lock: failures.append(str(exc))
            finally:
                if counted:
                    with lock: active -= 1

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    upstream.daemon_threads = True
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    memory = {'python_fixture_peak_rss_bytes': None, 'app_server_peak_rss_bytes': None,
              'sampled_combined_peak_rss_bytes': None, 'sample_interval_seconds': .05,
              'scope': 'Python includes HTTP fixture and gateway; app-server is one process. Sampled RSS, not total cgroup peak. Six image/context requests, not six 100 MiB histories or real-model capacity.'}
    results = []
    submitted = []
    tasks = []
    with tempfile.TemporaryDirectory(prefix='relay-capacity-') as directory:
        root = Path(directory)
        home = root / 'codex-home'; home.mkdir()
        work = root / 'work'; work.mkdir()
        provider = ('-c', 'model_provider="fixture"', '-c', 'model_providers.fixture.name="Local capacity fixture"',
                    '-c', f'model_providers.fixture.base_url="http://127.0.0.1:{upstream.server_port}/v1"',
                    '-c', 'model_providers.fixture.wire_api="responses"',
                    '-c', 'model_providers.fixture.requires_openai_auth=false',
                    '-c', 'model_providers.fixture.supports_websockets=false')
        settings = Settings(binary=str(binary), codex_home=str(home), work_dir=str(work),
                            db=str(root / 'ledger.sqlite'), turn_timeout=60,
                            extra_args=provider + (() if standalone else ('app-server',)))
        db = Store(settings.db); keys = db.bootstrap()
        rpc = FixtureRPC(settings)
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        app = create_app(settings, rpc, db)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False,
                                             timeout_graceful_shutdown=5))
        serving = asyncio.create_task(server.serve(sockets=[listener]))

        async def sample_memory():
            while True:
                python = process_rss(os.getpid())
                engine = process_rss(rpc.proc.pid) if rpc.proc else None
                for key, value in [('python_fixture_peak_rss_bytes', python),
                                   ('app_server_peak_rss_bytes', engine),
                                   ('sampled_combined_peak_rss_bytes', python + engine if python is not None and engine is not None else None)]:
                    if value is not None: memory[key] = max(memory[key] or 0, value)
                await asyncio.sleep(.05)

        sampling = asyncio.create_task(sample_memory())

        async def wait_until(predicate, message, timeout=40):
            async def poll():
                while not predicate():
                    if failures: raise AssertionError(failures[0])
                    if serving.done(): await serving; raise RuntimeError('Gateway exited')
                    await asyncio.sleep(.02)
            try: await asyncio.wait_for(poll(), timeout)
            except asyncio.TimeoutError: raise RuntimeError(message) from None

        try:
            await wait_until(lambda: server.started, 'Gateway startup timed out')
            headers = lambda user: {'Authorization': 'Bearer ' + keys[user]}
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=65, trust_env=False) as client:
                async def get(path, user='admin'):
                    response = await client.get(path, headers=headers(user))
                    assert response.status_code == 200, 'Gateway read failed'
                    return response.json()

                async def submit(phase, user, index):
                    marker = f'CAPACITY_{phase}_{user.upper()}_{index}'
                    payload = {'model': 'gpt-6-astra', 'input': marker, 'reasoning': {'effort': 'medium'}}
                    if phase == 'SIX':
                        payload['input'] = [
                            {'role': 'user', 'content': history_context},
                            {'role': 'assistant', 'content': 'Context received.'},
                            {'role': 'user', 'content': [{'type': 'input_text', 'text': marker},
                                {'type': 'input_image', 'image_url': image_url, 'detail': 'original'}]},
                        ]
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
                    stages[phase]['gateway_body_bytes'].append(len(encoded))
                    response = await client.post('/v1/responses',
                        headers={**headers(user), 'Content-Type': 'application/json'}, content=encoded)
                    assert response.status_code == 200, f'{marker}: HTTP {response.status_code}'
                    data = response.json()
                    assert data['status'] == 'completed', f'{marker}: incomplete response'
                    text = ''.join(part.get('text', '') for item in data['output'] for part in item.get('content', []))
                    assert text == marker, 'Response crossed request ownership'
                    assert data['usage']['total_tokens'] == 110, 'Wrong response usage'
                    submitted.append((user, data['id']))

                for phase, counts, queued_user in [('DEFAULT', [1, 1, 1, 1], 'user1'), ('SIX', [3, 2, 1, 0], 'user4')]:
                    if phase == 'SIX':
                        for user, limit in [('user1', 3), ('user2', 2), ('user3', 1)]:
                            response = await client.patch('/api/admin/users/' + user,
                                headers=headers('admin'), json={'concurrent_limit': limit})
                            assert response.status_code == 200 and response.json()['concurrent_limit'] == limit
                    expected = sum(counts)
                    stage = stages[phase] = {'release': threading.Event(), 'arrived': [], 'maximum': 0,
                                            'gateway_body_bytes': [], 'model_body_bytes': []}
                    batch = [asyncio.create_task(submit(phase, f'user{number}', index))
                             for number, count in enumerate(counts, 1) for index in range(count)]
                    tasks.extend(batch)
                    await wait_until(lambda: len(stage['arrived']) == expected, f'{phase}: barrier was not filled')
                    extra = asyncio.create_task(submit(phase, queued_user, 9)); tasks.append(extra); batch.append(extra)
                    for _ in range(100):
                        stats = (await get('/api/me'))['concurrency']
                        if stats['waiting'] == 1: break
                        await asyncio.sleep(.02)
                    assert stats == {'running': expected, 'limit': 6, 'waiting': 1}, 'Wrong gateway capacity or waiting count'
                    users = await get('/api/admin/users')
                    limits = [1, 1, 1, 1] if phase == 'DEFAULT' else [3, 2, 1, 1]
                    for number, user in enumerate(users, 1):
                        assert user['concurrent_limit'] == limits[number - 1], 'Wrong persisted user limit'
                        assert user['concurrency']['running'] == counts[number - 1], 'Per-user limit breached'
                        assert user['concurrency']['waiting'] == int(user['id'] == queued_user), 'Wrong queue owner'
                    await asyncio.sleep(.1)  # Sample resident memory while the full barrier is held.
                    assert len(stage['arrived']) == expected and not extra.done(), 'Queued request dispatched early'
                    stage['release'].set()
                    await asyncio.wait_for(asyncio.gather(*batch), 40)
                    assert stage['maximum'] == expected, 'Wrong upstream maximum'
                    assert (await get('/api/me'))['concurrency']['running'] == 0
                    results.append({'phase': phase.lower(), 'max_active_model_requests': stage['maximum'],
                                    'user_limits': limits, 'queued_request_blocked': True,
                                    'completed_requests': len(batch),
                                    'image_bytes_per_request': len(png) if phase == 'SIX' else 0,
                                    'history_text_bytes_per_request': len(history_context.encode()) if phase == 'SIX' else 0,
                                    'gateway_request_bytes_range': [min(stage['gateway_body_bytes']), max(stage['gateway_body_bytes'])],
                                    'model_request_bytes_range': [min(stage['model_body_bytes']), max(stage['model_body_bytes'])]})

                assert len(submitted) == 12 and len({rid for _, rid in submitted}) == 12
                for user in ('user1', 'user2', 'user3', 'user4'):
                    me = await get('/api/me', user)
                    expected_ids = {rid for owner, rid in submitted if owner == user}
                    assert {row['id'] for row in me['requests']} == expected_ids, 'Ledger owner mismatch'
                    assert all(row['user_id'] == user and row['charged'] == 110 and row['reserved'] == 0
                               and row['status'] == 'completed' and row['authoritative'] == 1
                               for row in me['requests']), 'Ledger settlement mismatch'
                    assert me['usage']['used'] == len(expected_ids) * 110 and me['usage']['held'] == 0
                foreign = next(rid for user, rid in submitted if user == 'user1')
                denied = await client.post('/v1/responses', headers=headers('user2'),
                    json={'model': 'gpt-6-astra', 'previous_response_id': foreign, 'input': 'Never dispatch this request'})
                assert denied.status_code == 404, 'Cross-user response history was accepted'
                assert len(captured) == 12 and not failures and db.token_counter() == 1320
                assert all(row['concurrent_limit'] == limit for row, limit in zip(db.users(), [3, 2, 1, 1]))
            return {'status': 'passed', 'created_at': datetime.now(timezone.utc).isoformat(),
                    'binary': binary.name, 'standalone': standalone, 'model': 'gpt-6-astra',
                    'upstream': 'localhost mock Responses; no real subscription or model call',
                    'phases': results, 'max_active_model_requests': maximum, 'requests': 12,
                    'tokens_per_request': 110, 'ledger_tokens': 1320,
                    'response_and_ledger_ownership': 'passed', 'cross_user_history': 'rejected', 'memory': memory}
        finally:
            for stage in stages.values(): stage['release'].set()
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            server.should_exit = True
            try:
                try: await asyncio.wait_for(serving, 12)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    serving.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await serving
            finally:
                await rpc.stop(); db.close(); listener.close()
                sampling.cancel()
                with contextlib.suppress(asyncio.CancelledError): await sampling
                await asyncio.to_thread(upstream.shutdown)
                upstream.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--standalone', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.binary.is_file(): parser.error('binary must point to an existing official executable')
    try: report = asyncio.run(verify(args.binary.resolve(), args.standalone))
    except Exception as exc: report = {'status': 'failed', 'error': str(exc)}
    raw = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output: args.output.write_text(raw + '\n', encoding='utf-8')
    print(raw)
    raise SystemExit(0 if report['status'] == 'passed' else 1)
