"""SynthPulse inbound A2A governance (PLAN 4.3 inbound, 4.5 E2).

Covers the admission hook right after ``authenticate`` (tokens required, the
local dev flag, peer mapping, organisation and installation checks), the
per-method hook (actor assertion, ``serve`` list and ``a2a:serve:<agent>``,
push-config refusal), the pure effective-principal intersection, and the audit
rows. Live tests run the real adapter HTTP server on a free loopback port with
a stub agent handler.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import time
import urllib.error
import urllib.request

import pytest
import yaml

from gateway import synthpulse_identity
from plugins.platforms.a2a import protocol, security, synthpulse_governance as gov
from plugins.platforms.a2a.synthpulse_assertion import ACTOR_HEADER, sign_actor

SECRET = "s3cretValueForTests_0123456789"
TOKEN = f"a2a1.org_acme.inst_a.inst_b.{SECRET}"
OTHER_ORG_TOKEN = f"a2a1.org_other.inst_a.inst_b.{SECRET}"
HQ_TOKEN = f"a2a1.-.hq_a.hq_b.{SECRET}"
PEER = "acme-inst_a"
SERVICE = "svc-a2a-inst-a@acme.example"
ACTOR = "rep.sales@acme.example"
STRANGER = "stranger@elsewhere.example"


def _policy(mode="enforce", *, org="org_acme", service_permissions=None, service_extra=None):
    service_permissions = (["a2a:serve:default", "a2a:serve:sales", "chat:use"]
                           if service_permissions is None else service_permissions)
    policy = {
        "version": 1,
        "mode": mode,
        "default_effect": "deny",
        "roles": {
            "a2a_service": {"grants": {
                "permissions": service_permissions,
                "tools": {"builtins": ["read_file", "web_search", "terminal"]},
            }},
            "a2a_guest": {"grants": {"permissions": ["chat:use"], "tools": {"builtins": ["web_search"]}}},
        },
        "users": {
            SERVICE: {"roles": ["a2a_service"], **(service_extra or {})},
            ACTOR: {"grants": {"permissions": ["chat:use"], "tools": {"builtins": ["read_file", "web_search"]}}},
        },
    }
    if org:
        policy["organization"] = {"id": org, "name": "Acme"}
    return policy


def _mapping(**overrides):
    row = {
        "email": SERVICE,
        "profile": "",
        "org_id": "org_acme",
        "caller_installation": "inst_a",
        "serve": ["default", "sales"],
    }
    row.update(overrides)
    return {k: v for k, v in row.items() if v is not None}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Scratch HERMES_HOME with an enforced policy, one mapped peer and its token."""
    from hermes_constants import get_hermes_home

    root = get_hermes_home()
    monkeypatch.setenv("A2A_PEER_TOKENS", f"{PEER}:{TOKEN}")
    monkeypatch.setenv("SP_INSTALLATION_ID", "inst_b")
    for name in ("A2A_BEARER_TOKEN", "A2A_ALLOW_UNAUTHENTICATED_LOCAL", "A2A_PUBLIC_URL",
                 "A2A_TRUSTED_PEERS", "A2A_ALLOW_ALL_USERS", "SYNTHPULSE_GATEWAY_IDENTITIES",
                 "SP_GATEWAY_IDENTITIES_JSON", "A2A_HOST"):
        monkeypatch.delenv(name, raising=False)
    write_policy(root, _policy())
    write_mapping(root, {PEER: _mapping()})
    return root


def write_policy(root, policy):
    (root / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")


def write_mapping(root, rows):
    path = root / "webui" / "gateway-identities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"a2a": rows}), encoding="utf-8")
    synthpulse_identity._CACHE.update(path="", mtime=None, data={})


def assertion(token=TOKEN, *, actor=ACTOR, caller="inst_a", depth=0, task="", ts=None):
    return sign_actor(token, {"actor": actor, "caller": caller, "depth": depth,
                              "task": task, "ts": int(time.time()) if ts is None else ts})


def auth(token=TOKEN, **claims):
    headers = {"Authorization": f"Bearer {token}"}
    if claims.pop("with_actor", True):
        headers[ACTOR_HEADER] = assertion(token, **claims)
    return headers


def _admit(token=TOKEN):
    identity = security.A2ASecurityContext.capture().authenticate(f"Bearer {token}", "127.0.0.1")
    return gov.admit_peer(identity, auth_header=f"Bearer {token}", client_ip="127.0.0.1",
                          headers={"Host": "127.0.0.1:9900"}, bind_host="127.0.0.1")


# --------------------------------------------------------------------------
# Live server helpers
# --------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _live_adapter(monkeypatch, *, extra=None):
    from gateway.config import PlatformConfig
    from plugins.platforms.a2a.adapter import A2AAdapter

    port = _free_port()
    monkeypatch.setenv("A2A_PORT", str(port))
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra=extra or {}))
    seen = []

    async def fake_handle_message(event):
        seen.append(event)
        await adapter.send(event.source.chat_id, "ECHO: " + event.text, metadata={"notify": True})

    adapter.handle_message = fake_handle_message  # type: ignore[assignment]
    adapter._message_handler = object()
    return adapter, f"http://127.0.0.1:{port}", seen


