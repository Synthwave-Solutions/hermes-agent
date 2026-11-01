"""Real encrypted stores and manager scopes stay personal across shared bots."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path

import pytest

from agent.vault_scope import bind_personal_vault, reset_personal_vault, manager_environment
from agent.vault_store import get_vault_store
from agent.vault_backends import unlock


def _add(label, password):
    return get_vault_store().add_item("login", label,
        {"identifier_type": "email", "identifier": label + "@example.test", "password": password},
        origin="https://example.test")


def test_personal_store_and_manager_tokens_inherit_into_delegated_context(tmp_path):
    host = _add("Host", "host password")
    alice = bind_personal_vault(tmp_path / "alice")
    try:
        item = _add("Alice", "alice password")
        unlock.store_session_token("bitwarden", "alice token")
        with ThreadPoolExecutor(max_workers=1) as pool:
            def child():
                return get_vault_store().resolve_secret(item.id), unlock.get_session_token("bitwarden")
            secret, token = pool.submit(copy_context().run, child).result(timeout=5)
        assert secret["password"] == "alice password"
        assert token == "alice token"
        bob = bind_personal_vault(tmp_path / "bob")
        try:
            assert get_vault_store().list_items() == []
            assert unlock.get_session_token("bitwarden") is None
            unlock.lock("bitwarden")
            with pytest.raises(Exception, match="no vault item"):
                get_vault_store().resolve_secret(item.id)
        finally:
            reset_personal_vault(bob)
        assert unlock.get_session_token("bitwarden") == "alice token"
    finally:
        unlock.lock()
        reset_personal_vault(alice)
    assert get_vault_store().resolve_secret(host.id)["password"] == "host password"


def test_settings_encrypted_not_listed_and_no_ambient_manager_credentials(tmp_path, monkeypatch):
    from agent.vault_backends.onepassword import OnePasswordLoginBackend
    from agent.vault_backends.bitwarden import BitwardenLoginBackend
    from agent.vault_backends.base import _cfg
    for key in ("OP_SERVICE_ACCOUNT_TOKEN", "OP_SESSION", "OP_CONNECT_TOKEN", "BW_SESSION"):
        monkeypatch.setenv(key, "HOST_PRIVATE_CREDENTIAL")
    monkeypatch.setenv("OP_CONFIG_DIR", str(tmp_path / "host-op"))
    monkeypatch.setenv("BITWARDENCLI_APPDATA_DIR", str(tmp_path / "host-bw"))
    token = bind_personal_vault(tmp_path / "person")
    try:
        empty = OnePasswordLoginBackend(_cfg().get("onepassword"))
        assert empty._service_token == ""
        store = get_vault_store()
        store.update_source_settings("onepassword", {"service_account_token": "PERSON_PRIVATE_TOKEN"})
        store.update_source_settings("bitwarden", {"enabled": False})
        cfg = _cfg()
        assert cfg["onepassword"]["service_account_token"] == "PERSON_PRIVATE_TOKEN"
        assert cfg["bitwarden"]["enabled"] is False
        assert store.list_items() == []
        blob = (tmp_path / "person/vault/sources.json.enc").read_bytes()
        assert b"PERSON_PRIVATE_TOKEN" not in blob
        assert (tmp_path / "person/vault/vault.key").stat().st_mode & 0o777 == 0o600
        op_env = OnePasswordLoginBackend(cfg["onepassword"])._env(None)
        bw_env = BitwardenLoginBackend()._env("PERSON_SESSION")
        assert op_env["OP_SERVICE_ACCOUNT_TOKEN"] == "PERSON_PRIVATE_TOKEN"
        assert bw_env["BW_SESSION"] == "PERSON_SESSION"
        assert "HOST_PRIVATE_CREDENTIAL" not in json.dumps([op_env, bw_env])
        for key in ("HOME", "OP_CONFIG_DIR", "BITWARDENCLI_APPDATA_DIR"):
            assert Path(op_env[key]).is_relative_to(tmp_path / "person")
        assert "OP_CONNECT_TOKEN" not in op_env
        assert "OP_SESSION" not in op_env
    finally:
        reset_personal_vault(token)


@pytest.mark.parametrize("relative", ["vault", "password-managers", "vault/vault.key", "vault/sources.json.enc"])
def test_personal_vault_rejects_symlink_sources(tmp_path, relative):
    root = tmp_path / "person"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = root / relative
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    token = bind_personal_vault(root)
    try:
        with pytest.raises(PermissionError):
            if relative == "password-managers":
                manager_environment()
            else:
                get_vault_store().read_source_settings()
    finally:
        reset_personal_vault(token)
