"""Distinguish literal interpreter payloads from shell-executed substitutions."""
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.dashboard_governance import action_approval
from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context
from hermes_cli.dashboard_governance.loader import load_governance_policy
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.tool_policy import (
    _check_cli_command, cli_command_requires_manual_approval, decide_tool_argument_access,
)


@pytest.mark.parametrize('principal', ['bootstrap', 'admin', 'member'])
@pytest.mark.parametrize('payload', [
    'print("`literal-template`")',
    'print("<(literal-process)")',
    'print(">(literal-process)")',
    'print("$(not-an-executable)")',
    'print("$(")',
    'page.evaluate("() => `literal ${document.title}`")',
])
def test_single_quoted_python_and_javascript_are_data_for_every_principal(principal, payload):
    subject = GovernanceSubject(email='synthetic@example.test')
    access = EffectiveAccess(subject=subject, mode='enforce',
        roles=frozenset({'admin'} if principal == 'admin' else ()),
        grant_sources=('bootstrap_admin',) if principal == 'bootstrap' else (),
        grants=GrantSet(cli_commands=frozenset({'*'} if principal != 'member' else {'uv'})))
    command = 'uv run --with playwright python -c ' + shlex.quote(payload)
    decision = decide_tool_argument_access(access, 'terminal', {'command': command})
    assert decision.allowed, decision.reason


@pytest.mark.parametrize('command', [
    'printf `touch forbidden`',
    'printf "`touch forbidden`"',
    'cat <(touch forbidden)',
    'cat >(touch forbidden)',
    'printf "$(touch forbidden)"',
    'printf $(touch forbidden)',
])
def test_actual_shell_execution_remains_denied_for_constrained_caller(command):
    grants = GrantSet(cli_commands=frozenset({'*'}), cli_denied_commands=frozenset({'touch'}))
    assert not _check_cli_command(command, grants).allowed


@pytest.mark.parametrize('command', [
    r'printf %s \`literal\`',
    r'printf %s "\`literal\`"',
    'printf %s "<(literal) >(literal)"',
    '''printf "$(printf '%s' ')')"''',
    '''printf "$(printf '%s' '`literal`')"''',
])
def test_literal_escaping_and_nested_command_quotes_preserve_allowed_meaning(command):
    assert _check_cli_command(command, GrantSet(cli_commands=frozenset({'printf'}))).allowed


def test_literal_substitution_does_not_trigger_manual_selector_but_real_one_does():
    access = EffectiveAccess(subject=GovernanceSubject(email='synthetic@example.test'), mode='enforce',
        grants=GrantSet(cli_approval_commands=frozenset({'touch'})))
    assert not cli_command_requires_manual_approval(access, "printf '%s' '$(touch literal)'")
    assert cli_command_requires_manual_approval(access, 'printf "$(touch effect)"')


@pytest.mark.parametrize('command', [
    'printf "$(printf ok # )\ntouch effect)"',
    '''printf "$(printf '%s' ')'; touch effect)"''',
    '''printf "$(printf '%s' "$(touch effect)")"''',
    "printf ok # `literal`\ntouch effect",
    r'printf \ #$(touch effect)',
    'printf "$(printf \\ #literal; touch effect)"',
])
def test_comment_and_quoted_parentheses_cannot_hide_real_denied_commands(command):
    decision = _check_cli_command(command, GrantSet(cli_commands=frozenset({'*'}),
        cli_denied_commands=frozenset({'touch'})))
    assert not decision.allowed
    assert decision.reason == 'cli_command_denied'


def test_quoted_interpreter_payload_reaches_real_dispatch_without_phantom_approval(tmp_path, monkeypatch):
    import model_tools
    from tools import approval

    path = tmp_path / 'policy.yaml'
    path.write_text(yaml.safe_dump({'mode': 'enforce', 'bootstrap_admins': ['owner@example.test']}))
    subject = GovernanceSubject(email='owner@example.test')
    ctx = DashboardGovernanceContext(subject=subject,
        access=resolve_effective_access(load_governance_policy(path=path), subject),
        active_profile='default', session_id='quoted-payload', approval_policy_path=str(path))
    monkeypatch.setattr(action_approval, '_ask_model', lambda *_: pytest.fail('legacy bootstrap does not request AI'))
    monkeypatch.setattr(approval, 'request_governance_action_approval', lambda *_: pytest.fail('literal text cannot require manual review'))
    monkeypatch.setattr('hermes_cli.plugins.has_hook', lambda _: False)
    monkeypatch.setattr(model_tools.registry, 'get_entry', lambda _: SimpleNamespace(toolset='terminal', schema={}))
    monkeypatch.setattr(model_tools.registry, 'get_toolset_for_tool', lambda _: 'terminal')
    calls = []
    expected = '`literal` <(data) >(data) $(not-a-command)'
    command = shlex.quote(sys.executable) + ' -B -c ' + shlex.quote('print(' + repr(expected) + ')')
    def dispatch(name, args, **kwargs):
        assert name == 'terminal'
        calls.append(args['command'])
        completed = subprocess.run(['/bin/bash', '-c', args['command']], cwd=tmp_path,
            env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=5)
        return json.dumps({'exit_code': completed.returncode, 'output': completed.stdout})
    monkeypatch.setattr(model_tools.registry, 'dispatch', dispatch)
    with governance_context(ctx):
        result = json.loads(model_tools.handle_function_call('terminal', {'command': command, 'workdir': str(tmp_path)},
            session_id='quoted-payload', skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True))
    assert result.get('exit_code') == 0, result
    assert result['output'] == expected + '\n'
    assert calls == [command]