def _post(url, body, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _send(text="hello", ctx="", method="SendMessage", extra_params=None):
    params = {"message": protocol.text_message(protocol.ROLE_USER, text, context_id=ctx)}
    params.update(extra_params or {})
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


def _run_live(monkeypatch, fn, *, extra=None):
    adapter, base, seen = _live_adapter(monkeypatch, extra=extra)

    async def run():
        assert await adapter.connect() is True
        try:
            return await asyncio.to_thread(fn, base, adapter, seen)
        finally:
            await adapter.disconnect()

    return asyncio.run(run())


def _audit_rows(root):
    path = root / "dashboard-governance-audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _central_events(root):
    path = root / "logs" / gov.CENTRAL_EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _hash(value):
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:24]


# --------------------------------------------------------------------------
# Tokens are always required; the local dev flag
# --------------------------------------------------------------------------

class TestTokensRequired:
    def test_missing_token_refused_on_loopback_without_dev_flag(self, home, monkeypatch):
        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)

        def check(base, adapter, seen):
            status, body = _post(base + "/", _send())
            assert status == 401
            assert body["error"]["code"] == protocol.ERR_UNAUTHORIZED
            assert seen == []

        _run_live(monkeypatch, check)
        events = _central_events(home)
        assert events and events[-1]["kind"] == "a2a_refused"
        assert events[-1]["context"]["reason"] == "tokens_required"

    def test_request_without_token_refused_when_tokens_exist(self, home, monkeypatch):
        def check(base, adapter, seen):
            status, body = _post(base + "/", _send())
            assert status == 401
            assert body["error"]["code"] == protocol.ERR_UNAUTHORIZED
            assert seen == []

        _run_live(monkeypatch, check)

    def test_dev_flag_refused_with_non_loopback_public_url(self, home, monkeypatch):
        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)
        write_policy(home, _policy("off"))
        monkeypatch.setenv("A2A_ALLOW_UNAUTHENTICATED_LOCAL", "1")
        monkeypatch.setenv("A2A_PUBLIC_URL", "https://acme-core.example.ts.net:9443/")

        def check(base, adapter, seen):
            status, body = _post(base + "/", _send())
            assert status == 401
            assert body["error"]["code"] == protocol.ERR_UNAUTHORIZED
            assert seen == []

        _run_live(monkeypatch, check)
        assert _central_events(home)[-1]["context"]["reason"] == "dev_flag_public_url"

    def test_dev_flag_refused_without_explicit_public_url(self, home, monkeypatch):
        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)
        write_policy(home, _policy("off"))
        monkeypatch.setenv("A2A_ALLOW_UNAUTHENTICATED_LOCAL", "1")
        identity = security.A2ASecurityContext.capture().authenticate(None, "127.0.0.1")
        refusal = gov.admit_peer(identity, auth_header=None, client_ip="127.0.0.1",
                                 headers={"Host": "127.0.0.1:9900"}, bind_host="127.0.0.1")
        assert isinstance(refusal, gov.Refusal)
        assert refusal.reason == "dev_flag_public_url"

    @pytest.mark.parametrize("headers", [
        {"Host": "127.0.0.1:9900", "X-Forwarded-For": "100.64.0.7"},
        {"Host": "127.0.0.1:9900", "Tailscale-User-Login": "someone@example.com"},
        {"Host": "acme-core.example.ts.net:9443"},
    ])
    def test_dev_flag_refused_for_proxied_or_remote_requests(self, home, monkeypatch, headers):
        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)
        write_policy(home, _policy("off"))
        monkeypatch.setenv("A2A_ALLOW_UNAUTHENTICATED_LOCAL", "1")
        monkeypatch.setenv("A2A_PUBLIC_URL", "http://127.0.0.1:9900/")
        identity = security.A2ASecurityContext.capture().authenticate(None, "127.0.0.1")
        refusal = gov.admit_peer(identity, auth_header=None, client_ip="127.0.0.1",
                                 headers=headers, bind_host="127.0.0.1")
        assert isinstance(refusal, gov.Refusal)
        assert refusal.http_status == 401

    def test_dev_flag_refused_under_enforce(self, home, monkeypatch):
        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)
        monkeypatch.setenv("A2A_ALLOW_UNAUTHENTICATED_LOCAL", "1")
        monkeypatch.setenv("A2A_PUBLIC_URL", "http://127.0.0.1:9900/")
        identity = security.A2ASecurityContext.capture().authenticate(None, "127.0.0.1")
        refusal = gov.admit_peer(identity, auth_header=None, client_ip="127.0.0.1",
                                 headers={"Host": "127.0.0.1:9900"}, bind_host="127.0.0.1")
        assert isinstance(refusal, gov.Refusal)
        assert refusal.reason == "dev_flag_under_enforce"

    def test_dev_flag_with_loopback_public_url_accepts_and_marks_binding(self, home, monkeypatch, caplog):
        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)
        write_policy(home, _policy("off"))
        monkeypatch.setenv("A2A_ALLOW_UNAUTHENTICATED_LOCAL", "1")

        def check(base, adapter, seen):
            import os
            os.environ["A2A_PUBLIC_URL"] = base + "/"
            try:
                status, body = _post(base + "/", _send("dev call"))
            finally:
                os.environ.pop("A2A_PUBLIC_URL", None)
            assert status == 200, body
            assert body["result"]["task"]["status"]["state"] == protocol.STATE_COMPLETED
            binding = seen[0].metadata[synthpulse_identity.A2A_BINDING_KEY]
            assert binding.unauthenticated_local is True
            assert binding.access is None

        with caplog.at_level("WARNING"):
            _run_live(monkeypatch, check)
        assert any("A2A_ALLOW_UNAUTHENTICATED_LOCAL" in r.getMessage() for r in caplog.records)

    def test_card_advertises_bearer_and_no_push(self, home, monkeypatch):
        def check(base, adapter, seen):
            status, card = _get(base + "/.well-known/agent-card.json")
            assert status == 200
            assert card["security"] == [{"bearer": []}]
            assert card["capabilities"]["pushNotifications"] is False

        _run_live(monkeypatch, check)

        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)

        def check_unconfigured(base, adapter, seen):
            status, card = _get(base + "/.well-known/agent-card.json")
            assert card["security"] == [{"bearer": []}]

        _run_live(monkeypatch, check_unconfigured)


