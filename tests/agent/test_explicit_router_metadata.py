"""Native metadata paths keep live model windows without probing router hardware."""
import json
import httpx
import pytest
import requests
from agent import model_metadata as metadata
from run_agent import AIAgent
MODEL='codex/gpt-6-astra'
URL='http://127.0.0.1:20128/v1'
NATIVE={'/api/v1/models','/api/tags','/v1/props','/props','/version','/api/show'}

@pytest.fixture
def endpoint(monkeypatch,tmp_path):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    (tmp_path/'config.yaml').write_text('{}\n')
    for name in ('_endpoint_probe_path_cache','_endpoint_blackhole_cache','_LOCAL_CTX_PROBE_CACHE',
                 '_lmstudio_probe_misses','_ollama_probe_misses','_endpoint_model_metadata_cache',
                 '_endpoint_model_metadata_cache_time','_endpoint_model_metadata_inflight'):
        monkeypatch.setattr(metadata,name,{})
    metadata.save_context_length(MODEL,URL,65536)
    state={'paths':[],'detail_status':200,'window':98304}
    def response(request):
        path=request.url.path
        assert request.headers.get('Authorization')=='Bearer fixture-only'
        state['paths'].append(path)
        if path in NATIVE:return httpx.Response(404,json={})
        if path=='/v1/models/'+MODEL:return httpx.Response(state['detail_status'],json={'id':MODEL,'context_length':state['window']})
        if path=='/v1/models':return httpx.Response(200,json={'data':[{'id':MODEL,'context_length':state['window']}]})
        raise AssertionError('Unexpected fixture path')
    client,async_client=httpx.Client,httpx.AsyncClient
    class FixtureClient(client):
        def __init__(self,*a,**kw):
            kw['transport']=httpx.MockTransport(response)
            super().__init__(*a,**kw)
    class FixtureAsyncClient(async_client):
        def __init__(self,*a,**kw):
            kw['transport']=httpx.MockTransport(response)
            super().__init__(*a,**kw)
    monkeypatch.setattr(httpx,'Client',FixtureClient)
    monkeypatch.setattr(httpx,'AsyncClient',FixtureAsyncClient)
    def requests_send(self,request,**kwargs):
        path=httpx.URL(request.url).path;state['paths'].append(path)
        assert path in {'/v1/models','/models'}
        assert request.headers.get('Authorization')=='Bearer fixture-only'
        result=requests.Response();result.status_code=200
        result._content=json.dumps({'data':[{'id':MODEL,'context_length':state['window']}]}).encode()
        result.url=request.url;return result
    monkeypatch.setattr(requests.Session,'send',requests_send)
    monkeypatch.setattr('run_agent.get_tool_definitions',lambda *a,**k:[])
    monkeypatch.setattr('run_agent.check_toolset_requirements',lambda *a,**k:{})
    yield state

@pytest.mark.parametrize('provider,requested',[('custom','custom:omniroute'),('openai','custom:omniroute'),('custom:omniroute',None)])
def test_native_constructor_router_keeps_live_window_without_hardware_probes(endpoint,provider,requested):
    agent=AIAgent(model=MODEL,provider=provider,requested_provider=requested,api_key='fixture-only',
        base_url=URL,api_mode='chat_completions',quiet_mode=True,skip_context_files=True,
        skip_memory=True,reasoning_config={'enabled':True,'effort':'high'},platform='webui')
    try:
        assert agent.context_compressor.context_length==98304
        assert '/v1/models/'+MODEL in endpoint['paths']
        assert not NATIVE.intersection(endpoint['paths'])
        assert agent._ollama_num_ctx is None
        assert agent.model==MODEL and agent.reasoning_config['effort']=='high'
    finally:agent.close()

@pytest.mark.parametrize('provider,requested',[('custom',''),('custom:local',''),('ollama','custom:omniroute')])
def test_unknown_or_native_provider_preserves_detection(endpoint,provider,requested):
    value=metadata.get_model_context_length(MODEL,URL,'fixture-only',provider=provider,requested_provider=requested)
    assert value==98304 and NATIVE.intersection(endpoint['paths'])

@pytest.mark.parametrize('window',[98304,32768])
def test_router_reconciles_changed_and_subminimum_model_windows(endpoint,window):
    endpoint['window']=window
    value=metadata.get_model_context_length(MODEL,URL,'fixture-only',provider='custom',requested_provider='custom:omniroute')
    assert value==window and not NATIVE.intersection(endpoint['paths'])

def test_router_detail_miss_preserves_models_list_fallback(endpoint):
    endpoint['detail_status']=404
    value=metadata.get_model_context_length(MODEL,URL,'fixture-only',provider='custom',requested_provider='custom:omniroute')
    assert value==98304 and endpoint['paths']==['/v1/models/'+MODEL,'/v1/models']

def test_explicit_context_still_wins_without_endpoint_read(endpoint):
    value=metadata.get_model_context_length(MODEL,URL,'fixture-only',config_context_length=196608,provider='custom',requested_provider='custom:omniroute')
    assert value==196608 and endpoint['paths']==[]

def test_router_uncached_catalog_uses_standard_models_without_native_probe(endpoint,monkeypatch):
    monkeypatch.setattr(metadata,'get_cached_context_length',lambda *a,**k:None)
    value=metadata.get_model_context_length(MODEL,URL,'fixture-only',provider='custom',requested_provider='custom:omniroute')
    assert value==98304 and endpoint['paths']==['/v1/models']

def test_native_and_router_probe_cache_modes_do_not_mask_each_other(endpoint):
    assert metadata.get_model_context_length(MODEL,URL,'fixture-only',provider='custom',requested_provider='custom:omniroute')==98304
    endpoint['paths'].clear()
    assert metadata.get_model_context_length(MODEL,URL,'fixture-only',provider='custom')==98304
    assert NATIVE.intersection(endpoint['paths'])

def test_endpoint_catalog_cache_modes_do_not_mask_each_other(endpoint):
    assert metadata.fetch_endpoint_model_metadata(URL,'fixture-only',native_protocol_probes=False)[MODEL]['context_length']==98304
    assert endpoint['paths']==['/v1/models']
    endpoint['paths'].clear()
    assert metadata.fetch_endpoint_model_metadata(URL,'fixture-only')[MODEL]['context_length']==98304
    assert '/api/v1/models' in endpoint['paths']

def test_compressor_update_drops_previous_router_identity(endpoint):
    from agent.context_compressor import ContextCompressor
    compressor=ContextCompressor(MODEL,base_url=URL,api_key='fixture-only',provider='custom',
                                 metadata_requested_provider='custom:omniroute',quiet_mode=True)
    assert compressor.context_length==98304
    compressor.update_model(MODEL,131072,base_url=URL,api_key='fixture-only',provider='ollama')
    assert compressor.metadata_requested_provider=='' and compressor.context_length==131072

def test_explicit_ollama_allocation_override_is_preserved_on_router(endpoint,tmp_path):
    (tmp_path/'config.yaml').write_text('model:\n  context_length: 196608\n  ollama_num_ctx: 32768\n')
    agent=AIAgent(model=MODEL,provider='custom',requested_provider='custom:omniroute',api_key='fixture-only',
        base_url=URL,api_mode='chat_completions',quiet_mode=True,skip_context_files=True,skip_memory=True)
    try:
        assert agent._ollama_num_ctx==32768 and agent.context_compressor.context_length==32768
        assert endpoint['paths']==[]
    finally:agent.close()
