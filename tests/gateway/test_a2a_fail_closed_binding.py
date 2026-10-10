"""``GatewayRunner._handle_message`` fails closed for A2A (PLAN 4.3, 4.5 E2).

An A2A turn runs only under the principal the adapter resolved when it
admitted the call (``A2ABinding`` on the event). Without it, with a forged or
stale one, the turn never reaches the agent. Other platforms keep the
sender-map behaviour (fail open for unmapped senders).
"""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from gateway import synthpulse_identity as spi
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.dashboard_governance.context import current_governance_context

PEER = "acme-inst_a"
SERVICE = "svc-a2a-inst-a@acme.example"
ACTOR = "rep.sales@acme.example"


def _policy(mode="enforce"):
    return {
        "version": 1, "mode": mode, "default_effect": "deny",
        "organization": {"id": "org_acme", "name": "Acme"},
        "roles": {"svc": {"grants": {"permissions": ["a2a:serve:default", "chat:use"],
                                     "tools": {"builtins": ["read_file", "terminal"]}}}},
        "users": {
            SERVICE: {"roles": ["svc"]},
            ACTOR: {"grants": {"permissions": ["chat:use"], "tools": {"builtins": ["read_file"]}}},
        },
    }


def _mapping_row():
    return {"email": SERVICE, "org_id": "org_acme", "caller_installation": "inst_a", "serve": ["default"]}


@pytest.fixture
def home(monkeypatch):
    from hermes_constants import get_hermes_home

    root = get_hermes_home()
    for name in ("SYNTHPULSE_GATEWAY_IDENTITIES", "SP_GATEWAY_IDENTITIES_JSON"):
        monkeypatch.delenv(name, raising=False)
    write_policy(root, _policy())
    write_mapping(root, {"a2a": {PEER: _mapping_row()}})
    return root