# --------------------------------------------------------------------------
# Peer mapping, organisation and installation
# --------------------------------------------------------------------------

class TestPeerAdmission:
    @pytest.mark.parametrize("mode", ["enforce", "report_only", "off"])
    def test_unmapped_peer_refused_in_every_mode(self, home, monkeypatch, mode):
        write_policy(home, _policy(mode))
        write_mapping(home, {})

        def check(base, adapter, seen):
            status, body = _post(base + "/", _send(), auth())
            assert status == 403
            assert body["error"]["code"] == protocol.ERR_UNTRUSTED_PEER
            assert seen == []

        _run_live(monkeypatch, check)
        rows = [r for r in _audit_rows(home) if r["event"] == "a2a_inbound"]
        assert rows[-1]["reason"] == "unmapped_peer"
        assert rows[-1]["extra"]["decision"] == "refuse"
        assert rows[-1]["extra"]["peer"] == PEER

    def test_shared_bearer_token_is_not_a_pair_token(self, home, monkeypatch):
        monkeypatch.setenv("A2A_BEARER_TOKEN", "shared-secret-token")

        def check(base, adapter, seen):
            status, body = _post(base + "/", _send(), {"Authorization": "Bearer shared-secret-token"})
            assert status == 403
            assert body["error"]["code"] == protocol.ERR_UNTRUSTED_PEER
            assert seen == []

        _run_live(monkeypatch, check)

    def test_peer_token_that_is_not_an_a2a1_token_is_refused(self, home, monkeypatch):
        monkeypatch.setenv("A2A_PEER_TOKENS", f"{PEER}:plain-token-value-123")
        refusal = _admit("plain-token-value-123")
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "not_a_pair_token"

    def test_cross_organisation_token_refused(self, home, monkeypatch):
        monkeypatch.setenv("A2A_PEER_TOKENS", f"{PEER}:{OTHER_ORG_TOKEN}")
        refusal = _admit(OTHER_ORG_TOKEN)
        assert isinstance(refusal, gov.Refusal)
        assert refusal.reason == "org_mismatch"
        assert refusal.code == protocol.ERR_UNTRUSTED_PEER

    def test_mapping_of_another_organisation_refused(self, home):
        write_mapping(home, {PEER: _mapping(org_id="org_other")})
        refusal = _admit()
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "org_mismatch"

    def test_policy_of_another_organisation_refused(self, home):
        write_policy(home, _policy(org="org_other"))
        refusal = _admit()
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "org_mismatch"

    def test_caller_installation_must_match_mapping(self, home):
        write_mapping(home, {PEER: _mapping(caller_installation="inst_z")})
        refusal = _admit()
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "caller_mismatch"

    def test_callee_installation_must_be_this_installation(self, home, monkeypatch):
        monkeypatch.setenv("SP_INSTALLATION_ID", "inst_c")
        refusal = _admit()
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "callee_mismatch"

    def test_client_installation_without_installation_id_refused(self, home, monkeypatch):
        monkeypatch.delenv("SP_INSTALLATION_ID", raising=False)
        refusal = _admit()
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "installation_unknown"

    def test_mapped_peer_of_this_organisation_admitted(self, home):
        admission = _admit()
        assert isinstance(admission, gov.PeerAdmission)
        assert admission.peer == PEER
        assert admission.org_scope == "org"
        assert admission.org_id == "org_acme"
        assert TOKEN not in repr(admission)

    def test_hq_accepts_only_an_org_less_mapping(self, home, monkeypatch):
        monkeypatch.setenv("A2A_PEER_TOKENS", f"hq-a:{HQ_TOKEN}")
        monkeypatch.delenv("SP_INSTALLATION_ID", raising=False)
        write_policy(home, _policy(org=None))
        write_mapping(home, {"hq-a": _mapping(org_id=None, caller_installation="hq_a")})
        admission = _admit(HQ_TOKEN)
        assert isinstance(admission, gov.PeerAdmission)
        assert admission.org_scope == "org_unscoped"
        assert admission.org_id is None

        write_mapping(home, {"hq-a": _mapping(org_id="org_acme", caller_installation="hq_a")})
        refusal = _admit(HQ_TOKEN)
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "org_mismatch"

    def test_hq_refuses_a_token_that_names_an_organisation(self, home, monkeypatch):
        monkeypatch.delenv("SP_INSTALLATION_ID", raising=False)
        write_policy(home, _policy(org=None))
        write_mapping(home, {PEER: _mapping(org_id=None)})
        refusal = _admit()
        assert isinstance(refusal, gov.Refusal) and refusal.reason == "org_mismatch"

    def test_hq_org_unscoped_call_is_logged(self, home, monkeypatch):
        monkeypatch.setenv("A2A_PEER_TOKENS", f"hq-a:{HQ_TOKEN}")
        monkeypatch.delenv("SP_INSTALLATION_ID", raising=False)
        write_policy(home, _policy(org=None))
        write_mapping(home, {"hq-a": _mapping(org_id=None, caller_installation="hq_a")})

        def check(base, adapter, seen):
            status, body = _post(base + "/", _send(), auth(HQ_TOKEN, caller="hq_a"))
            assert status == 200, body

        _run_live(monkeypatch, check)
        row = [r for r in _audit_rows(home) if r["event"] == "a2a_inbound"][-1]
        assert row["extra"]["org_scope"] == "org_unscoped"
        assert row["extra"]["decision"] == "allow"


