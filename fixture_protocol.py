"""Run the installed official binary against a localhost-only mock model."""
import asyncio,json,tempfile,threading,os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from cr.config import Settings
from cr.rpc import AppServer
from cr.bridge import Bridge,BridgeError
from cr.store import Store
from cr.translate import normalize
captured=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        captured.append(body)
        output_seen=any(x.get('type')=='function_call_output' for x in body['input'])
        call=any(t.get('name')=='relay_tool_0' for t in body.get('tools',[])) and not output_seen
        if call:
            item={'type':'function_call','id':'fc_fixture','call_id':'call_fixture','name':'relay_tool_0','arguments':'{"path":"README.md"}','status':'completed'}
        else:
            item={'type':'message','id':'msg_fixture','role':'assistant','status':'completed','content':[{'type':'output_text','text':'fixture OK','annotations':[]}],'phase':'final_answer'}
        response={'id':'resp_fixture','object':'response','created_at':1,'model':body['model'],'status':'completed','output':[item],
            'usage':{'input_tokens':100,'input_tokens_details':{'cached_tokens':20},'output_tokens':10,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':110}}
        events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
            {'type':'response.output_item.added','output_index':0,'item':{**item,'arguments':''} if call else {**item,'content':[]}}]
        if not call:
            events += [{'type':'response.content_part.added','item_id':item['id'],'output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}},
                {'type':'response.output_text.delta','item_id':item['id'],'output_index':0,'content_index':0,'delta':'fixture OK'}]
        events += [{'type':'response.output_item.done','output_index':0,'item':item},{'type':'response.completed','response':response}]
        raw=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)

async def main():
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler); threading.Thread(target=http.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='relay-protocol-') as root:
        home=Path(root)/'codex'; work=Path(root)/'work'; home.mkdir(); work.mkdir()
        s=Settings(codex_home=str(home),work_dir=str(work),turn_timeout=30,extra_args=(
            '-c','model_provider="fixture"','-c','model_providers.fixture.name="Local protocol fixture"',
            '-c',f'model_providers.fixture.base_url="http://127.0.0.1:{http.server_port}/v1"',
            '-c','model_providers.fixture.wire_api="responses"','-c','model_providers.fixture.requires_openai_auth=false',
            '-c','model_providers.fixture.supports_websockets=false'))
        db=Store(Path(root)/'history.sqlite3'); db.bootstrap()
        rpc=AppServer(s); bridge=Bridge(rpc,s,db)
        try:
            await rpc.start(); print('INITIALIZED',flush=True)
            events=rpc.subscribe()
            model=os.environ.get('FIXTURE_MODEL','gpt-5.4')
            req=normalize({'model':model,'input':'Reply with fixture OK'},s.model)
            out=await bridge.segment('user1',req,'resp_test1')
            print('TEXT_RESULT',json.dumps(out),flush=True)
            assert out['status']=='completed' and out['output'][0]['content'][0]['text']=='fixture OK'
            assert out['usage']['total_tokens']==110
            assert captured[-1].get('tools',[])==[], captured[-1].get('tools')
            await bridge.close(); await rpc.stop(); db.close()
            db=Store(Path(root)/'history.sqlite3'); rpc=AppServer(s); bridge=Bridge(rpc,s,db)
            await rpc.start(); events=rpc.subscribe()
            previous=normalize({'model':model,'input':'A second message','previous_response_id':'resp_test1'},s.model)
            continued=await bridge.segment('user1',previous,'resp_history')
            assert continued['status']=='completed'
            assert any(x.get('role')=='assistant' for x in captured[-1]['input'])
            try:await bridge.segment('user2',previous,'stolen')
            except BridgeError as error:assert error.status==404
            else:raise AssertionError('Cross-user continuation was accepted')
            print('HISTORY_RESTART_AND_OWNERSHIP_PASSED',flush=True)
            req=normalize({'model':model,'input':'Read README.md with the supplied tool',
                'tools':[{'type':'function','name':'read_file','parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}]},s.model)
            out=await bridge.segment('user1',req,'resp_test2'); print('TOOL_RESULT',json.dumps(out),flush=True)
            call=out['output'][-1]; assert call['type']=='function_call'
            req2={**req,'items':[{'type':'function_call_output','call_id':call['call_id'],'output':'Example README'},
                {'type':'message','role':'user','content':[{'type':'input_text','text':'NEW MESSAGE DURING TOOL WAIT'}]}]}
            out2=await bridge.segment('user1',req2,'resp_test3'); print('RESUME_RESULT',json.dumps(out2),flush=True)
            assert out2['status']=='completed' and out2['output'][0]['content'][0]['text']=='fixture OK'
            assert (out.get('usage') or {}).get('total_tokens',0)+out2['usage']['total_tokens']==220
            assert all(t.get('name')=='relay_tool_0' for t in captured[-1].get('tools',[]))
            assert 'NEW MESSAGE DURING TOOL WAIT' in json.dumps(captured[-1]['input'])
            assert 'NEW MESSAGE DURING TOOL WAIT' in json.dumps(db.load_history('user1','resp_test3')['history'])
            print('PENDING_TOOL_NEW_MESSAGE_PASSED',flush=True)
            print('OFFICIAL_BINARY_FIXTURE_PASSED',flush=True)
            methods={}
            while not events.q.empty():
                event=events.q.get_nowait(); method=event['method']
                if 'raw' in method.lower():methods[method]=event.get('params')
            print('RAW_NOTIFICATION_EXAMPLES',json.dumps(methods),flush=True)
        finally:
            print('CAPTURED_TOOLS',json.dumps([[t.get('name') or t.get('type') for t in b.get('tools',[])] for b in captured]),flush=True)
            await bridge.close(); await rpc.stop(); db.close(); http.shutdown()
if __name__=='__main__':asyncio.run(main())
