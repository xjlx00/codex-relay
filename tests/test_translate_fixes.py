import base64,copy
import pytest
from cr.translate import normalize,InputError,reservation_size


def test_image_admission_ignores_encoding_size_but_counts_each_image():
    def request(size):
        image={'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'x'*size).decode()}
        return normalize({'input':[{'role':'user','content':[image]}]},'gpt-6-astra')
    small=request(50); large=request(2*1024*1024); before=copy.deepcopy(large)
    assert reservation_size(small)==reservation_size(large)
    assert 65536<reservation_size(large)<66000 and large==before
    large['items'].append({'type':'function_call_output','call_id':'call','output':large['items'][0]['content']})
    assert reservation_size(large)>2*65536


def test_parallel_false_without_tools_is_supported_for_real_client_compaction():
    assert normalize({'input':'summarize','parallel_tool_calls':False},'m')['dynamic']==[]
    assert normalize({'input':'summarize','tools':[{'type':'function','name':'f'}],
        'tool_choice':'none','parallel_tool_calls':False},'m')['dynamic']==[]
    with pytest.raises(InputError,match='parallel_tool_calls=false'):
        normalize({'input':'tools','tools':[{'type':'function','name':'f'}],'parallel_tool_calls':False},'m')


@pytest.mark.parametrize('fields',[
    {'parallel_tool_calls':1},{'parallel_tool_calls':'false'},
    {'reasoning':{'summary':'invalid'}},{'text':{'verbosity':'invalid'}}])
def test_generation_parameters_reject_unenforceable_values(fields):
    with pytest.raises(InputError):normalize({'input':'x',**fields},'m')


def test_client_identity_metadata_is_not_an_upstream_instruction():
    result=normalize({'input':'TASK','user':'PRIVATE_USER','safety_identifier':'PRIVATE_SAFETY',
        'client_metadata':{'marker':'PRIVATE_CLIENT'},'metadata':{'local':'VISIBLE_TO_CLIENT'},
        'prompt_cache_key':'PRIVATE_CACHE','text':{'verbosity':'high'},'reasoning':{'summary':'detailed'}},'m')
    assert result['verbosity']=='high' and result['summary']=='detailed'
    assert 'PRIVATE' not in str(result)


def test_responses_lite_prefix_uses_normal_tool_mapping_and_preserves_developer():
    tools=[{'type':'function','name':'echo','parameters':{'type':'object','properties':{}}}]
    content=[{'role':'developer','content':'Instructions'},{'role':'user','content':'Task'}]
    lite=normalize({'input':[{'type':'additional_tools','role':'developer','tools':tools}]+content,
        'parallel_tool_calls':False},'m')
    legacy=normalize({'input':content,'tools':tools},'m')
    assert lite==legacy and lite['dynamic'][0]['name']=='relay_tool_0'
    assert [item['role'] for item in lite['items']]==['developer','user']


@pytest.mark.parametrize('prefix',[
    {'role':'user','tools':[]},{'role':'developer','tools':'invalid'},
    {'role':'developer','tools':[{'type':'web_search'}]}])
def test_responses_lite_prefix_keeps_tool_security_boundary(prefix):
    with pytest.raises(InputError):normalize({'input':[{'type':'additional_tools',**prefix},{'role':'user','content':'x'}]},'m')


def test_responses_lite_rejects_ambiguous_or_conflicting_tool_sources():
    prefix={'type':'additional_tools','role':'developer','tools':[]};message={'role':'user','content':'x'}
    for items in ([message,prefix],[prefix,prefix,message],[prefix]):
        with pytest.raises(InputError):normalize({'input':items},'m')
    with pytest.raises(InputError):normalize({'input':[prefix,message],'tools':[{'type':'function','name':'f'}]},'m')
