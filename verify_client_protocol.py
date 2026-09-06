"""Real Codex client -> gateway -> real app-server -> localhost mock model.

No subscription credentials or real model calls. Pass the local codex executable.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

import uvicorn
import httpx
from cr.config import Settings,DISABLED_FEATURES
from cr.rpc import AppServer
from cr.server import create_app
from cr.store import Store

captured=[]
captured_headers=[]
gateway_requests=[]
compact_state={'active':False,'calls':0,'summaries':0,'steps':[]}

class Model(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        captured.append(body)
        captured_headers.append(dict(self.headers))
        text=json.dumps(body['input'])
        output_seen=any(x.get('type')=='function_call_output' for x in body['input'])
        item=None
        compact=compact_state['active']
        if compact and 'FIXTURE_COMPACT_PROMPT' in text and not body.get('tools'):
            compact_state['summaries']+=1
            compact_state['steps'].append('summary')
            answer='COMPACT_CHECKPOINT. The first fixture command finished. Continue with the next command.'
            item={'type':'message','id':'msg_compact','role':'assistant','status':'completed','phase':'final_answer',
                  'content':[{'type':'output_text','text':answer,'annotations':[]}]}
        if item is None and ((compact and compact_state['calls']<2) or ('RUN_CLIENT_TOOL' in text and not output_seen)):
            for tool in body.get('tools',[]):
                properties=tool.get('parameters',{}).get('properties',{})
                code_mode='input' in properties and 'Client tool functions.exec ' in tool.get('description','')
                if 'cmd' in properties or code_mode:
                    if compact:
                        compact_state['calls']+=1
                        n=compact_state['calls']
                        compact_state['steps'].append('tool'+str(n))
                        command=f"Add-Content -LiteralPath 'fixture-command-{n}.txt' -Value 'executed'; Write-Output 'COMPACT_TOOL_{n}_OK'"
                        if n==1:command+="; Write-Output ('fixture output ' * 12000)"
                    else:n=0; command='echo CLIENT_TOOL_OK'
                    arguments={'cmd':command,'max_output_tokens':50000}
                    if code_mode:arguments={'input':'// @exec: {"max_output_tokens": 50000}\nconst result=await tools.exec_command('+json.dumps(arguments)+');text(result.output);'}
                    item={'type':'function_call','id':f'fc_client_fixture_{n}','call_id':f'call_client_fixture_{n}',
                          'name':tool['name'],'arguments':json.dumps(arguments),'status':'completed'}
                    break
        if item is None:
            answer='CLIENT_TOOL_OK' if output_seen else 'WINDOW_B_VALUE' if 'WINDOW_B_SEED' in text else 'WINDOW_A_VALUE'
            item={'type':'message','id':'msg_client_fixture','role':'assistant','status':'completed','phase':'final_answer',
                  'content':[{'type':'output_text','text':answer,'annotations':[]}]}
        response={'id':'resp_client_fixture','object':'response','created_at':1,'model':body['model'],'status':'completed','output':[item],
                  'usage':{'input_tokens':100,'output_tokens':10,'total_tokens':110}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                {'type':'response.output_item.added','output_index':0,'item':{**item,'arguments':''} if item['type']=='function_call' else {**item,'content':[]}}]
        if item['type']=='message':
            events.append({'type':'response.output_text.delta','item_id':item['id'],'output_index':0,'content_index':0,'delta':item['content'][0]['text']})
        events+=[{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':response}]
        raw=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

class FixtureRPC(AppServer):
    async def call(self,method,params,**kw):
        if method=='account/read':return {'account':{'type':'chatgpt'}}
        if method=='account/rateLimits/read':return {}
        return await super().call(method,params,**kw)

async def main(binary,lite=False,code_mode=False):
    version_process=await asyncio.create_subprocess_exec(binary,'--version',stdout=asyncio.subprocess.PIPE)
    version,_=await version_process.communicate()
    assert version_process.returncode==0
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='relay-client-protocol-',dir=Path(__file__).parent) as directory:
        root=Path(directory); work=root/'work'; work.mkdir()
        home=root/'server-home'; home.mkdir(); client_home=root/'client-home'; client_home.mkdir()
        client_catalog=json.loads((Path(__file__).parent/'cr'/'model_catalog.json').read_text(encoding='utf-8'))
        for model in client_catalog['models']:model.update(shell_type='unified_exec',use_responses_lite=lite)
        catalog_path=root/'client-models.json'; catalog_path.write_text(json.dumps(client_catalog),encoding='utf-8')
        settings=Settings(binary=binary,codex_home=str(home),work_dir=str(work),turn_timeout=40,
            extra_args=('-c','model_provider="fixture"','-c','model_providers.fixture.name="Mock model"',
                '-c',f'model_providers.fixture.base_url="http://127.0.0.1:{upstream.server_port}/v1"',
                '-c','model_providers.fixture.wire_api="responses"','-c','model_providers.fixture.requires_openai_auth=false',
                '-c','model_providers.fixture.supports_websockets=false','app-server'))
        db_path=root/'ledger.sqlite3'; db=Store(db_path); keys=db.bootstrap(); db.close()
        sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]; sock.close()
        async def start():
            db=Store(db_path); rpc=FixtureRPC(settings)
            app=create_app(settings,rpc,db)
            @app.middleware('http')
            async def capture_gateway(request,call_next):
                if request.url.path=='/v1/responses':gateway_requests.append(json.loads(await request.body()))
                return await call_next(request)
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
            task=asyncio.create_task(server.serve())
            while not server.started:
                if task.done():await task; raise RuntimeError('Fixture gateway did not start')
                await asyncio.sleep(.05)
            return server,task
        server,task=await start()
        configs=['model_provider="relay_test"','model="gpt-6-astra"','model_reasoning_effort="medium"',
                 'model_providers.relay_test.name="Relay test"',f'model_providers.relay_test.base_url="http://127.0.0.1:{port}/v1"',
                 'model_providers.relay_test.wire_api="responses"','model_providers.relay_test.env_key="RELAY_TEST_KEY"',
                 'model_providers.relay_test.requires_openai_auth=false','model_providers.relay_test.supports_websockets=false',
                 'orchestrator.skills.enabled=false','orchestrator.mcp.enabled=false','skills.include_instructions=false',
                 'features.skip_host_skill_discovery=true','web_search="disabled"']
        # Only controlled echo / temporary fixture commands are generated here.
        configs+=['sandbox_mode="danger-full-access"','approval_policy="never"']
        if not code_mode:configs+=['model_catalog_json='+json.dumps(str(catalog_path))]
        enabled=('shell_tool','shell_snapshot','unified_exec')+(('code_mode','code_mode_only') if code_mode else ())
        configs += [f'features.{f}=false' for f in DISABLED_FEATURES if f not in enabled]
        env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE')}
        env.update(CODEX_HOME=str(client_home),RELAY_TEST_KEY=keys['user1'])
        async def client(prompt,thread=None,extra_configs=()):
            args=[binary]
            for value in [*configs,*extra_configs]:args+=['-c',value]
            args+=['exec']+(['resume',thread] if thread else [])+['--json','--skip-git-repo-check',prompt]
            process=await asyncio.create_subprocess_exec(*args,cwd=work,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:out,err=await asyncio.wait_for(process.communicate(),90)
            except BaseException:process.kill(); await process.wait(); raise
            lines=[json.loads(line) for line in out.decode().splitlines() if line.startswith('{')]
            if process.returncode or any(x.get('type')=='turn.failed' for x in lines):
                raise RuntimeError('Client failed: '+out.decode()[-3000:]+' '+err.decode()[-1500:])
            return next((x['thread_id'] for x in lines if x.get('type')=='thread.started'),thread)
        try:
            a=await client('WINDOW_A_SEED. Reply with the stored window value.')
            await client('WINDOW_B_SEED. Reply with the stored window value.')
            server.should_exit=True; await task
            server,task=await start()
            await client('Continue this window; use its earlier value.',a)
            replay=json.dumps(captured[-1]['input'])
            assert 'WINDOW_A_SEED' in replay and 'WINDOW_B_SEED' not in replay
            print('REAL_CLIENT_TWO_WINDOWS_AND_RESTART_PASSED',flush=True)
            await client('RUN_CLIENT_TOOL. Run echo CLIENT_TOOL_OK once, then return its result.')
            assert any(x.get('type')=='function_call_output' for x in captured[-1]['input'])
            assert 'CLIENT_TOOL_OK' in json.dumps(captured[-1]['input'])
            print('REAL_CLIENT_TOOL_ROUND_TRIP_PASSED',flush=True)
            compact_state.update(active=True,calls=0,summaries=0,steps=[])
            await client('RUN_COMPACT_TOOL. Run the two fixture commands in order and report completion.',extra_configs=(
                'model_auto_compact_token_limit=5000','compact_prompt="FIXTURE_COMPACT_PROMPT"'))
            assert compact_state['summaries']>=1,[(bool(b.get('tools')),len(json.dumps(b['input']))) for b in gateway_requests]
            assert compact_state['calls']==2
            assert compact_state['steps'].index('tool1')<compact_state['steps'].index('summary')<compact_state['steps'].index('tool2')
            for n in (1,2):
                assert (work/f'fixture-command-{n}.txt').read_text().splitlines()==['executed']
            compact_requests=[b for b in gateway_requests if not b.get('tools') and 'FIXTURE_COMPACT_PROMPT' in json.dumps(b['input'])]
            assert any(any(x.get('type') in ('function_call_output','custom_tool_call_output') for x in b['input']) for b in compact_requests)
            print('REAL_CLIENT_TOOL_AUTO_COMPACTION_AND_CONTINUATION_PASSED',flush=True)
            compact_state['active']=False
            markers=['RELAY_INTERNAL_USER_MARKER','RELAY_INTERNAL_BUDGET_MARKER','RELAY_INTERNAL_METADATA_MARKER',
                'RELAY_ADMINISTRATIVE_NAME_MARKER','987654319']
            ledger=Store(db_path); ledger.update_user('user1',{'name':markers[3],'budget':int(markers[4])}); ledger.close()
            async with httpx.AsyncClient() as client_http:
                response=await client_http.post(f'http://127.0.0.1:{port}/v1/responses',headers={'Authorization':'Bearer '+keys['user1']},json={
                    'input':'Public test task','user':markers[0],'safety_identifier':markers[1],
                    'metadata':{'private_marker':markers[2]},'client_metadata':{'user_id':markers[0]},
                    'text':{'verbosity':'low'}})
                assert response.status_code==200,response.text
            wire=json.dumps([captured,captured_headers])
            assert all(marker not in wire for marker in markers+list(keys.values()))
            assert captured[-1]['text']['verbosity']=='low'
            if lite or code_mode:
                assert any(b['input'][0].get('type')=='additional_tools' for b in gateway_requests if isinstance(b['input'],list))
            print('REAL_ENGINE_OUTBOUND_MANAGEMENT_BOUNDARY_AND_VERBOSITY_PASSED',flush=True)
            print(json.dumps({'passed':True,'binary_version':version.decode().strip(),'model_requests':len(captured),
                'compaction_steps':compact_state['steps'],'command_execution_counts':[1,1],
                'gateway_requests':[{'top_level_tools':bool(b.get('tools')),
                    'additional_tools':isinstance(b['input'],list) and b['input'][0].get('type')=='additional_tools',
                    'parallel_tool_calls':b.get('parallel_tool_calls')} for b in gateway_requests],
                'upstream':'localhost mock','client_mode':'default Code Mode' if code_mode else 'direct tools; isolated catalog',
                'responses_lite':lite or code_mode}),flush=True)
        finally:
            server.should_exit=True; await task; upstream.shutdown()

if __name__=='__main__':asyncio.run(main(sys.argv[1],lite='--responses-lite' in sys.argv,code_mode='--default-client' in sys.argv))
