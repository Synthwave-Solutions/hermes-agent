"""Per-peer task scoping for inbound A2A (PLAN 4.3 task store, 4.5 E2).

A peer only ever sees its own tasks: GetTask, ListTasks, CancelTask and
SubscribeToTask answer "not found" for another peer's task, and a context id
stays with the peer that opened it. Unit tests drive ``TaskStore`` directly;
the live tests run two mapped peers against the real adapter HTTP server.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

from gateway import synthpulse_identity
from plugins.platforms.a2a import protocol
from plugins.platforms.a2a.synthpulse_assertion import ACTOR_HEADER, sign_actor

SECRET_A = "peerAsecretValue_0123456789"
SECRET_B = "peerBsecretValue_9876543210"
TOKEN_A = f"a2a1.org_acme.inst_a.inst_c.{SECRET_A}"
TOKEN_B = f"a2a1.org_acme.inst_b.inst_c.{SECRET_B}"
SERVICE_A = "svc-a2a-inst-a@acme.example"
SERVICE_B = "svc-a2a-inst-b@acme.example"
ACTOR = "rep.sales@acme.example"


# --------------------------------------------------------------------------
# TaskStore
# --------------------------------------------------------------------------

class TestTaskStorePeerScope:
    def _store(self):
        store = protocol.TaskStore()
        store.create("task-a", "ctx-a", "peer-a")
        store.create("task-b", "ctx-b", "peer-b")
        store.complete("task-a", protocol.STATE_COMPLETED, "answer for a")
        return store

    def test_explicit_peer_hides_other_peers_tasks(self):
        store = self._store()
        assert store.get("task-a", peer="peer-a")["reply"] == "answer for a"
        assert store.get("task-a", peer="peer-b") is None
        assert store.watch("task-a", peer="peer-b") is None
        assert store.watch("task-a", peer="peer-a") is not None
        ids = [r["task_id"] for r in store.list(peer="peer-b")[0]]
        assert ids == ["task-b"]

    def test_request_peer_scope_applies_to_every_read(self):
        store = self._store()
        with store.peer_scope("peer-b"):
            assert protocol.current_request_peer() == "peer-b"
            assert store.get("task-a") is None
            assert store.watch("task-a") is None
            assert [r["task_id"] for r in store.list()[0]] == ["task-b"]
            assert store.list(with_total=True)[2] == 1
            assert store.set_push_config("task-a", "https://example.com/h") is None
            assert store.get_push_config("task-a") is None
            assert store.list_push_configs("task-a") == []
            assert store.delete_push_config("task-a") is False
        assert protocol.current_request_peer() is None
        assert store.get("task-a")["task_id"] == "task-a"  # internal reads stay unscoped

    def test_peer_scope_is_per_thread(self):
        store = self._store()
        seen = {}

        def other_thread():
            seen["other"] = store.get("task-a")

        with store.peer_scope("peer-b"):
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
            assert store.get("task-a") is None
        assert seen["other"] is not None

    def test_scope_combines_with_tenant(self):
        store = protocol.TaskStore()
        store.create("t1", "c1", "peer-a", "research", "research")
        assert store.get("t1", "research", "research", peer="peer-a") is not None
        assert store.get("t1", "dev", "dev", peer="peer-a") is None
        assert store.get("t1", "research", "research", peer="peer-b") is None

    def test_context_stays_with_the_peer_that_opened_it(self):
        store = protocol.TaskStore()
        assert store.claim_context("ctx-shared", "peer-a") is True
        assert store.claim_context("ctx-shared", "peer-a") is True
        assert store.claim_context("ctx-shared", "peer-b") is False
        store.create("task-x", "ctx-new", "peer-a")
        assert store.context_owner("ctx-new") == "peer-a"
        assert store.claim_context("ctx-new", "peer-b") is False
        assert store.claim_context("", "peer-b") is True

    def test_context_owners_are_bounded(self, monkeypatch):
        monkeypatch.setattr(protocol.TaskStore, "_MAX_CONTEXT_OWNERS", 3)
        store = protocol.TaskStore()
        for i in range(5):
            assert store.claim_context(f"ctx-{i}", "peer-a")
        assert store.context_owner("ctx-0") is None
        assert store.context_owner("ctx-4") == "peer-a"


# --------------------------------------------------------------------------
# Live: two mapped peers
# --------------------------------------------------------------------------

@pytest.fixture
def two_peers(monkeypatch):
    from hermes_constants import get_hermes_home

    root = get_hermes_home()
    monkeypatch.setenv("A2A_PEER_TOKENS", f"peer-a:{TOKEN_A},peer-b:{TOKEN_B}")
    monkeypatch.setenv("SP_INSTALLATION_ID", "inst_c")
    for name in ("A2A_BEARER_TOKEN", "A2A_ALLOW_UNAUTHENTICATED_LOCAL", "A2A_PUBLIC_URL",
                 "A2A_TRUSTED_PEERS", "SYNTHPULSE_GATEWAY_IDENTITIES", "SP_GATEWAY_IDENTITIES_JSON"):
        monkeypatch.delenv(name, raising=False)
    policy = {
        "version": 1, "mode": "enforce", "default_effect": "deny",
        "organization": {"id": "org_acme", "name": "Acme"},
        "roles": {"svc": {"grants": {"permissions": ["a2a:serve:default", "chat:use"]}}},
        "users": {SERVICE_A: {"roles": ["svc"]}, SERVICE_B: {"roles": ["svc"]}},
    }
    (root / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    rows = {
        "peer-a": {"email": SERVICE_A, "org_id": "org_acme", "caller_installation": "inst_a", "serve": ["default"]},
        "peer-b": {"email": SERVICE_B, "org_id": "org_acme", "caller_installation": "inst_b", "serve": ["default"]},
    }
    path = root / "webui" / "gateway-identities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"a2a": rows}), encoding="utf-8")
    synthpulse_identity._CACHE.update(path="", mtime=None, data={})
    return root


def _headers(token, caller, task=""):
    return {
        "Authorization": f"Bearer {token}",
        ACTOR_HEADER: sign_actor(token, {"actor": ACTOR, "caller": caller, "depth": 0,
                                         "task": task, "ts": int(time.time())}),
    }


def peer_a(task=""):
    return _headers(TOKEN_A, "inst_a", task)


def peer_b(task=""):
    return _headers(TOKEN_B, "inst_b", task)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url, body, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            if r.headers.get("Content-Type", "").startswith("text/event-stream"):
                return r.status, raw
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _rpc(method, params, rid="1"):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def _send(text, ctx=""):
    return _rpc("SendMessage", {"message": protocol.text_message(protocol.ROLE_USER, text, context_id=ctx)})


def _run_live(monkeypatch, fn):
    from gateway.config import PlatformConfig
    from plugins.platforms.a2a.adapter import A2AAdapter

    port = _free_port()
    monkeypatch.setenv("A2A_PORT", str(port))
    adapter = A2AAdapter(PlatformConfig(enabled=True))
    seen = []

    async def fake_handle_message(event):
        seen.append(event)
        await adapter.send(event.source.chat_id, "reply to " + event.source.user_id, metadata={"notify": True})

    adapter.handle_message = fake_handle_message  # type: ignore[assignment]
    adapter._message_handler = object()

    async def run():
        assert await adapter.connect() is True
        try:
            return await asyncio.to_thread(fn, f"http://127.0.0.1:{port}", adapter, seen)
        finally:
            await adapter.disconnect()

    return asyncio.run(run())


class TestLivePeerIsolation:
    def test_peer_b_cannot_read_peer_a_task(self, two_peers, monkeypatch):
        def check(base, adapter, seen):
            status, body = _post(base + "/", _send("secret question", ctx="ctx-peer-a"), peer_a())
            assert status == 200, body
            task = body["result"]["task"]
            tid = task["id"]
            assert adapter.tasks.get(tid)["peer"] == "peer-a"

            # Peer A sees its own task.
            status, body = _post(base + "/", _rpc("GetTask", {"id": tid}), peer_a(tid))
            assert body["result"]["id"] == tid

            # Peer B does not, by any method.
            status, body = _post(base + "/", _rpc("GetTask", {"id": tid}), peer_b(tid))
            assert body["error"]["code"] == protocol.ERR_TASK_NOT_FOUND
            status, body = _post(base + "/", _rpc("tasks/get", {"taskId": tid}), peer_b(tid))
            assert body["error"]["code"] == protocol.ERR_TASK_NOT_FOUND
            status, body = _post(base + "/", _rpc("CancelTask", {"id": tid}), peer_b(tid))
            assert body["error"]["code"] == protocol.ERR_TASK_NOT_FOUND
            status, body = _post(base + "/", _rpc("SubscribeToTask", {"id": tid}), peer_b(tid))
            assert status == 200
            assert body["error"]["code"] == protocol.ERR_TASK_NOT_FOUND

            status, body = _post(base + "/", _rpc("ListTasks", {}), peer_b())
            assert body["result"]["tasks"] == []
            assert body["result"]["totalSize"] == 0
            status, body = _post(base + "/", _rpc("ListTasks", {"contextId": "ctx-peer-a"}), peer_b())
            assert body["result"]["tasks"] == []

            status, body = _post(base + "/", _rpc("ListTasks", {}), peer_a())
            assert [t["id"] for t in body["result"]["tasks"]] == [tid]

            # Peer A can still subscribe to its own finished task.
            status, raw = _post(base + "/", _rpc("SubscribeToTask", {"id": tid}), peer_a(tid))
            assert status == 200 and "TASK_STATE_COMPLETED" in raw

        _run_live(monkeypatch, check)

    def test_peer_b_cannot_continue_peer_a_context(self, two_peers, monkeypatch):
        def check(base, adapter, seen):
            status, body = _post(base + "/", _send("first", ctx="ctx-owned"), peer_a())
            assert status == 200, body
            status, body = _post(base + "/", _send("hijack", ctx="ctx-owned"), peer_b())
            assert status == 403
            assert body["error"]["code"] == protocol.ERR_FORBIDDEN
            assert [e.source.user_id for e in seen] == ["peer-a"]
            status, body = _post(base + "/", _send("again", ctx="ctx-owned"), peer_a())
            assert status == 200, body

        _run_live(monkeypatch, check)

    def test_generated_context_belongs_to_its_peer(self, two_peers, monkeypatch):
        def check(base, adapter, seen):
            status, body = _post(base + "/", _send("no context given"), peer_a())
            ctx = body["result"]["task"]["contextId"]
            assert adapter.tasks.context_owner(ctx) == "peer-a"
            status, body = _post(base + "/", _send("follow up", ctx=ctx), peer_b())
            assert status == 403

        _run_live(monkeypatch, check)
