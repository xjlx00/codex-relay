"""Real app-server through the gateway, using a mock model or existing subscription.
The live probe uses its own ledger and removes its temporary credential copy.
"""
import argparse,asyncio,base64,hashlib,json,os,shutil,struct,tempfile,threading,zlib
from pathlib import Path
from http.server import ThreadingHTTPServer
import httpx
from cr.config import Settings
from cr.rpc import AppServer
from cr.server import create_app
from cr.store import Store
from fixture_protocol import Handler,captured

def test_png():
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    pixels=b''.join(b'\0'+b'\xff\0\0'*128+b'\0\0\xff'*128 for _ in range(128))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',256,128,8,2,0,0,0))+
            chunk(b'IDAT',zlib.compress(pixels))+chunk(b'tEXt',b'fixture\0'+b'x'*(2*1024*1024))+chunk(b'IEND',b''))

class MockAccountRPC(AppServer):
    async def call(self,method,params,**kw):
        if method=='account/read':return {'account':{'type':'chatgpt'}}
        if method=='account/rateLimits/read':return {}
        return await super().call(method,params,**kw)

async def main(args):
    root=Path(tempfile.mkdtemp(prefix='relay-vision-'));os.chmod(root,0o700)
    home=root/'home';home.mkdir(mode=0o700);work=root/'work';work.mkdir()
    image=test_png();(root/'fixture.png').write_bytes(image)
    url='data:image/png;base64,'+base64.b64encode(image).decode()
    part={'type':'input_image','image_url':url,'detail':'original'}
    upstream=None;extra=('app-server',) if args.binary.endswith('.exe') else ()
    if args.live:
        shutil.copyfile('/var/lib/codex-relay/codex/auth.json',home/'auth.json');os.chmod(home/'auth.json',0o600)
    else:
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        extra=('-c','model_provider="fixture"','-c','model_providers.fixture.name="Vision fixture"',
               '-c',f'model_providers.fixture.base_url="http://127.0.0.1:{upstream.server_port}/v1"',
               '-c','model_providers.fixture.wire_api="responses"','-c','model_providers.fixture.requires_openai_auth=false',
               '-c','model_providers.fixture.supports_websockets=false')+extra
    settings=Settings(binary=args.binary,codex_home=str(home),work_dir=str(work),turn_timeout=180,extra_args=extra)
    db=Store(root/'ledger.db');keys=db.bootstrap();rpc=(AppServer if args.live else MockAccountRPC)(settings)
    app=create_app(settings,rpc,db);report={'live':args.live,'requests':[],'image_bytes':len(image),'passed':False}
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',timeout=200) as client:
                async def send(body):
                    response=await client.post('/v1/responses',headers={'Authorization':'Bearer '+keys['user1']},
                                              json={'reasoning':{'effort':'medium'},**body})
                    assert response.status_code==200,(response.status_code,response.text[:250])
                    result=response.json();assert result['status']=='completed',result.get('error')
                    text=' '.join(p['text'] for i in result['output'] if i['type']=='message' for p in i['content'])
                    report['requests'].append({'id':result['id'],'usage':result['usage'],'text':text})
                    return result,text
                direct,text=await send({'input':[{'role':'user','content':[{'type':'input_text','text':'What colors are the left and right halves of this image? Answer in English.'},part]}]})
                if args.live:assert 'red' in text.lower() and 'blue' in text.lower(),text
                else:assert any(p.get('type')=='input_image' for i in captured[-1]['input'] for p in i.get('content',[]) if isinstance(p,dict))
                _,text=await send({'input':'What was the color of the right half? Answer in English.','previous_response_id':direct['id']})
                if args.live:assert 'blue' in text.lower(),text
                tools=[{'type':'function','name':'view_image','description':'Read a local project image and return its pixels.',
                        'parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}]
                first,_=await send({'input':'Call view_image exactly once with path asset.png, then describe the left and right colors. Do not guess without the image. Answer in English.','tools':tools})
                call=next(i for i in first['output'] if i['type']=='function_call')
                output=[{'type':'input_text','text':'Image read from asset.png'},part]
                second,text=await send({'input':[{'type':'function_call_output','call_id':call['call_id'],'output':output}],
                                        'previous_response_id':first['id'],'tools':tools})
                if args.live:assert 'red' in text.lower() and 'blue' in text.lower(),text
                else:
                    result=next(i for i in captured[-1]['input'] if i['type']=='function_call_output')
                    assert any(p.get('type')=='input_image' for p in result['output']),result
                _,text=await send({'input':'Recall the right-hand color from the image. Answer in English.',
                                  'previous_response_id':second['id'],'tools':tools})
                if args.live:assert 'blue' in text.lower(),text
                total=sum((r['usage'] or {}).get('total_tokens',0) for r in report['requests'])
                assert total>0 and db.usage('user1')['used']==total and db.usage('user1')['held']==0
                assert db.usage('user2')['used']==0
                report.update(passed=True,ledger_used=total,other_user_used=0)
    finally:
        (home/'auth.json').unlink(missing_ok=True)
        if upstream:upstream.shutdown()
        (root/'report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'evidence':str(root),**report}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True);parser.add_argument('--live',action='store_true')
    asyncio.run(main(parser.parse_args()))
