"""Real detached forks retain current route watchdogs; no live config or HTTP."""
from unittest.mock import patch
import httpx
import pytest
from agent import background_review as review
from agent.chat_completion_helpers import _derive_stream_stale_timeout
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def hermetic_runtime(monkeypatch, tmp_path):
    attempts = []
    def refuse(*args, **kwargs):
        attempts.append('http')
        raise AssertionError('No live HTTP permitted in detached fork fixture')
    async def refuse_async(*args, **kwargs):
        return refuse(*args, **kwargs)
    monkeypatch.setattr(httpx.Client, 'send', refuse)
    monkeypatch.setattr(httpx.AsyncClient, 'send', refuse_async)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('{}\n')
    for name in ('HERMES_STREAM_STALE_TIMEOUT', 'HERMES_LOCAL_STREAM_STALE_TIMEOUT',
                 'HERMES_API_CALL_STALE_TIMEOUT', 'HERMES_STREAM_READ_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr('agent.model_metadata.detect_local_server_type', lambda *a, **k: None)
    monkeypatch.setattr('agent.model_metadata.fetch_endpoint_model_metadata', lambda *a, **k: {})
    monkeypatch.setattr('agent.model_metadata._query_local_context_length', lambda *a, **k: None)
    monkeypatch.setattr('agent.model_metadata._query_ollama_api_show', lambda *a, **k: None)
    monkeypatch.setattr('run_agent.get_tool_definitions', lambda *a, **k: [])
    monkeypatch.setattr('run_agent.check_toolset_requirements', lambda *a, **k: {})
    yield
    assert attempts == [], 'An internally caught failure hid an HTTP attempt'


@pytest.fixture
def parent_factory():
    created = []
    def make(provider='custom', requested='custom:omniroute', model='codex/gpt-6-astra'):
        agent = AIAgent(model=model, provider=provider, requested_provider=requested,
            api_key='fixture-parent-only', base_url='http://127.0.0.1:20128/v1',
            api_mode='chat_completions', quiet_mode=True, skip_context_files=True,
            skip_memory=True, reasoning_config={'enabled': True, 'effort': 'high'},
            prefill_messages=[{'role': 'user', 'content': 'synthetic prefix'}],
            ephemeral_system_prompt='synthetic session context', platform='webui')
        agent._cached_system_prompt = 'synthetic stable system bytes'
        created.append(agent)
        return agent
    yield make
    for agent in created:
        agent.close()


def assert_detached_parity(parent, child):
    assert child._cached_system_prompt == parent._cached_system_prompt
    assert child.ephemeral_system_prompt == parent.ephemeral_system_prompt
    assert child.reasoning_config == parent.reasoning_config
    assert child.prefill_messages == parent.prefill_messages
    assert child.prefill_messages is not parent.prefill_messages
    assert child.prefill_messages[0] is not parent.prefill_messages[0]
    assert child.tools == parent.tools
    assert child.session_id == parent.session_id
    assert child._persist_disabled and child._session_db is None
    assert child._skip_mcp_refresh and not child._end_session_on_close


@pytest.mark.parametrize('provider', ['custom', 'openai'])
def test_same_router_native_fork_keeps_remote_watchdog_and_cache_parity(parent_factory, provider):
    parent = parent_factory(provider=provider)
    child, runtime, routed = review.build_cache_parity_fork(parent, {}, max_iterations=2)
    try:
        assert not routed
        assert child.provider == provider
        assert child.requested_provider == 'custom:omniroute'
        assert runtime['requested_provider'] == 'custom:omniroute'
        assert child._compute_non_stream_stale_timeout({'input': 'synthetic'}) == 90
        assert _derive_stream_stale_timeout(child, {'input': 'synthetic'}) == 180
        assert child.api_key == parent.api_key
        assert child.base_url == parent.base_url
        assert_detached_parity(parent, child)
    finally:
        child.close()


@pytest.mark.parametrize('provider,requested', [
    ('custom', 'custom'), ('custom', 'custom:local'), ('ollama', 'custom:omniroute'),
])
def test_native_current_route_or_fallback_keeps_local_patience(parent_factory, provider, requested):
    parent = parent_factory(provider=provider, requested=requested)
    child, _, routed = review.build_cache_parity_fork(parent, {}, max_iterations=2)
    try:
        assert not routed
        assert child._compute_non_stream_stale_timeout({'input': 'synthetic'}) == float('inf')
        assert _derive_stream_stale_timeout(child, {'input': 'synthetic'}) == _derive_stream_stale_timeout(parent, {'input': 'synthetic'})
        if provider == 'ollama':
            assert child.requested_provider == 'ollama'
        assert_detached_parity(parent, child)
    finally:
        child.close()


@pytest.mark.parametrize('aux_provider,expected_requested,finite', [
    ('custom:local', 'custom:local', False), ('custom:omniroute', 'custom:omniroute', True),
])
def test_explicit_aux_uses_its_own_resolved_identity(parent_factory, aux_provider, expected_requested, finite):
    parent = parent_factory()
    task = {'provider': aux_provider, 'model': 'synthetic-aux'}
    resolved = {'provider': 'custom', 'requested_provider': expected_requested,
                'model': 'synthetic-aux', 'api_key': 'fixture-aux-only',
                'base_url': 'http://127.0.0.1:21434/v1', 'api_mode': 'chat_completions'}
    with patch('hermes_cli.runtime_provider.resolve_runtime_provider', return_value=resolved) as resolve:
        child, runtime, routed = review.build_cache_parity_fork(parent, task, max_iterations=2)
    try:
        resolve.assert_called_once_with(requested=aux_provider, target_model='synthetic-aux',
                                       explicit_api_key=None, explicit_base_url=None)
        assert routed
        assert child.requested_provider == expected_requested
        assert runtime['requested_provider'] == expected_requested
        assert child.model == 'synthetic-aux'
        assert child.api_key == 'fixture-aux-only'
        assert child.base_url == resolved['base_url']
        assert child._compute_non_stream_stale_timeout({'input': 'synthetic'}) == (90 if finite else float('inf'))
        assert child._cached_system_prompt != parent._cached_system_prompt
        assert child._persist_disabled and child._session_db is None
    finally:
        child.close()


def test_aux_resolution_failure_reuses_current_parent_identity(parent_factory):
    parent = parent_factory()
    with patch('hermes_cli.runtime_provider.resolve_runtime_provider', side_effect=RuntimeError('synthetic')):
        child, _, routed = review.build_cache_parity_fork(
            parent, {'provider': 'custom:missing', 'model': 'other'}, max_iterations=2)
    try:
        assert not routed
        assert child.requested_provider == 'custom:omniroute'
        assert child._compute_non_stream_stale_timeout({'input': 'synthetic'}) == 90
        assert_detached_parity(parent, child)
    finally:
        child.close()


def test_router_fork_preserves_explicit_named_and_canonical_timeout_settings(parent_factory, tmp_path):
    parent = parent_factory()
    (tmp_path / 'config.yaml').write_text('providers:\n  omniroute:\n    stale_timeout_seconds: 420\n'
                                         '  custom:\n    request_timeout_seconds: 75\n')
    child, _, _ = review.build_cache_parity_fork(parent, {}, max_iterations=2)
    try:
        assert child._resolved_api_call_timeout() == 75
        assert child._compute_non_stream_stale_timeout({'input': 'synthetic'}) == 420
    finally:
        child.close()


def test_legacy_parent_without_requested_identity_remains_native(parent_factory):
    parent = parent_factory(requested='custom')
    del parent.requested_provider
    child, _, routed = review.build_cache_parity_fork(parent, {}, max_iterations=2)
    try:
        assert not routed
        assert child.requested_provider == 'custom'
        assert child._compute_non_stream_stale_timeout({'input': 'synthetic'}) == float('inf')
    finally:
        child.close()


def test_actual_detached_router_stream_aborts_silence_and_recovers(parent_factory, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from agent import chat_completion_helpers as helpers
    parent = parent_factory()
    child, _, _ = review.build_cache_parity_fork(parent, {}, max_iterations=2)
    started, aborted, finished = threading.Event(), threading.Event(), threading.Event()
    epoch = time.time()
    monkeypatch.setattr(helpers, 'time', SimpleNamespace(
        time=lambda: epoch + (200 if started.is_set() else 0), sleep=time.sleep))
    calls = []
    client = MagicMock()
    def chunk(text):
        return SimpleNamespace(choices=[SimpleNamespace(index=0, delta=SimpleNamespace(
            content=text, tool_calls=None, reasoning_content=None, reasoning=None),
            finish_reason='stop')], model=child.model, usage=None)
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
    monkeypatch.setattr(child, '_create_request_openai_client', lambda **kw: client)
    monkeypatch.setattr(child, '_close_request_openai_client', lambda *a, **k: None)
    monkeypatch.setattr(child, '_abort_request_openai_client', lambda *a, **k: aborted.set())
    payload = {'model': child.model, 'messages': [{'role': 'user', 'content': 'synthetic'}],
               'reasoning_effort': 'high'}
    try:
        try:
            child._interruptible_streaming_api_call(payload)
        except httpx.ReadError:
            pass
        assert aborted.is_set(), 'Detached named router inherited the native 900-second wait'
        assert finished.wait(2)
        assert child._interruptible_streaming_api_call(payload).choices[0].message.content == 'recovered'
        assert all(c['model'] == parent.model and c['reasoning_effort'] == 'high' for c in calls)
    finally:
        aborted.set()
        child.close()