# --------------------------------------------------------------------------
# Actor assertions
# --------------------------------------------------------------------------

class TestActorAssertion:
    def _refused(self, monkeypatch, headers, body=None):
        def check(base, adapter, seen):
            status, resp = _post(base + "/", body or _send(), headers)
            assert status == 403, resp
            assert resp["error"]["code"] == protocol.ERR_ACTOR_ASSERTION
            assert seen == []

        _run_live(monkeypatch, check)

    def test_missing_assertion_refused(self, home, monkeypatch):
        self._refused(monkeypatch, auth(with_actor=False))
        assert _central_events(home)[-1]["context"]["reason"] == "actor_missing"

    def test_stale_assertion_refused(self, home, monkeypatch):
        self._refused(monkeypatch, auth(ts=int(time.time()) - 301))
        assert _central_events(home)[-1]["context"]["reason"] == "actor_stale"

    def test_forged_assertion_refused(self, home, monkeypatch):
        forged = sign_actor(f"a2a1.org_acme.inst_a.inst_b.{'x' * 32}", {
            "actor": ACTOR, "caller": "inst_a", "depth": 0, "task": "", "ts": int(time.time())})
        self._refused(monkeypatch, {"Authorization": f"Bearer {TOKEN}", ACTOR_HEADER: forged})
        assert _central_events(home)[-1]["context"]["reason"] == "actor_bad_signature"

    def test_depth_above_cap_refused(self, home, monkeypatch):
        self._refused(monkeypatch, auth(depth=3))

    def test_depth_cap_is_configurable(self, home):
        admission = _admit()
        headers = {ACTOR_HEADER: assertion(depth=1)}
        refused = gov.authorize_call(admission, rpc_method="SendMessage", operation="send",
                                     params=_send()["params"], agent={"slug": "", "local": True},
                                     headers=headers, max_depth=gov.max_actor_depth({"max_actor_depth": 0}))
        assert isinstance(refused, gov.Refusal) and refused.reason == "actor_depth_exceeded"
        assert gov.max_actor_depth({}) == 2
        assert gov.max_actor_depth({"max_actor_depth": 99}) == 2
        assert gov.max_actor_depth({"max_actor_depth": "x"}) == 2

    def test_assertion_bound_to_the_json_rpc_task_id(self, home):
        admission = _admit()
        adapter_agent = {"slug": "", "local": True}
        wrong = gov.authorize_call(admission, rpc_method="GetTask", operation="get",
                                   params={"id": "task-1"}, agent=adapter_agent,
                                   headers={ACTOR_HEADER: assertion(task="task-2")})
        assert isinstance(wrong, gov.Refusal) and wrong.reason == "actor_task_mismatch"
        unbound = gov.authorize_call(admission, rpc_method="GetTask", operation="get",
                                     params={"id": "task-1"}, agent=adapter_agent,
                                     headers={ACTOR_HEADER: assertion(task="")})
        assert isinstance(unbound, gov.Refusal) and unbound.reason == "actor_task_mismatch"
        ok = gov.authorize_call(admission, rpc_method="GetTask", operation="get",
                                params={"id": "task-1"}, agent=adapter_agent,
                                headers={ACTOR_HEADER: assertion(task="task-1")})
        assert isinstance(ok, synthpulse_identity.A2ABinding)
        assert ok.task_id == "task-1"

    @pytest.mark.parametrize("now", [float("nan"), float("inf"), None])
    def test_non_finite_clock_fails_closed(self, home, now):
        admission = _admit()
        if now is None:
            now = "not-a-number"
        refused = gov.authorize_call(admission, rpc_method="SendMessage", operation="send",
                                     params=_send()["params"], agent={"slug": "", "local": True},
                                     headers={ACTOR_HEADER: assertion()}, now=now)
        assert isinstance(refused, gov.Refusal) and refused.reason == "actor_clock_invalid"


