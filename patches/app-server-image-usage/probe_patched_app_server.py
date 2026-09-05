"""Run one real image through an isolated patched app-server on the existing VPS.

No upstream HTTP implementation: all model and image calls belong to app-server.
Never logs credentials or writes to the production relay ledger.
"""
import argparse,asyncio,base64,hashlib,json,os,shutil,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,'/opt/codex-relay-v2/current')
from cr.config import Settings
from cr.rpc import AppServer

def validate_usage(value):
    if not isinstance(value,dict):return {'complete':False,'reason':'usage_missing'}
    fields=('input_tokens','output_tokens','total_tokens')
    if any(type(value.get(k)) is not int or value[k]<0 for k in fields):
        return {'complete':False,'reason':'token_totals_missing_or_invalid'}
    if value['input_tokens']+value['output_tokens']!=value['total_tokens']:
        return {'complete':False,'reason':'token_totals_inconsistent'}
    for prefix in ('input','output'):
        details=value.get(prefix+'_tokens_details')
        if not isinstance(details,dict):return {'complete':False,'reason':prefix+'_details_missing'}
        if any(type(details.get(k)) is not int or details[k]<0 for k in ('image_tokens','text_tokens')):
            return {'complete':False,'reason':prefix+'_modality_details_missing'}
        if details['image_tokens']+details['text_tokens']!=value[prefix+'_tokens']:
            return {'complete':False,'reason':prefix+'_details_inconsistent'}
    return {'complete':True,'reason':'all_totals_and_modality_breakdowns_present'}

async def main(binary):
    root=Path(tempfile.mkdtemp(prefix='relay-patched-image-',dir='/tmp'));os.chmod(root,0o700)
    home=root/'home';home.mkdir(mode=0o700)
    work=root/'work';work.mkdir(mode=0o700)
    shutil.copyfile('/var/lib/codex-relay/codex/auth.json',home/'auth.json');os.chmod(home/'auth.json',0o600)
    catalog=json.loads(Path('/opt/codex-relay-v2/current/cr/model_catalog.json').read_text())
    for model in catalog['models']:
        model['base_instructions']='You are running one image compatibility test. Use the native image_gen.imagegen tool exactly once for the requested image. Do not use other tools. After generation stop with a brief confirmation.'
    (root/'models.json').write_text(json.dumps(catalog))
    server=AppServer(Settings(binary=binary,codex_home=str(home),work_dir=str(work),
        extra_args=('-c','features.image_generation=true','-c','features.imagegenext=true',
            '-c','model_catalog_json='+json.dumps(str(root/'models.json')))))
    report={'started_at':time.time(),'binary_sha256':hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
        'source_commit':'52e73e3a548ae5310c7765995b9803dd538b82b0','images':[],
        'model_usage_updates':[],'events':[],'production_ledger_written_by_probe':False}
    def clean(value):
        if isinstance(value,dict):
            return {k:('<base64 omitted>' if k=='result' and isinstance(v,str) and len(v)>1000 else clean(v)) for k,v in value.items()}
        if isinstance(value,list):return [clean(v) for v in value]
        return value
    try:
        await server.start()
        account=(await server.call('account/read',{'refreshToken':False})).get('account') or {}
        report['account']={k:account.get(k) for k in ('type','planType')}
        thread=await server.call('thread/start',{'model':'gpt-6-astra','cwd':str(work),
            'ephemeral':True,'approvalPolicy':'never','sandbox':'read-only','environments':[],
            'experimentalRawEvents':True})
        tid=thread['thread']['id'];tap=server.subscribe(tid)
        await server.call('turn/start',{'threadId':tid,'effort':'medium','input':[{'type':'text',
            'text':'Generate exactly one small, simple image: a solid green square centered on a white background, no text. Use image_gen.imagegen exactly once. This is a connectivity and usage-reporting test. Do not retry or call any other tools.'}]})
        async with asyncio.timeout(300):
            while True:
                event=await tap.get(300);method=event.get('method');params=event.get('params',{})
                item=params.get('item',{})
                if method=='item/completed' and item.get('type')=='imageGeneration':
                    entry={'item_id':item['id'],'status':item['status'],'usage':item.get('usage'),
                        'validation':validate_usage(item.get('usage')),'thread_id':tid,'turn_id':params.get('turnId')}
                    if item.get('result'):
                        raw=base64.b64decode(item['result']);(root/'image.png').write_bytes(raw)
                        entry['image_bytes']=len(raw);entry['image_sha256']=hashlib.sha256(raw).hexdigest()
                    report['images'].append(entry);print('IMAGE_RESULT',json.dumps(entry),flush=True)
                if method in ('thread/tokenUsage/updated','turn/tokenUsage/updated'):
                    report['model_usage_updates'].append(params.get('tokenUsage'))
                if method in ('item/completed','rawResponse/completed','thread/tokenUsage/updated','error','turn/completed'):
                    report['events'].append(clean(event))
                if method=='turn/completed':
                    report['turn_status']=params.get('turn',{}).get('status');break
                if method=='relay/disconnected':raise RuntimeError('app_server_disconnected')
        report['passed']=len(report['images'])==1 and report['images'][0]['status']=='completed' and report['images'][0]['validation']['complete']
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)[:250];report['passed']=False
    finally:
        await server.stop();(home/'auth.json').unlink(missing_ok=True)
        report['finished_at']=time.time();(root/'report.json').write_text(json.dumps(report,indent=2))
        print('PROBE_RESULT',json.dumps({'passed':report.get('passed',False),'error':report.get('error'),
            'evidence':str(root),'images':len(report['images']),'seconds':round(report['finished_at']-report['started_at'],1)}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True)
    asyncio.run(main(parser.parse_args().binary))
