"""Routing may pick a tier without overriding explicit reasoning or profile policy."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

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