# --------------------------------------------------------------------------
# The hook runs on every JSON-RPC method
# --------------------------------------------------------------------------

_EVERY_METHOD = [
    ("SendMessage", _send()["params"]),
    ("message/send", _send()["params"]),
    ("SendStreamingMessage", _send()["params"]),
    ("message/stream", _send()["params"]),
    ("GetTask", {"id": "task-x"}),
    ("tasks/get", {"taskId": "task-x"}),
    ("ListTasks", {}),
    ("tasks/list", {}),
    ("CancelTask", {"id": "task-x"}),
    ("tasks/cancel", {"taskId": "task-x"}),
    ("SubscribeToTask", {"id": "task-x"}),
    ("tasks/subscribe", {"taskId": "task-x"}),
    ("CreateTaskPushNotificationConfig", {"taskId": "task-x", "pushNotificationConfig": {"url": "https://x.example/h"}}),
    ("tasks/pushNotificationConfig/set", {"taskId": "task-x", "pushNotificationConfig": {"url": "https://x.example/h"}}),
    ("GetTaskPushNotificationConfig", {"taskId": "task-x"}),
    ("ListTaskPushNotificationConfigs", {"taskId": "task-x"}),
    ("DeleteTaskPushNotificationConfig", {"taskId": "task-x"}),
    ("no/such/method", {}),
]


