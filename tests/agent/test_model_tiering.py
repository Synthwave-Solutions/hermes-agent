"""Routing may pick a tier without overriding explicit reasoning or profile policy."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
import agent.conversation_loop as loop
import agent.turn_api_request as request_module
from agent.turn_api_request import build_api_request

from agent.model_tiering import apply_model_tiering, configure_model_tiering


@pytest.fixture(autouse=True)
def tiering_env(monkeypatch):
    monkeypatch.delenv('HERMES_MODEL_TIERING', raising=False)


def request():
    return {'model': 'synthwave-auto', 'extra_body': {'reasoning': {'enabled': True, 'effort': 'medium'}}}


@pytest.mark.parametrize('value', [False, 'false', 0, 'off'])
def test_profile_switch_disables_all_tiering(value):
    agent = SimpleNamespace()
    configure_model_tiering(agent, {'model': {'tiering': value}})
    kwargs = request()
    before = deepcopy(kwargs)
    apply_model_tiering(agent, kwargs, [{'role': 'user', 'content': 'hoi'}])
    assert kwargs == before


def test_profile_switch_is_scoped_and_snapshotted():
    disabled, enabled = SimpleNamespace(), SimpleNamespace()
    config = {'model': {'tiering': False}}
    configure_model_tiering(disabled, config)
    configure_model_tiering(enabled, {})
    config['model']['tiering'] = True
    for agent, expected in [(disabled, 'synthwave-auto'), (enabled, 'synthwave-luna'), (disabled, 'synthwave-auto')]:
        kwargs = request()
        apply_model_tiering(agent, kwargs, [{'role': 'user', 'content': 'hoi'}])
        assert kwargs['model'] == expected


@pytest.mark.parametrize('reasoning', [{'enabled': False}, {'enabled': True, 'effort': 'high'}, {'effort': 'low'}])
def test_explicit_reasoning_survives_automatic_tier(reasoning):
    agent = SimpleNamespace(reasoning_config=reasoning)
    kwargs = request()
    kwargs['extra_body']['reasoning'] = deepcopy(reasoning)
    apply_model_tiering(agent, kwargs, [{'role': 'user', 'content': 'hoi'}])
    assert kwargs['model'] == 'synthwave-luna'
    assert kwargs['extra_body']['reasoning'] == reasoning


@pytest.mark.parametrize('state', [
    {'_reasoning_effort_rejected': True},
    {'_reasoning_disable_rejected': True},
    {'_wire_reasoning_config': {'enabled': False}},
    {'request_overrides': {'reasoning_effort': 'low'}},
    {'request_overrides': {'extra_body': {'reasoning': {'effort': 'high'}}}},
    {'request_overrides': {'extra_body': {'enable_thinking': False}}},
])
def test_provider_recovery_and_overrides_are_preserved(state):
    kwargs = {'model': 'synthwave-auto'}
    apply_model_tiering(SimpleNamespace(**state), kwargs, [{'role': 'user', 'content': 'hoi'}])
    assert kwargs == {'model': 'synthwave-luna'}


def test_automatic_effort_and_prompt_stability():
    messages = [{'role': 'system', 'content': 'Stable prefix'}, {'role': 'user', 'content': 'hoi'}]
    before = deepcopy(messages)
    agent = SimpleNamespace(reasoning_config=None, _wire_reasoning_config={'enabled': True, 'effort': 'medium'})
    kwargs = request()
    apply_model_tiering(agent, kwargs, messages)
    assert kwargs['extra_body']['reasoning']['effort'] == 'low'
    assert messages == before
    assert agent.reasoning_config is None


def test_environment_kill_switch_and_explicit_model(monkeypatch):
    for model, env in [('synthwave-auto', '0'), ('chosen-model', '1')]:
        monkeypatch.setenv('HERMES_MODEL_TIERING', env)
        kwargs = {'model': model}
        apply_model_tiering(SimpleNamespace(), kwargs, [{'role': 'user', 'content': 'hoi'}])
        assert kwargs == {'model': model}


def test_clean_request_ignores_injected_cues_but_prices_full_context():
    from agent.model_tiering import tiering_request_user
    wire = [{'role': 'user', 'content': '[Workspace::v1: /production/architecture]\n' + 'attached context ' * 40 + '\nhoi'}]
    before = deepcopy(wire)
    agent = SimpleNamespace(_persist_user_message_idx=0, reasoning_config=None)
    with tiering_request_user(agent, wire, 'hoi'):
        kwargs = request()
        apply_model_tiering(agent, kwargs, wire)
        assert kwargs['model'] == 'synthwave-luna'
        assert kwargs['extra_body']['reasoning']['effort'] == 'low'
        large = [{'role': 'system', 'content': 'x' * 600_004}, *wire]
        kwargs = request()
        apply_model_tiering(agent, kwargs, large)
        assert kwargs['model'] == 'synthwave-sol'
    assert wire == before
    kwargs = request()
    apply_model_tiering(agent, kwargs, wire)
    assert kwargs['model'] == 'synthwave-sol'  # no clean provenance outside this request


def test_newer_steer_and_tool_failures_still_escalate():
    from agent.model_tiering import tiering_request_user
    agent = SimpleNamespace(_persist_user_message_idx=0, reasoning_config=None)
    messages = [{'role': 'user', 'content': 'hoi'}, {'role': 'user', 'content': 'security review'}]
    with tiering_request_user(agent, messages, 'hoi'):
        kwargs = request()
        apply_model_tiering(agent, kwargs, messages)
        assert kwargs['extra_body']['reasoning']['effort'] == 'high'
    messages = [messages[0], {'role': 'tool', 'content': 'error'}, {'role': 'tool', 'content': 'failed'}]
    with tiering_request_user(agent, messages, 'hoi'):
        kwargs = request()
        apply_model_tiering(agent, kwargs, messages)
        assert kwargs['extra_body']['reasoning']['effort'] == 'high'


def test_nested_requests_and_exceptions_do_not_leak_clean_input():
    from agent.model_tiering import classify_turn, tiering_request_user
    agent = SimpleNamespace(_persist_user_message_idx=0)
    messages = [{'role': 'user', 'content': 'architecture'}]
    with tiering_request_user(agent, messages, [{'type': 'text', 'text': 'hoi'}]):
        assert classify_turn(messages)[0] == 'luna'
        with pytest.raises(ValueError):
            with tiering_request_user(agent, messages, 'architecture'):
                assert classify_turn(messages)[0] == 'sol'
                raise ValueError('builder failed')
        assert classify_turn(messages)[0] == 'luna'
    assert classify_turn(messages)[0] == 'sol'


def test_request_builder_binds_clean_user_for_real_tiering(monkeypatch):
    monkeypatch.setattr(loop, '_redecorate_prompt_cache_for_provider',
                        lambda agent, messages, **kw: (messages, None, kw['tools_for_api']))
    monkeypatch.setattr(request_module, 'observe_request_tools', lambda *args: None)
    monkeypatch.setattr(request_module, '_fire_pre_api_request_hook', lambda *args, **kw: None)
    agent = SimpleNamespace(
        _persist_user_message_idx=0, reasoning_config=None, tools=[],
        _reset_stream_delivery_tracking=lambda: None,
        _reapply_reasoning_echo_for_provider=lambda messages: None,
        api_mode='chat_completions', _empty_content_retries=0,
        _image_rejecting_models=set(), _force_ascii_payload=False,
        _is_user_initiated_turn=False, session_id='test', platform='webui',
        model='synthwave-auto', provider='custom:omniroute', base_url='http://invalid',
    )
    def build(messages, **kw):
        result = request()
        apply_model_tiering(agent, result, messages)
        return result
    agent._build_api_kwargs = build
    wire = [{'role': 'user', 'content': '[Workspace::v1: /production/architecture]\nhoi'}]
    result = build_api_request(
        agent, api_messages=wire, _moa_prepared_request=None, tools_for_api=[],
        system_message='', messages=wire, original_user_message='hoi', approx_tokens=20,
        total_chars=80, retry_count=0, api_call_count=0, api_request_id='request',
        api_start_time=0, effective_task_id='task', turn_id='turn',
    )
    assert result.api_kwargs['model'] == 'synthwave-luna'
    assert build(wire)['model'] == 'synthwave-sol'