def write_policy(root, policy):
    (root / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")


def write_mapping(root, data):
    path = root / "webui" / "gateway-identities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    spi._CACHE.update(path="", mtime=None, data={})


def _event(platform="a2a", user_id=PEER, binding=None, metadata=None):
    source = SessionSource(platform=Platform(platform), chat_id="ctx-1", chat_type="dm",
                           user_id=user_id, user_name=user_id)
    meta = dict(metadata or {})
    if binding is not None:
        meta[spi.A2A_BINDING_KEY] = binding
    return MessageEvent(text="hello", message_type=MessageType.TEXT, source=source,
                        message_id="task-1", metadata=meta)


def _binding(**overrides):
    from hermes_cli.dashboard_governance.loader import parse_governance_policy
    from hermes_cli.dashboard_governance.models import GovernanceSubject
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access

    policy = parse_governance_policy(_policy())
    service = resolve_effective_access(policy, GovernanceSubject(email=SERVICE, user_id=PEER))
    actor = resolve_effective_access(policy, GovernanceSubject(email=ACTOR, user_id=PEER))
    values = dict(peer=PEER, agent="default", service_email=SERVICE, actor_email=ACTOR,
                  org_id="org_acme", org_scope="org", caller_installation="inst_a", depth=0,
                  access=actor, service_access=service)
    values.update(overrides)
    return spi.A2ABinding(**values)


def _runner(record):
    runner = GatewayRunner.__new__(GatewayRunner)

    async def unbound(event):
        ctx = current_governance_context()
        record.append(ctx)
        if ctx is not None and ctx.bot_access_check is not None:
            record.append(("ceiling_check", ctx.bot_access_check()))
        return "ran"

    runner._handle_message_unbound = unbound  # type: ignore[assignment]
    return runner


class TestA2AFailsClosed:
    def test_a2a_event_without_binding_never_runs(self, home):
        record = []
        with pytest.raises(spi.A2ABindingRefused) as info:
            asyncio.run(_runner(record)._handle_message(_event()))
        assert info.value.reason == "binding_missing"
        assert record == []

    def test_mapped_a2a_sender_without_binding_never_runs(self, home):
        # Even a peer whose token name is in gateway-identities.json["a2a"]
        # is not bound through the sender map: the adapter's binding is the
        # only way in.
        record = []
        with pytest.raises(spi.A2ABindingRefused):
            asyncio.run(_runner(record)._handle_message(_event()))
        assert record == []
        assert spi.bind_for_source(_event().source) is None

    def test_forged_binding_of_another_type_refused(self, home):
        record = []
        forged = {"peer": PEER, "agent": "default", "actor_email": ACTOR}
        with pytest.raises(spi.A2ABindingRefused) as info:
            asyncio.run(_runner(record)._handle_message(_event(metadata={spi.A2A_BINDING_KEY: forged})))
        assert info.value.reason == "binding_missing"
        assert record == []

    def test_binding_for_another_peer_refused(self, home):
        record = []
        with pytest.raises(spi.A2ABindingRefused) as info:
            asyncio.run(_runner(record)._handle_message(_event(user_id="other-peer", binding=_binding())))
        assert info.value.reason == "binding_peer_mismatch"
        assert record == []

    def test_binding_without_principal_refused(self, home):
        record = []
        with pytest.raises(spi.A2ABindingRefused) as info:
            asyncio.run(_runner(record)._handle_message(_event(binding=_binding(access=None))))
        assert info.value.reason == "binding_incomplete"
        assert record == []

    def test_binding_whose_mapping_was_removed_refused(self, home):
        write_mapping(home, {"a2a": {}})
        record = []
        with pytest.raises(spi.A2ABindingRefused) as info:
            asyncio.run(_runner(record)._handle_message(_event(binding=_binding())))
        assert info.value.reason == "mapping_changed"
        assert record == []

    def test_valid_binding_runs_under_the_bound_principal(self, home):
        record = []
        binding = _binding()
        result = asyncio.run(_runner(record)._handle_message(_event(binding=binding)))
        assert result == "ran"
        ctx = record[0]
        assert ctx.subject.email == ACTOR
        assert ctx.access is binding.access
        assert ctx.bot_access_ceiling is binding.service_access
        assert not ctx.access.allows("tools", "terminal")
        assert ctx.access.allows("tools", "read_file")
        assert record[1] == ("ceiling_check", True)
        assert current_governance_context() is None

    def test_live_ceiling_check_follows_the_mapping(self, home):
        seen = {}

        async def unbound(event):
            ctx = current_governance_context()
            seen["before"] = ctx.bot_access_check()
            write_mapping(home, {"a2a": {PEER: dict(_mapping_row(), email="someone-else@acme.example")}})
            seen["after"] = ctx.bot_access_check()
            return "ran"

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._handle_message_unbound = unbound  # type: ignore[assignment]
        asyncio.run(runner._handle_message(_event(binding=_binding())))
        assert seen == {"before": True, "after": False}

    def test_unauthenticated_local_binding_refused_under_enforce(self, home):
        record = []
        dev = spi.A2ABinding(peer=PEER, agent="default", unauthenticated_local=True)
        with pytest.raises(spi.A2ABindingRefused) as info:
            asyncio.run(_runner(record)._handle_message(_event(binding=dev)))
        assert info.value.reason == "dev_flag_under_enforce"
        assert record == []

    def test_unauthenticated_local_binding_runs_unbound_when_not_enforced(self, home):
        write_policy(home, _policy("off"))
        record = []
        dev = spi.A2ABinding(peer=PEER, agent="default", unauthenticated_local=True)
        assert asyncio.run(_runner(record)._handle_message(_event(binding=dev))) == "ran"
        assert record == [None]

    def test_other_platforms_keep_the_fail_open_sender_map(self, home):
        record = []
        assert asyncio.run(_runner(record)._handle_message(_event(platform="telegram", user_id="12345"))) == "ran"
        assert record == [None]


class TestThroughTheAdapter:
    def test_adapter_task_without_binding_fails_instead_of_running(self, home, monkeypatch):
        """The real A2A adapter and base dispatch: an event built without a
        binding reaches ``_handle_message``, is refused there, and the task
        ends FAILED without the agent ever running."""
        from gateway.config import PlatformConfig
        from plugins.platforms.a2a import protocol
        from plugins.platforms.a2a.adapter import A2AAdapter

        monkeypatch.setenv("A2A_PORT", "0")
        record = []
        runner = _runner(record)
        adapter = A2AAdapter(PlatformConfig(enabled=True))
        adapter.set_message_handler(runner._handle_message)

        async def run():
            adapter._loop = asyncio.get_running_loop()
            params = {"message": protocol.text_message(protocol.ROLE_USER, "hi", context_id="ctx-x")}
            terminal, pending = await asyncio.to_thread(adapter._prepare_task, params, PEER)
            assert terminal is None
            return await asyncio.to_thread(adapter._await_reply, pending)

        state, reply = asyncio.run(run())
        assert state == protocol.STATE_FAILED
        assert reply == "[agent processing failed]"
        assert record == []


def test_binding_refuses_profile_remap(home):
    binding = _binding()
    row = _mapping_row()
    row["profile"] = "other"
    write_mapping(home, {"a2a": {PEER: row}})
    with pytest.raises(spi.A2ABindingRefused, match="mapping changed"):
        spi.bind_for_a2a_event(_event(binding=binding))


def test_binding_refuses_missing_profile(home, monkeypatch):
    row = _mapping_row()
    row["profile"] = "missing-profile"
    write_mapping(home, {"a2a": {PEER: row}})
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _: False)
    with pytest.raises(spi.A2ABindingRefused) as info:
        spi.bind_for_a2a_event(_event(binding=_binding(profile="missing-profile")))
    assert info.value.reason == "profile_missing"
