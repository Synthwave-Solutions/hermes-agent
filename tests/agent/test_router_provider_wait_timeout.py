"""Exercise native watchdogs for a remote provider behind a loopback router."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from run_agent import AIAgent



@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    attempts=[]
    def refuse(*args,**kwargs):
        attempts.append('http')
        raise AssertionError('Hermetic router test attempted live HTTP')
    async def refuse_async(*args,**kwargs):
        return refuse(*args,**kwargs)
    monkeypatch.setattr(httpx.Client,'send',refuse)
    monkeypatch.setattr(httpx.AsyncClient,'send',refuse_async)
    yield
    assert not attempts, 'A caught exception hid a live HTTP attempt'


def make_agent(monkeypatch, tmp_path, provider='custom:omniroute', requested=None):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path/'config.yaml').write_text('{}\n')
    for key in ('HERMES_STREAM_STALE_TIMEOUT','HERMES_LOCAL_STREAM_STALE_TIMEOUT',
                'HERMES_API_CALL_STALE_TIMEOUT','HERMES_STREAM_READ_TIMEOUT'):
        monkeypatch.delenv(key, raising=False)
    # Construction metadata is unrelated to the request watchdog; no endpoint
    # or model provider may be contacted by this synthetic test.
    monkeypatch.setattr('agent.model_metadata.detect_local_server_type', lambda *a, **k: None)
    monkeypatch.setattr('agent.model_metadata.fetch_endpoint_model_metadata', lambda *a, **k: {})
    monkeypatch.setattr('agent.model_metadata._query_local_context_length', lambda *a, **k: None)
    monkeypatch.setattr('agent.model_metadata._query_ollama_api_show', lambda *a, **k: None)
    agent = AIAgent(model='codex/gpt-6-astra', provider=provider,
        requested_provider=requested, api_key='fixture-only',
        base_url='http://127.0.0.1:20128/v1', api_mode='chat_completions',
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        reasoning_config={'enabled':True,'effort':'high'}, platform='webui')
    return agent


@pytest.mark.parametrize('provider,requested,finite', [
    ('custom:omniroute',None,True), ('custom','custom:omniroute',True),
    ('ollama',None,False), ('custom:local',None,False),
    ('ollama','custom:omniroute',False),
])
def test_nonstream_router_uses_remote_patience_without_changing_native(monkeypatch,tmp_path,provider,requested,finite):
    agent = make_agent(monkeypatch,tmp_path,provider,requested)
    value = agent._compute_non_stream_stale_timeout({'messages':[{'role':'user','content':'synthetic QA'}]})
    assert value == (90 if finite else float('inf'))


def chunk(content):
    return SimpleNamespace(choices=[SimpleNamespace(index=0,
        delta=SimpleNamespace(content=content,tool_calls=None,reasoning_content=None,reasoning=None),
        finish_reason='stop')], model='codex/gpt-6-astra',usage=None)


@pytest.mark.parametrize('provider,setting,expected', [
    ('custom:omniroute','default',180), ('ollama','default',1800),
    ('custom:local','default',1800), ('custom:omniroute','request',75),
    ('custom:omniroute','stale',420), ('custom:omniroute','read_env',42),
])
def test_native_stream_client_preserves_explicit_timeouts(monkeypatch,tmp_path,provider,setting,expected):
    agent=make_agent(monkeypatch,tmp_path,provider)
    if setting in {'request','stale'}:
        field='request_timeout_seconds' if setting=='request' else 'stale_timeout_seconds'
        (tmp_path/'config.yaml').write_text(f'providers:\n  "{provider}":\n    {field}: {expected}\n')
    if setting=='read_env':
        monkeypatch.setenv('HERMES_STREAM_READ_TIMEOUT',str(expected))
    received=[]
    client=MagicMock()
    client.chat.completions.create.return_value=iter([chunk('synthetic success')])
    def create(**kwargs):
        received.append(kwargs)
        return client
    monkeypatch.setattr(agent,'_create_request_openai_client',create)
    monkeypatch.setattr(agent,'_close_request_openai_client',lambda *a, **k:None)
    result=agent._interruptible_streaming_api_call({'model':agent.model,'messages':[{'role':'user','content':'synthetic QA'}]})
    assert result.choices[0].message.content=='synthetic success'
    assert received[0]['api_kwargs']['timeout'].read==expected
    if setting=='stale':
        assert agent._compute_non_stream_stale_timeout({'input':'synthetic QA'})==expected


@pytest.mark.parametrize('model_specific', [False,True])
@pytest.mark.parametrize('canonicalized', [False,True])
def test_named_router_timeout_settings_use_the_configured_provider_entry(monkeypatch,tmp_path,model_specific,canonicalized):
    agent=make_agent(monkeypatch,tmp_path,'custom' if canonicalized else 'custom:omniroute',
                     'custom:omniroute' if canonicalized else None)
    (tmp_path/'config.yaml').write_text('providers:\n  omniroute:\n    stale_timeout_seconds: 420\n    request_timeout_seconds: 510\n'
        + ('    models:\n      codex/gpt-6-astra:\n        stale_timeout_seconds: 600\n        timeout_seconds: 700\n' if model_specific else ''))
    assert agent._compute_non_stream_stale_timeout({'input':'synthetic QA'})==(600 if model_specific else 420)
    assert agent._resolved_api_call_timeout()==(700 if model_specific else 510)



@pytest.mark.parametrize('provider', ['custom', 'openai'])
@pytest.mark.parametrize('setting', ['provider', 'model', 'empty_named', 'partial_named'])
def test_router_preserves_explicit_canonical_timeout_fallback(monkeypatch,tmp_path,provider,setting):
    from agent.chat_completion_helpers import _derive_stream_stale_timeout
    import yaml
    agent=make_agent(monkeypatch,tmp_path,provider,'custom:omniroute')
    entry={'request_timeout_seconds':75,'stale_timeout_seconds':420}
    if setting=='model':
        entry['models']={agent.model:{'timeout_seconds':125,'stale_timeout_seconds':520}}
    config={'providers':{provider:entry}}
    if setting=='empty_named':
        config['providers']['custom:omniroute']={}
    elif setting=='partial_named':
        config['providers']['omniroute']={'request_timeout_seconds':65}
    (tmp_path/'config.yaml').write_text(yaml.safe_dump(config))
    expected_request=65 if setting=='partial_named' else 125 if setting=='model' else 75
    expected_stale=520 if setting=='model' else 420
    assert agent._resolved_api_call_timeout()==expected_request
    assert agent._compute_non_stream_stale_timeout({'input':'synthetic QA'})==expected_stale
    assert agent._stale_timeout_is_explicit()
    assert _derive_stream_stale_timeout(agent,{'input':'synthetic QA'})==expected_stale
    received=[]
    client=MagicMock()
    client.chat.completions.create.return_value=iter([chunk('synthetic success')])
    def create(**kwargs):
        received.append(kwargs)
        return client
    monkeypatch.setattr(agent,'_create_request_openai_client',create)
    monkeypatch.setattr(agent,'_close_request_openai_client',lambda *a, **k:None)
    agent._interruptible_streaming_api_call({'model':agent.model,'messages':[{'role':'user','content':'synthetic QA'}]})
    assert received[0]['api_kwargs']['timeout'].read==expected_request


def test_exact_named_provider_key_takes_precedence_over_alias(monkeypatch,tmp_path):
    agent=make_agent(monkeypatch,tmp_path)
    (tmp_path/'config.yaml').write_text('providers:\n  "custom:omniroute":\n    stale_timeout_seconds: 420\n'
                                     '  omniroute:\n    stale_timeout_seconds: 600\n')
    assert agent._compute_non_stream_stale_timeout({'input':'synthetic QA'})==420



@pytest.mark.parametrize('fallback_provider', ['custom', 'openai'])
def test_init_fallback_uses_current_native_identity(monkeypatch,tmp_path,fallback_provider):
    import run_agent
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    (tmp_path/'config.yaml').write_text('{}\n')
    for key in ('HERMES_API_CALL_STALE_TIMEOUT','HERMES_STREAM_STALE_TIMEOUT','HERMES_LOCAL_STREAM_STALE_TIMEOUT'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr('agent.model_metadata.detect_local_server_type',lambda *a, **k:None)
    monkeypatch.setattr('agent.model_metadata.fetch_endpoint_model_metadata',lambda *a, **k:{})
    monkeypatch.setattr('agent.model_metadata._query_local_context_length',lambda *a, **k:None)
    monkeypatch.setattr('agent.model_metadata._query_ollama_api_show',lambda *a, **k:None)
    monkeypatch.setattr(run_agent,'get_tool_definitions',lambda *a, **k:[])
    monkeypatch.setattr(run_agent,'check_toolset_requirements',lambda *a, **k:{})
    monkeypatch.setattr(run_agent,'OpenAI',MagicMock())
    local_client=SimpleNamespace(api_key='fixture-only',base_url='http://127.0.0.1:11434/v1',
                                _custom_headers=None,default_headers=None,_default_headers=None)
    routed=[]
    def resolve(provider,model=None,**kwargs):
        routed.append(provider)
        return (local_client,'synthetic-native') if provider==fallback_provider else (None,None)
    monkeypatch.setattr('agent.auxiliary_client.resolve_provider_client',resolve)
    agent=AIAgent(provider='custom:omniroute',requested_provider='custom:omniroute',
        model='codex/gpt-6-astra',api_key=None,base_url=None,quiet_mode=True,
        skip_context_files=True,skip_memory=True,
        fallback_model=[{'provider':fallback_provider,'model':'synthetic-native'}])
    assert routed==['custom:omniroute',fallback_provider]
    assert agent._fallback_activated
    assert agent.provider==fallback_provider
    assert agent.requested_provider==fallback_provider
    assert agent._compute_non_stream_stale_timeout({'input':'synthetic QA'})==float('inf')


def test_router_wait_user_cancel_does_not_retry(monkeypatch,tmp_path):
    agent=make_agent(monkeypatch,tmp_path)
    client=MagicMock()
    def create(**kwargs):
        agent._interrupt_requested=True
        return iter([])
    client.chat.completions.create.side_effect=create
    monkeypatch.setattr(agent,'_create_request_openai_client',lambda **kw:client)
    monkeypatch.setattr(agent,'_close_request_openai_client',lambda *a, **k:None)
    monkeypatch.setattr(agent,'_abort_request_openai_client',lambda *a, **k:None)
    with pytest.raises(InterruptedError):
        agent._interruptible_streaming_api_call({'model':agent.model,'messages':[{'role':'user','content':'synthetic QA'}]})
    assert client.chat.completions.create.call_count==1


@pytest.mark.parametrize('whole_turn',[False,True])
def test_no_first_token_router_wait_aborts_then_same_route_can_recover(monkeypatch,tmp_path,whole_turn):
    from agent import chat_completion_helpers as helpers
    agent = make_agent(monkeypatch,tmp_path,'custom','custom:omniroute')
    started = threading.Event()
    aborted = threading.Event()
    finished = threading.Event()
    calls = []
    real_time = time.time
    epoch = real_time()
    # Advance only the watchdog clock after the real request starts. The
    # blocking transport has a short safety ceiling, so unfixed code fails
    # with an assertion instead of waiting for the live 900-second threshold.
    monkeypatch.setattr(helpers,'time',SimpleNamespace(time=lambda:epoch+((400 if whole_turn else 200) if started.is_set() else 0),sleep=time.sleep))
    client = MagicMock()

    def create(**kwargs):
        calls.append(kwargs)
        def stream():
            started.set()
            try:
                if len(calls) == 1:
                    if aborted.wait(1.5):
                        raise httpx.ReadError('synthetic closed transport')
                    yield chunk('late un-aborted result')
                else:
                    yield chunk('recovered')
            finally:
                finished.set()
        return stream()

    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(agent,'_create_request_openai_client',lambda **kw:client)
    monkeypatch.setattr(agent,'_close_request_openai_client',lambda *a, **k:None)
    monkeypatch.setattr(agent,'_abort_request_openai_client',lambda *a, **k:aborted.set())
    payload={'model':agent.model,'messages':[{'role':'user','content':'synthetic QA'}], 'reasoning_effort':'high'}
    try:
        if whole_turn:
            result=agent.run_conversation('synthetic QA')
            assert result['final_response']=='recovered'
            assert len(calls)>=2, 'native conversation loop did not recover automatically'
        else:
            try:
                agent._interruptible_streaming_api_call(payload)
            except httpx.ReadError:
                pass
        assert aborted.is_set(), 'loopback router wrongly inherited native inference patience'
        assert finished.wait(2), 'aborted request did not finish'
        if not whole_turn:
            response=agent._interruptible_streaming_api_call(payload)
            assert response.choices[0].message.content == 'recovered'
        def effort(call):
            return call.get('reasoning_effort') or (call.get('extra_body') or {}).get('reasoning',{}).get('effort')
        assert calls and all(c['model']=='codex/gpt-6-astra' and effort(c)=='high' for c in calls)
        assert agent.provider=='custom' and agent.requested_provider=='custom:omniroute'
    finally:
        aborted.set()
