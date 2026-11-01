"""Explicit CLI denies apply to directly executed interpreter targets."""
import pytest
from hermes_cli.dashboard_governance.models import GrantSet
from hermes_cli.dashboard_governance.tool_policy import _check_cli_command

@pytest.mark.parametrize('command', [
    'python -mbunq_cli accounts',
    'node --require ./bunq.js allowed.js', 'node --import ./bunq.js allowed.js',
    'node --require=./bunq.js allowed.js', 'node -r./bunq.js allowed.js',
    'node --loader ./bunq.js allowed.js',
    "node --require ./bunq.js -e 'console.log(1)'",
    'python bunq_cli.py accounts', 'python3 /tools/bunq_cli.py accounts',
    'python3.12 -B -W ignore -X dev bunq_cli.py accounts',
    'python -m bunq_cli accounts', 'python -- bunq_cli.py',
    'env X=1 python bunq_cli.py', 'command python bunq_cli.py',
    'node /tools/bunq_cli.js', 'node -- bunq_cli.js',
    'bash bunq_cli.sh', 'bash -o errexit bunq_cli.sh',
    'printf "%s" "$(python bunq_cli.py accounts)"',
])
def test_direct_interpreter_target_is_denied_before_any_dispatch(command):
    decision = _check_cli_command(command, GrantSet(cli_commands=frozenset({'*'}),
        cli_denied_commands=frozenset({'*bunq*'})))
    assert not decision.allowed and decision.reason == 'cli_command_denied'

@pytest.mark.parametrize('command', ['python -c \'print("bunq_cli.py")\'', 'python -c\'print("bunq_cli.py")\'', 'python -B -c \'print("bunq_cli.py")\'', 'node -e \'console.log("bunq_cli.js")\'', 'node --eval \'console.log("bunq_cli.js")\'', 'python allowed.py bunq_cli.py', 'printf "%s" bunq_cli.py', 'python -W bunq_warning allowed.py'])
def test_inert_mention_is_not_an_executed_target(command):
    assert _check_cli_command(command, GrantSet(cli_commands=frozenset({'*'}),
        cli_denied_commands=frozenset({'*bunq*'}))).allowed
