"""Responses contracts. Preserve roles; never flatten tool results into user text."""
import copy
import base64
import binascii
import json
import re
import uuid

class InputError(Exception):pass

def uid(prefix):return prefix+'_'+uuid.uuid4().hex

def validate_image(part):
    url=part.get('image_url','')
    match=re.match(r'^data:image/(png|jpeg|webp);base64,',url) if isinstance(url,str) else None
    if not match:raise InputError('Only inline PNG/JPEG/WebP images are accepted; remote/local file URLs are disabled')
    try:data=base64.b64decode(url[match.end():],validate=True)
    except (ValueError,binascii.Error):raise InputError('Invalid image Base64')
    kind=match[1]
    valid=(data.startswith(b'\x89PNG\r\n\x1a\n') if kind=='png' else
           data.startswith(b'\xff\xd8\xff') if kind=='jpeg' else
           data.startswith(b'RIFF') and data[8:12]==b'WEBP')
    if not valid:raise InputError('Image data does not match its MIME type')
    if part.get('detail') is not None and part['detail'] not in ('auto','low','high','original'):
        raise InputError('Invalid image detail')

def tool_content(output):
    if isinstance(output,str):return [{'type':'inputText','text':output}]
    return [{'type':'inputImage','imageUrl':p['image_url']} if p['type']=='input_image'
            else {'type':'inputText','text':p['text']} for p in output]

def reservation_size(request):
    """Admission estimate, never a charge or a model-specific token count.

    Text retains the conservative UTF-8 byte estimate. Images use a 65,536-token
    allowance instead of counting encoded file bytes: documented patch inputs
    permit up to 30,000 patches before model multipliers. This deliberately broad
    allowance is not an upstream guarantee; checkpoints enforce actual usage.
    https://developers.openai.com/api/docs/guides/images-vision#calculating-costs
    """
    items=copy.deepcopy(request['items']); images=0
    for item in items:
        parts=item.get('content',[]) if item.get('type')=='message' else item.get('output',[])
        if not isinstance(parts,list):continue
        for part in parts:
            if isinstance(part,dict) and part.get('type')=='input_image':
                part['image_url']=''; images+=1
    return (len(json.dumps([items,request['dynamic']],ensure_ascii=False).encode())+
        len(request['instructions'].encode())+images*65536)