class TestEveryMethod:
    def test_hook_runs_and_refuses_on_every_method_without_assertion(self, home, monkeypatch):
        calls = []
        real = gov.authorize_call

        def spy(admission, **kwargs):
            calls.append(kwargs["rpc_method"])
            return real(admission, **kwargs)

        monkeypatch.setattr(gov, "authorize_call", spy)

        def check(base, adapter, seen):
            for method, params in _EVERY_METHOD:
                status, body = _post(base + "/", {"jsonrpc": "2.0", "id": method, "method": method,
                                                   "params": params}, auth(with_actor=False))
                assert status == 403, (method, body)
                assert body["error"]["code"] == protocol.ERR_ACTOR_ASSERTION, method
            assert seen == []

        _run_live(monkeypatch, check)
        assert calls == [m for m, _ in _EVERY_METHOD]

    def test_push_config_methods_refused_for_an_authorized_peer(self, home, monkeypatch):
        def check(base, adapter, seen):
            for method, params in _EVERY_METHOD:
                if "Push" not in method and "pushNotification" not in method:
                    continue
                headers = auth(task=params["taskId"])
                status, body = _post(base + "/", {"jsonrpc": "2.0", "id": "1", "method": method,
                                                   "params": params}, headers)
                assert status == 200, (method, body)
                assert body["error"]["code"] == protocol.ERR_PUSH_NOT_SUPPORTED, method
            assert adapter.tasks.get("task-x") is None

        _run_live(monkeypatch, check)

    def test_inline_push_config_on_send_refused(self, home, monkeypatch):
        def check(base, adapter, seen):
            body = _send(extra_params={"configuration": {"taskPushNotificationConfig": {
                "url": "http://100.64.0.1:20128/hook"}}})
            status, resp = _post(base + "/", body, auth())
            assert status == 200
            assert resp["error"]["code"] == protocol.ERR_PUSH_NOT_SUPPORTED
            assert seen == []

        _run_live(monkeypatch, check)


# --------------------------------------------------------------------------
# Effective principal (pure intersection) and a2a:serve
# --------------------------------------------------------------------------

def _load_policy(policy):
    from hermes_cli.dashboard_governance.loader import parse_governance_policy
    return parse_governance_policy(policy)


