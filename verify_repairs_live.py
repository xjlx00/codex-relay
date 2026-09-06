"""Small live smoke test using the existing login and an isolated temporary ledger."""
import argparse,asyncio,json,os,shutil,tempfile,time
from pathlib import Path
import httpx
from cr.config import Settings
from cr.rpc import AppServer
from cr.server import create_app
from cr.store import Store

async def main(args):
    report={'passed':False,'requests':[]};started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='relay-repair-live-') as directory:
        root=Path(directory);home=root/'home';home.mkdir(mode=0o700);work=root/'work';work.mkdir()
        shutil.copyfile('/var/lib/codex-relay/codex/auth.json',home/'auth.json')
        os.chmod(home/'auth.json',0o600)
        settings=Settings(binary=args.binary,codex_home=str(home),work_dir=str(work),turn_timeout=180)
        db=Store(root/'ledger.sqlite3');keys=db.bootstrap();rpc=AppServer(settings)
        app=create_app(settings,rpc,db)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
                async def request(body):
                    # Exercise the newer Codex wire format against the real model.
                    declared=body.pop('tools',[]);items=body['input']
                    if isinstance(items,str):items=[{'role':'user','content':items}]
                    body['input']=[{'type':'additional_tools','role':'developer','tools':declared}]+items
                    body['parallel_tool_calls']=False
                    response=await asyncio.wait_for(client.post('/v1/responses',headers={'Authorization':'Bearer '+keys['user1']},
                        json={'model':'gpt-6-astra','reasoning':{'effort':'low'},**body}),210)
                    assert response.status_code==200,{'http_status':response.status_code}
                    data=response.json()
                    assert data['status']=='completed',{'status':data['status'],'code':(data.get('error') or {}).get('code')}
                    report['requests'].append({'status':data['status'],'usage':data.get('usage')})
                    return data
                tools=[{'type':'function','name':'client_echo','description':'Return the supplied verification value.',
                    'parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}}]
                first=await request({'input':'Call client_echo exactly once with value REPAIR_TOOL_OK. After receiving its result, reply exactly REPAIR_LIVE_OK.',
                    'tools':tools,'parallel_tool_calls':True})
                calls=[item for item in first['output'] if item['type']=='function_call']
                assert len(calls)==1 and calls[0]['name']=='client_echo','Expected one client tool'
                assert json.loads(calls[0]['arguments'])=={'value':'REPAIR_TOOL_OK'},'Unexpected tool arguments'
                last=await request({'input':[{'type':'function_call_output','call_id':calls[0]['call_id'],'output':'REPAIR_TOOL_OK'}],
                    'previous_response_id':first['id'],'tools':tools,'parallel_tool_calls':True})
                text=''.join(part['text'] for item in last['output'] if item['type']=='message' for part in item['content'])
                assert 'REPAIR_LIVE_OK' in text,'Expected final marker'
                rows=db.recent('user1')
                assert len(rows)==2 and all(row['reserved']==0 for row in rows),'Unsettled test ledger'
                assert not app.state.bridge.sessions and app.state.scheduler.stats()['running']==0
                report.update(passed=True,tool_roundtrip=True,responses_lite=True,settled=True,seconds=round(time.monotonic()-started,2))
    Path(args.output).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True)
    parser.add_argument('--output',default='REPAIR_LIVE_REPORT.json')
    asyncio.run(main(parser.parse_args()))
