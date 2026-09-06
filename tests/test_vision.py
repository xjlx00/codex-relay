import base64,copy,json
import httpx,pytest
from cr.bridge import Bridge
from cr.config import Settings
from cr.server import create_app
from cr.store import Store
from cr.translate import normalize,InputError
from test_bridge_gateway import FakeRPC

PNG='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=='
PART={'type':'input_image','image_url':PNG,'detail':'original'}

class ImageRPC(FakeRPC):
    async def produce(self,tid,turn):
        self.emit('thread/tokenUsage/updated',tid,turnId=turn,tokenUsage={'total':{'inputTokens':100,'outputTokens':10}})
        if self.mode=='tool':
            self.received=await self.handler({'threadId':tid,'turnId':turn,'tool':'relay_tool_0','arguments':{}})
            self.emit('thread/tokenUsage/updated',tid,turnId=turn,tokenUsage={'total':{'inputTokens':420,'outputTokens':30}})
        self.emit('item/agentMessage/delta',tid,turnId=turn,itemId='image-answer',delta='red square')
        self.emit('turn/completed',tid,turn={'id':turn,'status':'completed'})

async def test_images_tool_continuation_history_and_billing(tmp_path):
    db=Store(tmp_path/'ledger.db'); keys=db.bootstrap(); rpc=ImageRPC()
    app=create_app(Settings(work_dir=str(tmp_path)),rpc,db)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'Authorization':'Bearer '+keys['user1']}
            body={'input':[{'role':'user','content':[{'type':'input_text','text':'compare'},PART,PART]}]}
            direct=(await client.post('/v1/responses',headers=headers,json=body)).json()
            assert direct['usage']['total_tokens']==110
            turn=[p for m,p in rpc.calls if m=='turn/start'][-1]
            assert turn['input'][1:]==[{'type':'image','url':PNG,'detail':'original'}]*2
            rpc.mode='tool'
            tools=[{'type':'function','name':'view_image'}]
            first=(await client.post('/v1/responses',headers=headers,json={'input':'read image','tools':tools})).json()
            result={'type':'function_call_output','call_id':first['output'][0]['call_id'],
                    'output':[{'type':'input_text','text':'local image'},PART]}
            continuation={'input':[result],'tools':tools,'previous_response_id':first['id']}
            stolen=await client.post('/v1/responses',headers={'Authorization':'Bearer '+keys['user2']},json=continuation)
            assert stolen.status_code==404
            answer=(await client.post('/v1/responses',headers=headers,json=continuation)).json()
            assert rpc.received=={'success':True,'contentItems':[{'type':'inputText','text':'local image'},
                                                              {'type':'inputImage','imageUrl':PNG}]}
            assert first['usage']['total_tokens']+answer['usage']['total_tokens']==450
            assert db.usage('user1')['used']==560 and db.usage('user1')['held']==0
            rpc.mode='text'
            response=await client.post('/v1/responses',headers=headers,json={'input':'explain again','tools':tools,'previous_response_id':answer['id']})
            assert response.status_code==200
            history=[p['items'] for m,p in rpc.calls if m=='thread/inject_items'][-1]
            assert next(i for i in history if i['type']=='function_call_output')['output']==result['output']

@pytest.mark.parametrize('url',[PNG.replace('png','jpeg'),'data:image/png;base64,@@@','https://example.com/a.png','file:///etc/passwd','data:image/png;base64,'])
@pytest.mark.parametrize('tool',[False,True])
def test_invalid_images_rejected_in_both_paths(url,tool):
    part={'type':'input_image','image_url':url}
    item={'type':'function_call_output','call_id':'call1','output':[part]} if tool else {'role':'user','content':[part]}
    with pytest.raises(InputError):normalize({'input':[item]},'m')

async def test_large_image_request_and_twenty_mib_limit(tmp_path):
    db=Store(tmp_path/'ledger.db'); keys=db.bootstrap(); rpc=ImageRPC(); app=create_app(Settings(),rpc,db)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'Authorization':'Bearer '+keys['user1']}
            # A transport fixture; real image decoding is covered by the binary probe.
            part={'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'x'*(2*1024*1024)).decode()}
            response=await client.post('/v1/responses',headers=headers,json={'input':[{'role':'user','content':[part]}]})
            assert response.status_code==200 and db.usage('user1')['used']==110
            too_big=await client.post('/v1/responses',headers={**headers,'content-length':str(20*1024*1024+1)},content=b'{}')
            assert too_big.status_code==413
            assert len([c for c in rpc.calls if c[0]=='turn/start'])==1
