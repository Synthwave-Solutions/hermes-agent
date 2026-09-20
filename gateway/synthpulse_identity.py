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
2. When the mapping names a profile that exists on disk, the turn runs inside
   that profile (``_profile_runtime_scope``: config, skills, memory, sessions,
   and that profile's .env as credential overlay), and ``source.profile`` is
   stamped so the session key lives in that person's namespace. The bot's own
   transport credentials stay with the gateway profile; nothing else of the
   shared profile leaks into the person's turn.

Unmapped senders keep the gateway's existing behaviour (allowlists, pairing).
Everything here fails open and logs once per error type, so a broken map can
never take the gateway down.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("gateway.synthpulse_identity")

_CACHE = {"path": "", "mtime": None, "data": {}, "loaded_at": 0.0}
_WARNED = set()
_USER_ID_RE = re.compile(r"^[A-Za-z0-9@._:+\-]{1,120}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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
    """The mapping entry for (platform, user_id) or {}."""
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
    profile = str(entry.get("profile") or "").strip().lower()
    return {"email": email, "name": str(entry.get("name") or ""), "profile": profile if _PROFILE_RE.match(profile) else ""}


def mapped_email(platform, user_id) -> str:
    return mapping_for(platform, user_id).get("email", "")


class Handle:
    __slots__ = ("token", "scope", "email", "profile")

    def __init__(self, token, scope, email, profile):
        self.token, self.scope, self.email, self.profile = token, scope, email, profile


def bind_for_source(source, session_key: str = ""):
    """Bind governance (and the person's profile) for a mapped sender.

    Returns a Handle to pass to ``reset`` or None when the sender is unmapped.
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
        ctx = DashboardGovernanceContext(
            subject=subject,
            access=access,
            active_profile=(profile if scope is not None else str(os.getenv("HERMES_PROFILE") or "default")),
            session_id=str(session_key or ""),
        )
        token = bind_governance_context(ctx)
        logger.info("governance bound: %s user %s -> %s (mode=%s, profile=%s)", platform_name, user_id, email,
                    getattr(access, "mode", "?"), profile if scope is not None else "gateway")
        return Handle(token, scope, email, profile if scope is not None else "")
    except Exception as exc:  # fail open, log once per error type
        key = type(exc).__name__
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning("synthpulse identity binding skipped: %s", exc, exc_info=True)
        return None


def reset(handle) -> None:
    if handle is None:
        return
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
