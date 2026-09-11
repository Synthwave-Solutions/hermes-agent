"""The automatic verdict uses the operator's configured approval model route."""
from types import SimpleNamespace

from agent import auxiliary_client
from hermes_cli import config
from hermes_cli.dashboard_governance.action_approval import _ask_model


def test_automatic_action_uses_configured_approval_task_without_advice_fallback(monkeypatch):
    configured = {'provider': 'custom:reviewer', 'model': 'approval-model',
                  'base_url': 'http://127.0.0.1:1'}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'auxiliary': {'approval': configured}})
    seen = []
    def call(**kwargs):
        # Use the real task-config lookup used by the router, without opening a
        # provider connection. An unconfigured task resolves to an empty dict.
        seen.append((kwargs, auxiliary_client._get_auxiliary_task_config(kwargs['task'])))
        return {'choices': [{'message': {'content':
            '{"decision":"approve","reason":"Synthetic read allowed","confidence":0.99}'}}]}
    monkeypatch.setattr(auxiliary_client, 'call_llm', call)
    result = _ask_model(SimpleNamespace(approval_prompt='Approve synthetic read-only checks.'),
                        {'tool': 'read_file', 'arguments': {'path': '/workspace/synthetic.txt'}})
    assert result['decision'] == 'approve'
    assert len(seen) == 1
    kwargs, selected = seen[0]
    assert kwargs['task'] == 'approval'
    assert selected == configured
    assert kwargs['timeout'] == 20
    assert kwargs.get('tools') is None