class TestEffectivePrincipal:
    def test_actor_with_fewer_rights_narrows_the_service_principal(self):
        policy = _load_policy(_policy())
        service, actor, effective = gov.resolve_principals(
            policy, service_email=SERVICE, actor_email=ACTOR, peer=PEER)
        assert service.allows("tools", "terminal")
        assert not actor.allows("tools", "terminal")
        assert not effective.allows("tools", "terminal")
        assert effective.allows("tools", "read_file")
        assert effective.allows("tools", "web_search")
        assert not effective.has_permission("a2a:serve:sales")  # the actor has no serve grant
        assert effective.subject.email == ACTOR

    def test_service_principal_narrows_a_broader_actor(self):
        policy_dict = _policy()
        policy_dict["bootstrap_admins"] = ["owner@acme.example"]
        policy = _load_policy(policy_dict)
        _service, actor, effective = gov.resolve_principals(
            policy, service_email=SERVICE, actor_email="owner@acme.example", peer=PEER)
        assert actor.allows("tools", "patch")  # a bootstrap admin holds everything
        assert not effective.allows("tools", "patch")
        assert effective.allows("tools", "read_file")
        assert effective.access_level == "user"  # the lower of admin and the service's user
        assert "bootstrap_admin" not in effective.grant_sources
        assert not (effective.roles & {"owner", "admin"})

    def test_unknown_actor_gets_the_a2a_guest_role_only(self):
        policy = _load_policy(_policy())
        _service, actor, effective = gov.resolve_principals(
            policy, service_email=SERVICE, actor_email=STRANGER, peer=PEER)
        assert gov.GUEST_ROLE in actor.roles
        assert effective.allows("tools", "web_search")
        assert not effective.allows("tools", "read_file")

    def test_unknown_actor_without_a_guest_role_gets_nothing(self):
        policy_dict = _policy()
        del policy_dict["roles"]["a2a_guest"]
        policy = _load_policy(policy_dict)
        _service, _actor, effective = gov.resolve_principals(
            policy, service_email=SERVICE, actor_email=STRANGER, peer=PEER)
        assert not effective.allows("tools", "web_search")
        assert effective.grants.tools == frozenset()

    def test_intersection_unites_denies_and_keeps_the_stricter_level(self):
        from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
        from hermes_cli.dashboard_governance.resolver import _wildcard_grants

        actor = EffectiveAccess(
            subject=GovernanceSubject(email=ACTOR), mode="enforce", grants=_wildcard_grants(),
            permissions=frozenset({"*"}), profiles=frozenset({"*"}), routes=frozenset({"*"}),
            deny=GrantSet(tools=frozenset({"patch"})), access_level="admin", access_mode="blacklist",
            roles=frozenset({"admin", "sales"}))
        service = EffectiveAccess(
            subject=GovernanceSubject(email=SERVICE), mode="enforce",
            grants=GrantSet(tools=frozenset({"patch", "read_file"}), permissions=frozenset({"chat:use"}),
                            usage_caps={"daily_tokens": 1000}),
            permissions=frozenset({"chat:use"}),
            deny=GrantSet(tools=frozenset({"read_file"})), access_level="elevated", access_mode="whitelist",
            roles=frozenset({"sales"}))
        effective = gov.intersect_access(actor, service)
        assert not effective.allows("tools", "patch")      # the actor's deny
        assert not effective.allows("tools", "read_file")  # the service's deny
        assert effective.access_level == "elevated"
        assert effective.access_mode == "whitelist"
        assert effective.roles == frozenset({"sales"})
        assert effective.grants.usage_caps == {"daily_tokens": 1000}
        assert effective.permissions == frozenset({"chat:use"})
        # Pure: inputs are unchanged.
        assert actor.deny.tools == frozenset({"patch"})

    def test_intersection_is_never_wider_than_either_side(self):
        policy = _load_policy(_policy())
        service, actor, effective = gov.resolve_principals(
            policy, service_email=SERVICE, actor_email=ACTOR, peer=PEER)
        for tool in ("read_file", "web_search", "terminal", "patch", "write_file"):
            if effective.allows("tools", tool):
                assert actor.allows("tools", tool) and service.allows("tools", tool), tool

    def test_serve_permission_is_mandatory(self, home, monkeypatch):
        write_policy(home, _policy(service_permissions=["chat:use"]))

        def check(base, adapter, seen):
            status, body = _post(base + "/", _send(), auth())
            assert status == 403
            assert body["error"]["code"] == protocol.ERR_FORBIDDEN
            assert seen == []

        _run_live(monkeypatch, check)
        assert _central_events(home)[-1]["context"]["reason"] == "serve_not_granted"

    def test_bare_wildcard_never_counts_as_a_serve_grant(self):
        policy_dict = _policy(service_permissions=["*"])
        policy_dict["bootstrap_admins"] = [SERVICE]
        policy = _load_policy(policy_dict)
        service, _actor, _effective = gov.resolve_principals(
            policy, service_email=SERVICE, actor_email=ACTOR, peer=PEER)
        assert not service.has_permission("a2a:serve:sales")
        assert gov.has_serve_permission(service, "sales") is False

        policy = _load_policy(_policy(service_permissions=["a2a:serve:*"]))
        service, _actor, _effective = gov.resolve_principals(
            policy, service_email=SERVICE, actor_email=ACTOR, peer=PEER)
        assert gov.has_serve_permission(service, "sales") is True

    def test_agent_outside_the_mapping_serve_list_refused(self, home):
        write_mapping(home, {PEER: _mapping(serve=["sales"])})
        admission = _admit()
        refused = gov.authorize_call(admission, rpc_method="SendMessage", operation="send",
                                     params=_send()["params"], agent={"slug": "", "local": True},
                                     headers={ACTOR_HEADER: assertion()})
        assert isinstance(refused, gov.Refusal) and refused.reason == "agent_not_served"

    def test_forwarded_profile_refused_under_enforce_until_in_process_routing(self, home):
        admission = _admit()
        refused = gov.authorize_call(admission, rpc_method="SendMessage", operation="send",
                                     params=_send()["params"], agent={"slug": "sales", "local": False},
                                     headers={ACTOR_HEADER: assertion()})
        assert isinstance(refused, gov.Refusal) and refused.reason == "forward_unbound"

    def test_allowed_call_carries_the_effective_principal_on_the_event(self, home, monkeypatch):
        def check(base, adapter, seen):
            status, body = _post(base + "/", _send("hello there"), auth())
            assert status == 200, body
            assert body["result"]["task"]["status"]["state"] == protocol.STATE_COMPLETED
            binding = seen[0].metadata[synthpulse_identity.A2A_BINDING_KEY]
            assert isinstance(binding, synthpulse_identity.A2ABinding)
            assert binding.peer == PEER == seen[0].source.user_id
            assert binding.actor_email == ACTOR
            assert binding.service_email == SERVICE
            assert binding.agent == "default"
            assert binding.org_scope == "org"
            assert binding.access.subject.email == ACTOR
            assert binding.service_access.subject.email == SERVICE
            assert not binding.access.allows("tools", "terminal")

        _run_live(monkeypatch, check)


