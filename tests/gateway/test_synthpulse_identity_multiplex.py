"""SynthPulse: a mapped platform user's turn runs as that person, also under multiplexing.

The fork ran a mapped sender's turn inside their own profile (config, memory, sessions) by
entering that profile's runtime scope around ``_handle_message``. Upstream's multiplexer now
pins one frozen ``RoutingIdentity`` per event at adapter ingress and re-enters every later scope
from it, so a scope entered in the wrapper was overridden: the turn ran (and keyed its session)
in the shared default profile. These tests pin the restored behaviour on top of upstream's
architecture, using the real ``GatewayRunner`` resolvers:

* a mapped sender on the shared bot is ROUTED to their own profile (identity, session key,
  runtime home, governance) when no ``profile_routes`` entry matched;
* unmapped senders, explicit routes and dedicated secondary bots keep upstream routing
  (the fork's precedence);
* a mapping whose profile cannot be applied (invalid name, not served, not existing, or an
  identity pinned without the routing step) drops the event instead of falling through.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.profile_routing import ProfileRouteRejected, parse_profile_routes
from gateway.session import SessionSource
from gateway.session_identity import IdentityUnresolved, identity_of, resolve_identity, restore_identity
from hermes_constants import get_hermes_home


class _Stub(BasePlatformAdapter):
    pass


_Stub.__abstractmethods__ = frozenset()

ALICE, GHOST, BAD, PLAIN, ROUTED, RESTORED = "111", "444", "445", "333", "72719239", "555"


def _stub(platform, runner, label):
    adapter = _Stub.__new__(_Stub)
    adapter.platform, adapter.gateway_runner, adapter.label = platform, runner, label
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._pending_messages, adapter._active_sessions = {}, {}
    return adapter


def _runner(*, multiplex, routes=()):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    runner.config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes(list(routes))
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"
    primary = _stub(Platform.TELEGRAM, runner, "PRIMARY")
    team_b = _stub(Platform.TELEGRAM, runner, "TEAM_B")
    team_b.set_owner_profile("team_b")
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {"team_b": {Platform.TELEGRAM: team_b}, "ops": {}, "alice": {}}
    return SimpleNamespace(runner=runner, primary=primary, team_b=team_b)


def _write_identities(path: Path) -> None:
    def person(email, profile=None):
        row = {"email": email, "name": email.split("@")[0]}
        if profile is not None:
            row["profile"] = profile
        return row

    path.write_text(json.dumps({"telegram": {
        ALICE: person("alice@example.com", "alice"),
        ROUTED: person("alice@example.com", "alice"),
        RESTORED: person("alice@example.com", "alice"),
        GHOST: person("ghost@example.com", "ghost"),
        BAD: person("bad@example.com", "../escape"),
        PLAIN: person("plain@example.com"),
    }}), encoding="utf-8")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    for name in ("ops", "team_b", "alice"):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    identities = tmp_path / "gateway-identities.json"
    _write_identities(identities)
    monkeypatch.setenv("SYNTHPULSE_GATEWAY_IDENTITIES", str(identities))
    monkeypatch.delenv("SP_GATEWAY_IDENTITIES_JSON", raising=False)
    served = [(name, home if name == "default" else home / "profiles" / name)
              for name in ("default", "ops", "team_b", "alice")]
    existing = {"default", "ops", "team_b", "alice"}
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=served), \
            patch("hermes_cli.profiles.get_profile_dir",
                  side_effect=lambda n: home if n == "default" else home / "profiles" / n), \
            patch("hermes_cli.profiles.profile_exists", side_effect=lambda n: n in existing):
        yield home


@pytest.fixture
def mux(homes):
    rig = _runner(multiplex=True, routes=[
        {"name": "admin-dm", "platform": "telegram", "profile": "ops", "chat_id": ROUTED},
    ])
    rig.home = homes
    return rig


def _capture(rig):
    """Replace the turn body: record where it runs and who governance says it runs as."""
    from hermes_cli.dashboard_governance.context import current_governance_context

    seen = []

    async def body(event):
        ctx = current_governance_context()
        seen.append(SimpleNamespace(
            home=Path(get_hermes_home()), profile=event.source.profile,
            key=rig.runner._session_key_for_source(event.source),
            email=getattr(getattr(ctx, "subject", None), "email", None),
            active_profile=getattr(ctx, "active_profile", None)))
        return "ok"

    rig.runner._handle_message_unbound = body
    return seen


def _event(source):
    return MessageEvent(text="hi", source=source)


# ── mapped user under multiplex runs in their own profile ────────────────────────────────────


def test_mapped_user_on_shared_bot_is_routed_into_own_profile(mux):
    source = mux.primary.build_source(chat_id=ALICE, chat_type="dm", user_id=ALICE)
    assert source.profile == "alice" and source.profile_route_rejected is False

    identity = resolve_identity(source, runner=mux.runner)
    assert (identity.transport_profile, identity.runtime_profile) == ("default", "alice")
    # Admitted by the shared bot's allowlist, but runs (memory, config, state.db) in alice's home.
    assert identity.authorization_home == mux.home
    assert identity.runtime_home == mux.home / "profiles" / "alice"
    assert identity.store_path == mux.home / "profiles" / "alice" / "state.db"
    # Adapter-side (busy path) and runner-side keys agree on alice's namespace.
    assert mux.primary._source_session_key(source) == "agent:alice:telegram:dm:" + ALICE
    assert mux.runner._session_key_for_source(source) == mux.primary._source_session_key(source)
    assert mux.runner._resolve_profile_home_for_source(source) == mux.home / "profiles" / "alice"


def test_mapped_user_turn_runs_inside_own_profile_with_governance(mux):
    seen = _capture(mux)
    source = mux.primary.build_source(chat_id=ALICE, chat_type="dm", user_id=ALICE)

    assert asyncio.run(mux.runner._primary_message_handler()(_event(source))) == "ok"

    assert len(seen) == 1
    turn = seen[0]
    assert turn.home == mux.home / "profiles" / "alice"
    assert turn.profile == "alice"
    assert turn.key == "agent:alice:telegram:dm:" + ALICE
    assert (turn.email, turn.active_profile) == ("alice@example.com", "alice")
    assert Path(get_hermes_home()) == mux.home  # scope unwound


def test_internal_source_without_build_source_is_routed_at_runner_ingress(mux):
    """Paths that construct ``SessionSource`` directly are canonicalized by the wrapper first."""
    seen = _capture(mux)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=ALICE, chat_type="dm", user_id=ALICE)

    assert asyncio.run(mux.runner._handle_message(_event(source))) == "ok"

    assert identity_of(source).runtime_profile == "alice"
    assert seen[0].profile == "alice" and seen[0].key == "agent:alice:telegram:dm:" + ALICE
    assert seen[0].active_profile == "alice"


# ── unmapped events keep upstream routing ────────────────────────────────────────────────────


def test_unmapped_sender_keeps_default_routing(mux):
    seen = _capture(mux)
    source = mux.primary.build_source(chat_id="222", chat_type="dm", user_id="222")
    assert source.profile is None

    identity = resolve_identity(source, runner=mux.runner)
    assert (identity.transport_profile, identity.runtime_profile) == ("default", "default")
    assert identity.runtime_home == mux.home

    assert asyncio.run(mux.runner._primary_message_handler()(_event(source))) == "ok"
    assert seen[0].home == mux.home and seen[0].key == "agent:main:telegram:dm:222"
    assert seen[0].email is None  # no governance principal for an unmapped sender


def test_explicit_route_and_dedicated_bot_keep_their_profile_for_mapped_sender(mux):
    """Fork precedence: an explicit ``profile_routes`` match and a secondary profile's own bot win
    over the person mapping; governance still names the person and the profile actually running."""
    routed = mux.primary.build_source(chat_id=ROUTED, chat_type="dm", user_id=ROUTED)
    assert resolve_identity(routed, runner=mux.runner).runtime_profile == "ops"

    own = mux.team_b.build_source(chat_id=ALICE, chat_type="dm", user_id=ALICE)
    assert own.profile == "team_b"
    identity = resolve_identity(own, runner=mux.runner, transport_profile="team_b")
    assert (identity.transport_profile, identity.runtime_profile) == ("team_b", "team_b")
    assert mux.runner._profile_name_for_source(own, adapter_profile="team_b") is None

    seen = _capture(mux)
    assert asyncio.run(mux.runner._make_profile_message_handler("team_b")(_event(own))) == "ok"
    assert seen[0].home == mux.home / "profiles" / "team_b"
    assert (seen[0].email, seen[0].active_profile) == ("alice@example.com", "team_b")


def test_governance_only_mapping_keeps_default_routing(mux):
    seen = _capture(mux)
    source = mux.primary.build_source(chat_id=PLAIN, chat_type="dm", user_id=PLAIN)
    assert source.profile is None

    assert asyncio.run(mux.runner._primary_message_handler()(_event(source))) == "ok"
    assert seen[0].home == mux.home and seen[0].key == "agent:main:telegram:dm:" + PLAIN
    assert (seen[0].email, seen[0].active_profile) == ("plain@example.com", "default")


def test_routing_step_is_inert_without_multiplexing(homes):
    solo = _runner(multiplex=False)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=ALICE, chat_type="dm", user_id=ALICE)
    assert solo.runner._profile_name_for_source(source) is None


# ── invalid / unknown mapping fails closed ───────────────────────────────────────────────────


@pytest.mark.parametrize("user_id", [GHOST, BAD], ids=["unserved-profile", "invalid-profile-name"])
def test_unservable_mapping_rejects_the_route_and_is_dropped(mux, user_id):
    source = mux.primary.build_source(chat_id=user_id, chat_type="dm", user_id=user_id)
    assert source.profile is None and source.profile_route_rejected is True
    with pytest.raises(IdentityUnresolved):
        resolve_identity(source, runner=mux.runner)
    assert mux.runner._admit_primary_source(source, mux.home) is None
    assert mux.primary._drop_unresolved(_event(source)) is True
    # Runner ingress gate: dropped before auth, hooks or any session state.
    assert asyncio.run(mux.runner._hm_admit_event(_event(source))) is None

    bare = SessionSource(platform=Platform.TELEGRAM, chat_id=user_id, chat_type="dm", user_id=user_id)
    with pytest.raises(ProfileRouteRejected):
        mux.runner._profile_name_for_source(bare)


def test_served_set_failure_fails_closed(mux):
    bare = SessionSource(platform=Platform.TELEGRAM, chat_id=ALICE, chat_type="dm", user_id=ALICE)
    with patch("gateway.run._multiplex_profile_homes", side_effect=OSError("profiles root unreadable")):
        with pytest.raises(ProfileRouteRejected):
            mux.runner._profile_name_for_source(bare)


def test_identity_pinned_without_routing_step_is_dropped_not_run_in_shared_profile(mux):
    """A source restored from durable state pins its identity without routing; for a mapped
    person that would be the shared default profile, so the wrapper drops it."""
    seen = _capture(mux)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=RESTORED, chat_type="dm", user_id=RESTORED)
    identity = restore_identity(source, runner=mux.runner, transport_profile="default")
    assert identity.runtime_profile == "default"

    assert asyncio.run(mux.runner._handle_message(_event(source))) is None
    assert seen == []


# ── standalone gateway (fork behaviour, plus fail closed for an unknown profile) ─────────────


def test_standalone_mapped_user_runs_in_own_profile(homes):
    solo = _runner(multiplex=False)
    seen = _capture(solo)
    source = solo.primary.build_source(chat_id=ALICE, chat_type="dm", user_id=ALICE)

    assert asyncio.run(solo.runner._primary_message_handler()(_event(source))) == "ok"
    assert seen[0].home == homes / "profiles" / "alice"
    assert seen[0].profile == "alice"
    assert (seen[0].email, seen[0].active_profile) == ("alice@example.com", "alice")
    assert Path(get_hermes_home()) == homes


@pytest.mark.parametrize("user_id", [GHOST, BAD], ids=["missing-profile", "invalid-profile-name"])
def test_standalone_unknown_profile_is_dropped(homes, user_id):
    solo = _runner(multiplex=False)
    seen = _capture(solo)
    source = solo.primary.build_source(chat_id=user_id, chat_type="dm", user_id=user_id)

    assert asyncio.run(solo.runner._primary_message_handler()(_event(source))) is None
    assert seen == []


def test_standalone_governance_only_mapping_runs_on_gateway_profile(homes):
    solo = _runner(multiplex=False)
    seen = _capture(solo)
    source = solo.primary.build_source(chat_id=PLAIN, chat_type="dm", user_id=PLAIN)

    assert asyncio.run(solo.runner._primary_message_handler()(_event(source))) == "ok"
    assert seen[0].home == homes and seen[0].profile is None
    assert seen[0].email == "plain@example.com"
