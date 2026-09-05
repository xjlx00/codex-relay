"""Responses contracts. Preserve roles; never flatten tool results into user text."""
import copy
import json
import re
import uuid

class InputError(Exception):pass

def uid(prefix):return prefix+'_'+uuid.uuid4().hex

def normalize(body,default_model):
    if not isinstance(body,dict):raise InputError('JSON body must be an object')
    allowed={'model','input','instructions','stream','tools','tool_choice','parallel_tool_calls','reasoning',
        'text','store','previous_response_id','include','metadata','service_tier','prompt_cache_key',
        'prompt_cache_retention','safety_identifier','user','stream_options','max_output_tokens'}
    unknown=set(body)-allowed
    if unknown:raise InputError('Unsupported fields: '+', '.join(sorted(unknown)))
    if body.get('max_output_tokens') is not None:raise InputError('app-server cannot enforce max_output_tokens; use relay budgets')
    model=body.get('model') or default_model
    if not isinstance(model,str) or len(model)>120:raise InputError('Invalid model')
    if not isinstance(body.get('stream',False),bool):raise InputError('stream must be boolean')
    instructions=body.get('instructions') or ''
    if not isinstance(instructions,str):raise InputError('instructions must be a string')
    inp=body.get('input')
    if isinstance(inp,str):inp=[{'type':'message','role':'user','content':[{'type':'input_text','text':inp}]}]
    if not isinstance(inp,list) or not inp:raise InputError('input must be a nonempty string or array')
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
                    url=part.get('image_url','')
                    if not isinstance(url,str) or not re.match(r'^data:image/(png|jpeg|webp);base64,',url):
                        raise InputError('Only inline PNG/JPEG/WebP images are accepted; remote/local file URLs are disabled')
                else:raise InputError('Unsupported content type: '+str(part.get('type')))
            item={'type':'message','role':role,'content':c,**({'phase':item['phase']} if item.get('phase') else {})}
        elif typ in ('function_call','custom_tool_call'):
            if not isinstance(item.get('call_id'),str) or not isinstance(item.get('name'),str):raise InputError('Invalid tool call')
        elif typ in ('function_call_output','custom_tool_call_output'):
            if not isinstance(item.get('call_id'),str):raise InputError('Tool output requires call_id')
            if not isinstance(item.get('output'),str):raise InputError('Tool output must be a string')
        elif typ=='reasoning':
            # Official ResponseItem history is retained by thread/inject_items.
            if not isinstance(item.get('summary',[]),list):raise InputError('Invalid reasoning item')
        else:raise InputError('Unsupported input item: '+str(typ))
        items.append(item)
    tools=body.get('tools') or []
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
    reasoning=body.get('reasoning') or {}
    if not isinstance(reasoning,dict):raise InputError('Invalid reasoning')
    effort=reasoning.get('effort')
    if effort is not None and effort not in ('none','minimal','low','medium','high','xhigh','max','ultra'):raise InputError('Invalid reasoning effort')
    text=body.get('text') or {}
    if not isinstance(text,dict):raise InputError('Invalid text settings')
    fmt=text.get('format',{})
    if not isinstance(fmt,dict):raise InputError('Invalid text format')
    if fmt.get('type','text') not in ('text','json_schema'):raise InputError('Only text/json_schema output is supported')
    if fmt.get('type')=='json_schema' and not isinstance(fmt.get('schema'),dict):raise InputError('Output schema required')
    if body.get('previous_response_id') is not None and not isinstance(body['previous_response_id'],str):raise InputError('Invalid previous_response_id')
    if body.get('metadata') is not None and not isinstance(body['metadata'],dict):raise InputError('metadata must be an object')
    return {'model':model,'items':items,'instructions':instructions,'dynamic':dynamic,'names':names,
      'tools':tools,'effort':effort,'summary':reasoning.get('summary'),
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