# --------------------------------------------------------------------------
# Audit rows and the central feed
# --------------------------------------------------------------------------

class TestAudit:
    def test_allowed_call_audit_row_carries_actor_and_peer(self, home, monkeypatch):
        def check(base, adapter, seen):
            status, body = _post(base + "/", _send(), auth(depth=1))
            assert status == 200, body

        _run_live(monkeypatch, check)
        row = [r for r in _audit_rows(home) if r["event"] == "a2a_inbound"][-1]
        assert row["subject_email_hash"] == _hash(ACTOR)
        assert row["subject_user_id_hash"] == _hash(PEER)
        assert row["extra"]["peer"] == PEER
        assert row["extra"]["agent"] == "default"
        assert row["extra"]["depth"] == 1
        assert row["extra"]["decision"] == "allow"
        assert row["extra"]["rpc_method"] == "SendMessage"
        assert row["mode"] == "enforce"
        assert "task_id" in row["extra"]
        assert ACTOR not in json.dumps(row)
        assert TOKEN not in json.dumps(row) and SECRET not in json.dumps(row)
        assert _central_events(home) == []

    def test_refusal_goes_to_audit_and_central_feed_without_secrets(self, home, monkeypatch):
        def check(base, adapter, seen):
            status, _ = _post(base + "/", _send(), auth(ts=int(time.time()) - 900))
            assert status == 403

        _run_live(monkeypatch, check)
        row = [r for r in _audit_rows(home) if r["event"] == "a2a_inbound"][-1]
        assert row["extra"]["decision"] == "refuse"
        assert row["extra"]["peer"] == PEER
        assert row["reason"] == "actor_stale"
        event = _central_events(home)[-1]
        assert event["kind"] == "a2a_refused"
        assert event["service"] == "engine"
        assert event["level"] == "warning"
        assert event["fingerprint"] == "a2a_refused:actor_stale"
        assert event["context"]["peer"] == PEER
        text = (home / "logs" / gov.CENTRAL_EVENTS_FILE).read_text()
        assert SECRET not in text and ACTOR not in text and SERVICE not in text
        assert oct((home / "logs" / gov.CENTRAL_EVENTS_FILE).stat().st_mode & 0o777) == "0o600"

    def test_central_feed_file_is_bounded(self, home, monkeypatch):
        monkeypatch.setattr(gov, "_CENTRAL_EVENTS_MAX_BYTES", 200)
        for _ in range(10):
            gov.record_decision("refuse", "unmapped_peer", peer=PEER)
        path = home / "logs" / gov.CENTRAL_EVENTS_FILE
        assert path.stat().st_size < 1000
        assert path.with_name(path.name + ".1").exists()


# --------------------------------------------------------------------------
# Exposure: health topology and metrics
# --------------------------------------------------------------------------

class TestExposure:
    def test_health_topology_only_for_admitted_peers_even_on_loopback(self, home, monkeypatch):
        def check(base, adapter, seen):
            status, payload = _get(base + "/health")
            assert status == 200 and "served_agents" not in payload
            status, payload = _get(base + "/health", {"Authorization": f"Bearer {TOKEN}"})
            assert "served_agents" in payload

        _run_live(monkeypatch, check)

        monkeypatch.delenv("A2A_PEER_TOKENS", raising=False)

        def check_no_tokens(base, adapter, seen):
            status, payload = _get(base + "/health")
            assert status == 200 and "served_agents" not in payload

        _run_live(monkeypatch, check_no_tokens)

    def test_metrics_need_an_admitted_peer(self, home, monkeypatch):
        def check(base, adapter, seen):
            status, _ = _get(base + "/metrics")
            assert status == 401
            status, payload = _get(base + "/metrics", {"Authorization": f"Bearer {TOKEN}"})
            assert status == 200 and "inbound_total" in payload

        _run_live(monkeypatch, check)