def normalize(body,default_model):
    if not isinstance(body,dict):raise InputError('JSON body must be an object')
    allowed={'model','input','instructions','stream','tools','tool_choice','parallel_tool_calls','reasoning',
        'text','store','previous_response_id','include','metadata','service_tier','prompt_cache_key',
        'prompt_cache_retention','safety_identifier','user','stream_options','max_output_tokens','client_metadata'}
    unknown=set(body)-allowed
    if unknown:raise InputError('Unsupported fields: '+', '.join(sorted(unknown)))
    if body.get('max_output_tokens') is not None:raise InputError('app-server cannot enforce max_output_tokens; use relay budgets')
    model=body.get('model') or default_model
    if not isinstance(model,str) or len(model)>120:raise InputError('Invalid model')
    if not isinstance(body.get('stream',False),bool):raise InputError('stream must be boolean')
    parallel=body.get('parallel_tool_calls')
    if parallel is not None and not isinstance(parallel,bool):raise InputError('parallel_tool_calls must be boolean')
    instructions=body.get('instructions') or ''
    if not isinstance(instructions,str):raise InputError('instructions must be a string')
    inp=body.get('input')
    if isinstance(inp,str):inp=[{'type':'message','role':'user','content':[{'type':'input_text','text':inp}]}]
    if not isinstance(inp,list) or not inp:raise InputError('input must be a nonempty string or array')
    # Current Codex Responses Lite puts the request's tool declarations in a
    # developer prefix instead of the top-level tools field. Feed them through
    # the same client-tool mapping; never expose a second set of raw tools.
    tools=body.get('tools') or []
    prefixes=[(i,item) for i,item in enumerate(inp) if isinstance(item,dict) and item.get('type')=='additional_tools']
    if prefixes:
        index,prefix=prefixes[0]
        if len(prefixes)!=1 or index!=0 or prefix.get('role')!='developer':
            raise InputError('additional_tools must be one developer prefix')
        declared=prefix.get('tools')
        if not isinstance(declared,list):raise InputError('additional_tools.tools must be an array')
        if tools:raise InputError('Conflicting tool declarations')
        tools=declared; inp=inp[1:]
        if not inp:raise InputError('input requires content after additional_tools')
    items=[]
    for entry in inp:
        if not isinstance(entry,dict):raise InputError('Invalid input item')
        item=copy.deepcopy(entry); typ=item.get('type','message')
        if typ=='message':
            role=item.get('role')
            if role not in ('system','developer','user','assistant'):raise InputError('Invalid message role')
            c=item.get('content')
            if isinstance(c,str):c=[{'type':'output_text' if role=='assistant' else 'input_text','text':c}]
            if not isinstance(c,list):raise InputError('Invalid message content')
            for part in c:
                if not isinstance(part,dict):raise InputError('Invalid content part')
                if part.get('type') in ('input_text','output_text'):
                    if not isinstance(part.get('text'),str):raise InputError('text must be string')
                elif part.get('type')=='input_image':
                    validate_image(part)
                else:raise InputError('Unsupported content type: '+str(part.get('type')))
            item={'type':'message','role':role,'content':c,**({'phase':item['phase']} if item.get('phase') else {})}
        elif typ in ('function_call','custom_tool_call'):
            if not isinstance(item.get('call_id'),str) or not isinstance(item.get('name'),str):raise InputError('Invalid tool call')
        elif typ in ('function_call_output','custom_tool_call_output'):
            if not isinstance(item.get('call_id'),str):raise InputError('Tool output requires call_id')
            output=item.get('output')
            if isinstance(output,list):
                for part in output:
                    if not isinstance(part,dict):raise InputError('Invalid tool content')
                    if part.get('type')=='input_image':validate_image(part)
                    elif part.get('type')=='input_text' and isinstance(part.get('text'),str):pass
                    else:raise InputError('Tool content must be input_text or input_image')
            elif not isinstance(output,str):raise InputError('Tool output must be a string or content array')
        elif typ=='reasoning':
            # Official ResponseItem history is retained by thread/inject_items.
            if not isinstance(item.get('summary',[]),list):raise InputError('Invalid reasoning item')
        else:raise InputError('Unsupported input item: '+str(typ))
        items.append(item)
    if not isinstance(tools,list):raise InputError('tools must be an array')
    dynamic=[]; names={}; seen=set()
    def add(tool,namespace=None):
        if not isinstance(tool,dict) or tool.get('type') not in ('function','custom'):raise InputError('Only client function/custom tools are supported')
        name=tool.get('name')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name):raise InputError('Invalid tool name')
        if (namespace,name) in seen:raise InputError('Duplicate tool name')
        seen.add((namespace,name)); internal='relay_tool_'+str(len(dynamic))
        custom=tool['type']=='custom'
        schema={'type':'object','properties':{'input':{'type':'string'}},'required':['input'],'additionalProperties':False} if custom else tool.get('parameters',{'type':'object','properties':{}})
        if not isinstance(schema,dict):raise InputError('Tool parameters must be JSON Schema')
        desc=tool.get('description') or ''
        if not isinstance(desc,str):raise InputError('Tool description must be a string')
        client_name=(namespace+'.' if namespace else '')+name
        desc='Client tool '+client_name+' is available through '+internal+'.\n'+desc
        if custom and tool.get('format'):desc+='\nClient input format: '+json.dumps(tool['format'],ensure_ascii=False)
        dynamic.append({'type':'function','name':internal,'description':desc,'inputSchema':schema})
        names[internal]={'name':name,'namespace':namespace,'custom':custom}
    for tool in tools:
        if isinstance(tool,dict) and tool.get('type')=='namespace':
            namespace=tool.get('name')
            if not isinstance(namespace,str):raise InputError('Invalid namespace')
            if not isinstance(tool.get('tools',[]),list):raise InputError('Namespace tools must be an array')
            for child in tool.get('tools',[]):add(child,namespace)
        else:add(tool)
    choice=body.get('tool_choice','auto')
    if choice not in ('auto','none'):raise InputError('Only auto/none tool_choice is supported')
    if choice=='none':dynamic=[]; names={}
    # Codex forces this flag false in Responses Lite even for parallel tools;
    # this fixed transport field is not a user-selected serial-tool setting.
    if parallel is False and dynamic and not prefixes:
        raise InputError('parallel_tool_calls=false with top-level tools is not supported by app-server')
    reasoning=body.get('reasoning') or {}
    if not isinstance(reasoning,dict):raise InputError('Invalid reasoning')
    effort=reasoning.get('effort')
    if effort is not None and effort not in ('none','minimal','low','medium','high','xhigh','max','ultra'):raise InputError('Invalid reasoning effort')
    if reasoning.get('summary') is not None and reasoning['summary'] not in ('auto','concise','detailed','none'):raise InputError('Invalid reasoning summary')
    text=body.get('text') or {}
    if not isinstance(text,dict):raise InputError('Invalid text settings')
    if text.get('verbosity') is not None and text['verbosity'] not in ('low','medium','high'):raise InputError('Invalid text verbosity')
    fmt=text.get('format',{})
    if not isinstance(fmt,dict):raise InputError('Invalid text format')
    if fmt.get('type','text') not in ('text','json_schema'):raise InputError('Only text/json_schema output is supported')
    if fmt.get('type')=='json_schema' and not isinstance(fmt.get('schema'),dict):raise InputError('Output schema required')
    if body.get('previous_response_id') is not None and not isinstance(body['previous_response_id'],str):raise InputError('Invalid previous_response_id')
    if body.get('metadata') is not None and not isinstance(body['metadata'],dict):raise InputError('metadata must be an object')
    # Official clients send transport metadata. It is not an identity or history source.
    if body.get('client_metadata') is not None and not isinstance(body['client_metadata'],dict):raise InputError('client_metadata must be an object')
    return {'model':model,'items':items,'instructions':instructions,'dynamic':dynamic,'names':names,
      'tools':tools,'effort':effort,'summary':reasoning.get('summary'),'verbosity':text.get('verbosity'),
      'schema':fmt.get('schema'),'previous':body.get('previous_response_id'),
      'stream':body.get('stream',False),'metadata':body.get('metadata') or {},'service_tier':body.get('service_tier')}

def usage_delta(total,previous):
    if total is None:return None
    inp=max(0,total.get('inputTokens',0)-previous.get('inputTokens',0))
    out=max(0,total.get('outputTokens',0)-previous.get('outputTokens',0))
    cached=max(0,total.get('cachedInputTokens',0)-previous.get('cachedInputTokens',0))
    reasoning=max(0,total.get('reasoningOutputTokens',0)-previous.get('reasoningOutputTokens',0))
    return {'input_tokens':inp,'output_tokens':out,'total_tokens':inp+out,
       'input_tokens_details':{'cached_tokens':min(inp,cached)},'output_tokens_details':{'reasoning_tokens':reasoning}}

def sse(event):
    return ('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode()
