"""Cached password-manager backends lose access when their source is revoked."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.vault_backends import bitwarden, onepassword, unlock
from agent.vault_scope import bind_personal_vault, reset_personal_vault
from agent.vault_store import get_vault_store


@pytest.fixture
def personal(tmp_path):
    token = bind_personal_vault(tmp_path / 'person')
    try:
        yield get_vault_store()
    finally:
        unlock.lock()
        reset_personal_vault(token)


def manager(store, monkeypatch, name):
    section = {'enabled': True, 'connected': True}
    calls = []
    if name == 'onepassword':
        section['service_account_token'] = 'personal-fixture-token'
        module, cls, handle = onepassword, onepassword.OnePasswordLoginBackend, 'op:item'
        items = [{'id': 'item', 'title': 'Example', 'vault': {'id': 'vault'},
                  'urls': [{'href': 'https://example.test'}]}]
        monkeypatch.setattr(cls, '_op', lambda self: Path('/fixture/op'))
    else:
        section['server'] = 'https://vault.bitwarden.eu'
        module, cls, handle = bitwarden, bitwarden.BitwardenLoginBackend, 'bw:item'
        items = [{'id': 'item', 'type': 1, 'name': 'Example',
                  'login': {'uris': [{'uri': 'https://example.test'}]}}]
        monkeypatch.setattr(cls, '_bw', lambda self: Path('/fixture/bw'))
        unlock.store_session_token(name, 'personal-fixture-session')
    store.update_source_settings(name, section)

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(items) if 'list' in argv else 'private-password', stderr='')

    monkeypatch.setattr(module, 'run_cli', run)
    backend = cls(store.read_source_settings()[name])
    assert backend.get_meta(handle) is not None
    return backend, handle, section, calls, module


@pytest.mark.parametrize('name', ['onepassword', 'bitwarden'])
@pytest.mark.parametrize('change', ['disabled', 'removed', 'replaced', 'same_settings_replaced'])
def test_preconstructed_backend_cannot_use_revoked_source(personal, monkeypatch, name, change):
    backend, handle, original, calls, _ = manager(personal, monkeypatch, name)
    replacement = dict(original)
    if change == 'disabled':
        replacement['enabled'] = False
    elif change == 'removed':
        replacement = {}
    elif change == 'replaced':
        replacement['service_account_token' if name == 'onepassword' else 'server'] = 'replacement'
    personal.update_source_settings(name, replacement)
    before = len(calls)
    with pytest.raises((PermissionError, RuntimeError)):
        backend.resolve_password(handle)
    assert backend.list_items() == []
    assert backend.resolve_otp(handle) is None
    assert len(calls) == before, 'revoked credentials must never reach the manager CLI'


@pytest.mark.parametrize('name', ['onepassword', 'bitwarden'])
def test_revocation_during_cli_call_discards_returned_secret(personal, monkeypatch, name):
    backend, handle, original, calls, module = manager(personal, monkeypatch, name)

    def revoke_during_call(argv, **kwargs):
        calls.append(argv)
        personal.update_source_settings(name, {**original, 'enabled': False})
        return SimpleNamespace(returncode=0, stdout='revoked-private-password', stderr='')

    monkeypatch.setattr(module, 'run_cli', revoke_during_call)
    with pytest.raises((PermissionError, RuntimeError)):
        backend.resolve_password(handle)


def test_bitwarden_explicit_lock_discards_inflight_read(personal, monkeypatch):
    backend, handle, _, _, module = manager(personal, monkeypatch, 'bitwarden')

    def lock_during_call(argv, **kwargs):
        unlock.lock('bitwarden')
        return SimpleNamespace(returncode=0, stdout='locked-private-password', stderr='')

    monkeypatch.setattr(module, 'run_cli', lock_during_call)
    from agent.vault_backends.base import UnlockRequired
    with pytest.raises(UnlockRequired):
        backend.resolve_password(handle)
