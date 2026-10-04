"""SynthPulse: run a mapped platform user's turn as that person.

The WebUI (Connections, Messaging channels) keeps a map of platform users to
people on this workstation:

    {platform: {user_id: {"email": ..., "name": ..., "profile": ...}}}

Read from, in order: ``SYNTHPULSE_GATEWAY_IDENTITIES`` (a path; at a client
this is the WebUI volume mounted read-only into the core container),
``$HERMES_HOME/webui/gateway-identities.json`` (single host), and the inline
seed ``SP_GATEWAY_IDENTITIES_JSON`` rendered from client.yaml ``people[].channels``.

For a mapped sender two things happen for the duration of the turn:

1. The person's dashboard governance is bound (``DashboardGovernanceContext``),
   so tools, files, models and CLI follow the same policy as in the browser.
2. When the mapping names a profile, the turn runs inside that profile
   (config, skills, memory, sessions, and that profile's .env as credential
   overlay) and ``source.profile`` carries it, so the session key lives in that
   person's namespace. The bot's own transport credentials and allowlist stay
   with the receiving bot's profile; nothing else of the shared profile leaks
   into the person's turn.

How (2) happens depends on the gateway:

* Standalone (``multiplex_profiles`` off): ``bind_for_source`` enters the
  person's ``_profile_runtime_scope`` around the whole turn.
* Multiplexed: the turn's runtime profile is decided ONCE at ingress by the
  frozen ``RoutingIdentity`` (``gateway/session_identity.py``), and every later
  scope (session lookup, transcript, agent run, persistence, busy path) is
  re-entered from it. A scope entered here would be overridden by those, so the
  person's profile is applied as a routing step instead:
  ``GatewayRunner._profile_name_for_source`` calls ``profile_route_for_source``
  after the configured ``profile_routes`` found no match. Precedence is the
  fork's: an explicit ``profile_routes`` match and a dedicated (secondary)
  profile bot keep their own profile; only an event that would otherwise fall
  through to the shared default bot's profile is re-homed into the person's.

Fail-closed rules for a mapping that names a profile (``fail_closed_reason``):
an invalid profile name, a profile that does not exist, or (multiplexed) a
profile this gateway does not serve drops the event instead of running the
person's turn with the shared profile's memory and sessions. A mapping without
a profile (or ``profile: default``) is governance-only, as before.

Unmapped senders keep the gateway's existing behaviour (allowlists, pairing).
An unreadable map counts as "no mapping" (the sender then still has to pass the
normal allowlists), and governance binding fails open and logs once per error
type, so a broken map can never take the gateway down.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("gateway.synthpulse_identity")

_CACHE = {"path": "", "mtime": None, "data": {}, "loaded_at": 0.0}
_WARNED = set()
_USER_ID_RE = re.compile(r"^[A-Za-z0-9@._:+\-]{1,120}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ROUTE_NAME = "synthpulse-identity"


def _identities_path() -> Path:
    override = os.getenv("SYNTHPULSE_GATEWAY_IDENTITIES")
    if override:
        return Path(override).expanduser()
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    return home / "webui" / "gateway-identities.json"


def _inline_seed() -> dict:
    raw = os.getenv("SP_GATEWAY_IDENTITIES_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _load() -> dict:
    path = _identities_path()
    try:
        st = path.stat()
    except OSError:
        return _inline_seed()
    if _CACHE["path"] == str(path) and _CACHE["mtime"] == st.st_mtime_ns:
        return _CACHE["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    _CACHE.update(path=str(path), mtime=st.st_mtime_ns, data=data, loaded_at=time.time())
    return data


def _normalize(platform: str, user_id) -> str:
    value = str(user_id or "").strip()
    if platform == "google_chat" or "@" in value:
        value = value.lower()
    if platform.startswith("whatsapp"):
        value = value.lstrip("+").replace(" ", "")
    return value if _USER_ID_RE.match(value) else ""


def _platform_name(platform) -> str:
    name = platform.value if hasattr(platform, "value") else str(platform or "")
    return name.strip().lower()


def mapping_for(platform, user_id) -> dict:
    """The mapping entry for (platform, user_id) or {}.

    ``profile`` is the validated profile id ("" when absent or invalid);
    ``profile_requested`` is what the entry asked for, so callers can tell a
    governance-only entry (nothing requested) from an invalid one (fail closed).
    """
    name = _platform_name(platform)
    uid = _normalize(name, user_id)
    if not name or not uid:
        return {}
    data = _load()
    rows = data.get(name) or {}
    if name == "whatsapp" and not rows:
        rows = data.get("whatsapp_cloud") or {}
    entry = rows.get(uid) if isinstance(rows, dict) else None
    if not isinstance(entry, dict):
        return {}
    email = str(entry.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return {}
    requested = str(entry.get("profile") or "").strip().lower()
    return {
        "email": email,
        "name": str(entry.get("name") or ""),
        "profile": requested if _PROFILE_RE.match(requested) else "",
        "profile_requested": requested,
    }


def mapped_email(platform, user_id) -> str:
    return mapping_for(platform, user_id).get("email", "")


def _requested_profile(entry: dict) -> str:
    """The non-default profile an entry asks for, or "" (unmapped / governance-only)."""
    requested = (entry or {}).get("profile_requested", "")
    return "" if not requested or requested == "default" else requested


def _source_entry(source) -> dict:
    try:
        return mapping_for(getattr(source, "platform", None), getattr(source, "user_id", None))
    except Exception:
        logger.debug("synthpulse identity lookup failed", exc_info=True)
        return {}


def profile_route_for_source(runner: Any, source, adapter_profile: Optional[str] = None) -> Optional[str]:
    """Multiplex routing step: the person's own profile for an event on the shared bot.

    Called by ``GatewayRunner._profile_name_for_source`` after the configured
    ``profile_routes`` found no match. Returns ``None`` (keep upstream routing)
    for unmapped senders, governance-only entries, and events received by a
    dedicated secondary bot (that bot serves its own profile, as in the fork).
    Raises ``ProfileRouteRejected`` when the entry names an invalid profile or
    one this gateway does not serve: the event is then dropped by the same
    fail-closed path as an explicit route to an unserved profile, never run in
    the shared default profile.
    """
    entry = _source_entry(source)
    requested = _requested_profile(entry)
    if not requested:
        return None
    from gateway.profile_routing import ProfileRouteRejected, _bot_profile_key

    if adapter_profile is None:
        try:
            owner = (runner._transport_owner(source)
                     if callable(getattr(source, "_transport_adapter_ref", None)) else None)
        except Exception:
            owner = None
        if isinstance(owner, tuple):
            adapter_profile = owner[1]
    if _bot_profile_key(adapter_profile) is not None:
        return None
    platform_name = _platform_name(getattr(source, "platform", None))

    def _reject(why: str, exc: Optional[BaseException] = None):
        # Routing runs several times per event (build_source, identity, busy policy); warn once per
        # mapping. The adapter / ingress gate still logs one drop warning per dropped event.
        key = ("route", why, entry.get("email"), requested)
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning("synthpulse identity: %s is mapped to profile %r on %s, which %s; dropping its events",
                           entry.get("email"), requested, platform_name, why, exc_info=exc is not None)
        raise ProfileRouteRejected(_ROUTE_NAME) from exc

    if not entry.get("profile"):
        _reject("is not a valid profile name")
    try:
        from gateway.run import _multiplex_profile_homes
        served = {name for name, _home in _multiplex_profile_homes(getattr(runner, "config", None))}
    except Exception as exc:
        _reject("cannot be checked (served profiles unavailable)", exc)
    if requested not in served:
        _reject("this gateway does not serve")
    logger.debug("synthpulse identity: %s user %s routed to profile %r",
                 platform_name, getattr(source, "user_id", None), requested)
    return requested


def fail_closed_reason(source, *, multiplexed: bool) -> str:
    """Why a mapped sender's turn must be dropped, or "" to let it run.

    Only entries that name a non-default profile can fail; unmapped senders and
    governance-only entries always return "". An explicitly stamped
    ``source.profile`` (``profile_routes`` match, dedicated bot, ``/p/<name>/``)
    keeps the fork's precedence and runs where it was routed. Otherwise:

    * multiplexed: the routing step must already have put the turn in the
      person's profile; a mapped event still on the default fall-through (an
      identity pinned without that step, e.g. a restored source) is dropped. A
      rejected route returns "" because the ingress gate drops it already.
    * standalone: the profile must be a valid, existing profile.
    """
    try:
        entry = _source_entry(source)
        requested = _requested_profile(entry)
        if not requested:
            return ""
        if getattr(source, "profile_route_rejected", False) is True:
            return ""
        if (getattr(source, "profile", None) or "").strip():
            return ""
        if not entry.get("profile"):
            return f"mapped profile {requested!r} is not a valid profile name"
        if multiplexed:
            return f"mapped profile {requested!r} was not applied to this event"
        from hermes_cli.profiles import profile_exists
        if not profile_exists(requested):
            return f"mapped profile {requested!r} does not exist"
        return ""
    except Exception as exc:  # a mapped person with a profile: never fall through to the shared one
        logger.debug("synthpulse identity fail-closed check raised", exc_info=True)
        return f"identity check failed ({type(exc).__name__})"


class Handle:
    __slots__ = ("token", "scope", "email", "profile")

    def __init__(self, token, scope, email, profile):
        self.token, self.scope, self.email, self.profile = token, scope, email, profile


def _runtime_profile(source) -> str:
    """The profile the turn actually runs in under multiplexing (pinned identity first)."""
    try:
        from gateway.session_identity import identity_of
        identity = identity_of(source)
        if identity is not None and identity.multiplexed and identity.runtime_profile:
            return identity.runtime_profile
    except Exception:
        pass
    return (getattr(source, "profile", None) or "").strip() or "default"


def bind_for_source(source, session_key: str = "", *, multiplexed: bool = False):
    """Bind governance (and, standalone, the person's profile) for a mapped sender.

    Returns a Handle to pass to ``reset`` or None when the sender is unmapped.
    Callers run ``fail_closed_reason`` first. Under multiplexing no scope is
    entered here: the routed identity already scopes the turn (see module doc),
    and governance names that runtime profile as the active one.
    """
    try:
        platform = getattr(source, "platform", None)
        user_id = getattr(source, "user_id", None)
        entry = mapping_for(platform, user_id)
        if not entry:
            return None
        email, profile = entry["email"], entry.get("profile", "")
        platform_name = _platform_name(platform)

        scope = None
        if multiplexed:
            active_profile = _runtime_profile(source)
            bound_profile = profile if profile and profile == active_profile else ""
        else:
            if profile and profile != "default" and not (getattr(source, "profile", None) or "").strip():
                try:
                    from hermes_cli.profiles import get_profile_dir, profile_exists
                    if profile_exists(profile):
                        from gateway.run import _profile_runtime_scope
                        scope = _profile_runtime_scope(get_profile_dir(profile))
                        scope.__enter__()
                        try:
                            source.profile = profile
                        except Exception:
                            pass
                    else:
                        logger.warning("synthpulse identity: profile %r for %s does not exist; running on the gateway profile", profile, email)
                except Exception as exc:
                    scope = None
                    key = "profile:" + type(exc).__name__
                    if key not in _WARNED:
                        _WARNED.add(key)
                        logger.warning("synthpulse identity: profile scope for %s skipped: %s", email, exc, exc_info=True)
            active_profile = profile if scope is not None else str(os.getenv("HERMES_PROFILE") or "default")
            bound_profile = profile if scope is not None else ""

        token, mode = None, "?"
        try:
            from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, bind_governance_context
            from hermes_cli.dashboard_governance.loader import load_governance_policy
            from hermes_cli.dashboard_governance.models import GovernanceSubject
            from hermes_cli.dashboard_governance.resolver import resolve_effective_access

            policy = load_governance_policy()
            subject = GovernanceSubject(
                email=email,
                display_name=str(getattr(source, "user_name", "") or entry.get("name") or ""),
                provider="gateway:" + platform_name,
                user_id=str(user_id or ""),
            )
            access = resolve_effective_access(policy, subject)
            mode = getattr(access, "mode", "?")
            ctx = DashboardGovernanceContext(
                subject=subject,
                access=access,
                active_profile=active_profile,
                session_id=str(session_key or ""),
            )
            token = bind_governance_context(ctx)
        except Exception as exc:
            # Governance fails open (as before), but an entered profile scope stays: the
            # person's turn must not fall back to the shared profile because policy was unreadable.
            key = "governance:" + type(exc).__name__
            if key not in _WARNED:
                _WARNED.add(key)
                logger.warning("synthpulse identity: governance for %s not bound: %s", email, exc, exc_info=True)
            if scope is None:
                return None
        logger.info("governance bound: %s user %s -> %s (mode=%s, profile=%s)", platform_name, user_id, email,
                    mode, bound_profile or ("routed:" + active_profile if multiplexed else "gateway"))
        return Handle(token, scope, email, bound_profile)
    except Exception as exc:  # fail open, log once per error type
        key = type(exc).__name__
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning("synthpulse identity binding skipped: %s", exc, exc_info=True)
        return None


def reset(handle) -> None:
    if handle is None:
        return
    if getattr(handle, "token", None) is not None:
        try:
            from hermes_cli.dashboard_governance.context import reset_governance_context
            reset_governance_context(handle.token)
        except Exception:
            logger.debug("synthpulse identity reset failed", exc_info=True)
    scope = getattr(handle, "scope", None)
    if scope is not None:
        try:
            scope.__exit__(None, None, None)
        except Exception:
            logger.debug("synthpulse identity profile scope exit failed", exc_info=True)
