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
from verify_vision import test_png
fixture_image = None
from cr.config import Settings,DISABLED_FEATURES
from cr.rpc import AppServer
from cr.server import create_app
from cr.store import Store

captured=[]

class Model(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        captured.append(body)
        text=json.dumps(body['input'])
        output_seen=any(x.get('type')=='function_call_output' for x in body['input'])
        item=None
        if 'RUN_CLIENT_TOOL' in text and not output_seen:
            for tool in body.get('tools',[]):
                properties=tool.get('parameters',{}).get('properties',{})
                if 'path' in properties and 'view_image' in tool.get('description',''):
                    item={'type':'function_call','id':'fc_client_fixture','call_id':'call_client_fixture',
                          'name':tool['name'],'arguments':json.dumps({'path':str(fixture_image)}),'status':'completed'}
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

async def main(binary,lite=False):
    global fixture_image
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='relay-client-protocol-',dir=Path(__file__).parent) as directory:
        root=Path(directory); work=root/'work'; work.mkdir()
        fixture_image=work/'asset.png';fixture_image.write_bytes(test_png())
        home=root/'server-home'; home.mkdir(); client_home=root/'client-home'; client_home.mkdir()
        # Exercise direct view_image independently of the optional Code Mode host.
        catalog_path=root/'client-models.json'
        client_catalog=json.loads((Path(__file__).parent/'cr'/'model_catalog.json').read_text(encoding='utf-8'))
        for model in client_catalog['models']:model['use_responses_lite']=lite
        catalog_path.write_text(json.dumps(client_catalog),encoding='utf-8')
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
        configs += [f'features.{f}=false' for f in DISABLED_FEATURES if f != 'view_image']
        configs.append('features.view_image=true')
        configs.append('model_catalog_json='+json.dumps(str(catalog_path)))
        # This isolated client can only read our fixture through view_image;
        # shell/edit tools are disabled. Avoid Windows sandbox setup in the test.
        configs.append('sandbox_mode="danger-full-access"')
        env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE')}
        env.update(CODEX_HOME=str(client_home),RELAY_TEST_KEY=keys['user1'])
        async def client(prompt,thread=None,image_path=None):
            args=[binary]
            for value in configs:args+=['-c',value]
            args+=['exec']+(['resume',thread] if thread else [])+['--json','--skip-git-repo-check']+(['--image',str(image_path)] if image_path else [])+['--',prompt]
            process=await asyncio.create_subprocess_exec(*args,cwd=work,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:out,err=await asyncio.wait_for(process.communicate(),90)
            except BaseException:process.kill(); await process.wait(); raise
            lines=[json.loads(line) for line in out.decode().splitlines() if line.startswith('{')]
            if process.returncode or any(x.get('type')=='turn.failed' for x in lines):
                raise RuntimeError('Client failed: '+out.decode()[-3000:]+' '+err.decode()[-1500:])
            return next((x['thread_id'] for x in lines if x.get('type')=='thread.started'),thread)
        try:
            await client('Describe the attached image.',image_path=fixture_image)
            assert any(p.get('type')=='input_image' for i in captured[-1]['input'] for p in i.get('content',[]) if isinstance(p,dict))
            await client('RUN_CLIENT_TOOL. Use view_image to view asset.png once, then answer.')
            result=next(i for i in captured[-1]['input'] if i.get('type')=='function_call_output')
            assert isinstance(result['output'],list) and any(p.get('type')=='input_image' for p in result['output']), str(result)[:1800]
            import sqlite3
            with sqlite3.connect(db_path) as ledger:
                charged=ledger.execute("SELECT SUM(charged) FROM requests WHERE user_id='user1'").fetchone()[0]
            ledger.close()
            assert charged==110*len(captured),(charged,len(captured))
            print(json.dumps({'passed':True,'client':'desktop bundled Codex executable','client_mode':'direct tools; isolated catalog and homes',
                'responses_lite':lite,'direct_image':True,'local_view_image_tool':True,'model_requests':len(captured),'charged':charged}),flush=True)
        finally:
            server.should_exit=True; await task; upstream.shutdown()

if __name__=='__main__':asyncio.run(main(sys.argv[1],lite='--responses-lite' in sys.argv))
