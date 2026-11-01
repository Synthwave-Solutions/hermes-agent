"""The host's provider approval applies to native connection mutations too."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json

import pytest

from tools.connectors.gateway.client import ConnectorClient
from tools.connectors.identity import (
    bind_connector_identity,
    personal_connector_token,
    reset_connector_identity,
)


class Transport:
    def __init__(self):
        self.mutations = []

    def request(self, method, url, *, headers, json=None, timeout=None):
        payload = {'items': [{'connector': 'gmail', 'connected': False}]}
        if method == 'POST':
            self.mutations.append(json)
            payload = {'results': [{'connector': name, 'status': 'initiated',
                                   'connectUrl': 'https://auth.example.test/start'}
                                  for name in json['connectors']]}
        return type('Response', (), {'status_code': 200, 'headers': {},
                                     'json': lambda self: payload})()


def client(transport):
    return ConnectorClient(transport=transport,
                           endpoint_resolver=lambda: 'https://connectors.example.test',
                           header_provider=lambda url: {'Authorization': 'Bearer fixture'})


@pytest.mark.parametrize('action,force', [('connect', False), ('reconnect', False), ('reconnect', True)])
def test_native_tool_checks_bound_approval_before_connect_or_reconnect(monkeypatch, action, force):
    from tools.connectors import managed, tool
    from tools.registry import registry

    transport = Transport()
    monkeypatch.setattr(managed, 'managed_client', lambda **kwargs: client(transport))
    monkeypatch.setattr(tool.gateway_config, 'connectors_available', lambda: True)
    approvals = set()
    checked = []

    def authorize(slugs):
        checked.append(tuple(slugs))
        if not set(slugs) <= approvals:
            raise PermissionError('Provider approval required')

    token = bind_connector_identity(lambda: 'personal-fixture', authorize_connection=authorize)
    try:
        args = {'action': action, 'connectors': ['gmail'], 'force': force}
        denied = json.loads(registry.dispatch('manage_connections', args))
        assert 'Provider approval required' in denied['error']
        assert transport.mutations == []
        approvals.add('gmail')
        allowed = json.loads(registry.dispatch('manage_connections', args))
        assert allowed['status'] == 'initiated'
        assert transport.mutations[0]['connectors'] == ['gmail']
        assert transport.mutations[0]['reinitiate'] is (action == 'reconnect')
        assert checked == [('gmail',), ('gmail',)]
    finally:
        reset_connector_identity(token)


def test_callback_and_token_follow_copied_context_and_reset_together():
    transport = Transport()

    def deny(slugs):
        raise PermissionError('Member approval required')

    outer = bind_connector_identity(lambda: 'member', authorize_connection=deny)
    try:
        inner = bind_connector_identity(lambda: 'legacy-caller')
        try:
            client(transport).connections(['gmail'])
            assert personal_connector_token() == (True, 'legacy-caller')
        finally:
            reset_connector_identity(inner)
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(context.run, personal_connector_token).result() == (True, 'member')
            future = pool.submit(context.run, client(transport).connections, ['gmail', 'notion'])
            with pytest.raises(PermissionError, match='Member approval required'):
                future.result()
        assert len(transport.mutations) == 1
    finally:
        reset_connector_identity(outer)
    assert personal_connector_token() == (False, None)
    client(transport).connections(['gmail'])
    assert len(transport.mutations) == 2
