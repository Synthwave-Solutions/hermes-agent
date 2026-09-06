from dataclasses import replace
import hashlib
import json

from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, serialize_context_for_env, context_from_env_payload
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance import grant_requests


def _context():
    subject = GovernanceSubject(email='person@example.test')
    return DashboardGovernanceContext(subject=subject, access=EffectiveAccess(subject=subject, mode='enforce', grants=GrantSet()), session_id='session1', request_id='run1', user_message_sha256=hashlib.sha256(b'original ask').hexdigest())


def test_operation_is_immutable_while_aggregate_retries_increase(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('HERMES_SESSION_LAST_USER_MESSAGE', 'token=secretvalue original ask')
    ctx = _context()
    assert grant_requests.record_denial(ctx, 'terminal', 'cli_command_not_allowed', 'example', tool_call_id='call1', dispatch_session_id='session1')
    first = grant_requests.load_store()['person@example.test|cli|example']
    op = next(iter(first['operations'].values()))
    assert op['binding_status'] == 'bound'
    assert op['prompt_sha256'] == ctx.user_message_sha256
    assert 'secretvalue' not in json.dumps(first)
    monkeypatch.setenv('HERMES_SESSION_LAST_USER_MESSAGE', 'different retry')
    grant_requests.record_denial(ctx, 'terminal', 'cli_command_not_allowed', 'example', tool_call_id='call1')
    later = grant_requests.load_store()['person@example.test|cli|example']
    assert later['count'] == 2
    assert later['operations'] == first['operations']
    grant_requests.record_denial(replace(ctx, request_id='run2'), 'terminal', 'cli_command_not_allowed', 'example', tool_call_id='call1')
    assert len(grant_requests.load_store()['person@example.test|cli|example']['operations']) == 2


def test_missing_or_conflicting_metadata_never_binds(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_WEBUI_STATE_DIR', str(tmp_path))
    for i, options in enumerate([{}, {'tool_call_id':'call', 'dispatch_session_id':'different'}, {'tool_call_id':'bad\ncall'}]):
        grant_requests.record_denial(_context(), 'terminal', 'cli_command_not_allowed', f'cmd{i}', **options)
    for entry in grant_requests.load_store().values():
        assert next(iter(entry['operations'].values()))['binding_status'] == 'unresolved'


def test_prompt_digest_roundtrip_and_unknown_freshness(tmp_path, monkeypatch):
    ctx = _context()
    assert context_from_env_payload(serialize_context_for_env(ctx)).user_message_sha256 == ctx.user_message_sha256
    monkeypatch.setenv('HERMES_WEBUI_STATE_DIR', str(tmp_path))
    grant_requests.record_denial(replace(ctx, user_message_sha256=''), 'terminal', 'cli_command_not_allowed', 'example', tool_call_id='call')
    op = next(iter(next(iter(grant_requests.load_store().values()))['operations'].values()))
    assert op['prompt_sha256'] is None
    assert op['freshness_status'] == 'unresolved'


def test_wait_reloads_policy_and_does_not_trust_signal_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    import model_tools
    from hermes_cli.dashboard_governance.context import bind_governance_context, reset_governance_context
    policy = tmp_path / 'policy.yaml'
    policy.write_text('mode: enforce\ndefault_effect: deny\n')
    seen = []
    def waiter(op):
        seen.append(op)
        return True
    failed = []
    waiter.finish = lambda op_id, success: failed.append(success)
    ctx = replace(_context(), approval_waiter=waiter, approval_policy_path=str(policy))
    grant_requests.record_denial(ctx, "read_file", "tool_not_allowed", "", tool_call_id="call1", dispatch_session_id="session1")
    token = bind_governance_context(ctx)
    try:
        assert model_tools._wait_for_governance_grant(ctx, 'read_file', {'path':str(tmp_path/'x')}, 'tool_not_allowed', '', 'call1', 'session1') is False
        assert failed == [False]
        assert len(seen) == 1
        policy.write_text('mode: enforce\ndefault_effect: deny\nusers:\n  person@example.test:\n    grants:\n      profiles: ["*"]\n      tools:\n        builtins: ["*"]\n      files:\n        read_roots: ["' + str(tmp_path) + '"]\n')
        assert model_tools._wait_for_governance_grant(ctx, 'read_file', {'path':str(tmp_path/'x')}, 'tool_not_allowed', '', 'call1', 'session1') is True
    finally:
        reset_governance_context(token)


def test_waiter_capabilities_do_not_serialize():
    ctx = replace(_context(), approval_waiter=lambda _:True, approval_policy_path='/private/policy')
    payload = serialize_context_for_env(ctx)
    assert 'approval_waiter' not in payload
    assert '/private/policy' not in payload
    assert context_from_env_payload(payload).approval_waiter is None


def test_dispatcher_continues_same_denied_read_after_exact_approval(tmp_path, monkeypatch):
    import model_tools
    from hermes_cli.dashboard_governance.context import bind_governance_context, reset_governance_context
    monkeypatch.setenv('HERMES_WEBUI_STATE_DIR', str(tmp_path / 'spool'))
    artifact = tmp_path / 'artifact.txt'
    artifact.write_text('same invocation marker')
    policy = tmp_path / 'policy.yaml'
    policy.write_text('mode: enforce\ndefault_effect: deny\n')
    observed = []
    def waiter(operation):
        observed.append(operation)
        policy.write_text('mode: enforce\ndefault_effect: deny\nusers:\n  person@example.test:\n    grants:\n      profiles: ["*"]\n      tools:\n        builtins: ["read_file"]\n      files:\n        read_roots: ["' + str(tmp_path) + '"]\n')
        return True
    ctx = replace(_context(), approval_waiter=waiter, approval_policy_path=str(policy))
    token = bind_governance_context(ctx)
    try:
        result = model_tools.handle_function_call('read_file', {'path':str(artifact)}, session_id='session1', tool_call_id='read-call', skip_pre_tool_call_hook=True, skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    finally:
        reset_governance_context(token)
    assert len(observed) == 1
    assert observed[0]['tool_call_id'] == 'read-call'
    assert 'same invocation marker' in result
