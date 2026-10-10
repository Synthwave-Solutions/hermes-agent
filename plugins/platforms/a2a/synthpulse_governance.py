"""SynthPulse governance for inbound A2A calls.

Two hooks, both called from ``A2ARequestHandler.do_POST``:

``admit_peer``
    Right after ``security.authenticate``. Tokens are always required: a
    caller must present a per-peer pair token (``A2A_PEER_TOKENS``) whose
    name is mapped in ``gateway-identities.json["a2a"]``. The token is parsed
    only after it authenticated, and its organisation and installation fields
    must agree with the mapping and with this installation (the policy's
    ``organization.id`` and ``SP_INSTALLATION_ID``). Unmapped peers are
    refused in every governance mode; A2A has no legacy behaviour to keep.
    The only way around a token is the local development flag
    ``A2A_ALLOW_UNAUTHENTICATED_LOCAL=1``, honoured only when no token is
    configured, the public URL, the bind host, the client and the Host header
    are all loopback, no proxy header is present and the policy is not
    enforced. Behind ``tailscale serve`` every caller appears as 127.0.0.1,
    so loopback alone is never evidence of trust.

``authorize_call``
    For every JSON-RPC method, not only sends. Verifies the signed actor
    assertion (``X-SP-A2A-Actor``) against the pair token, bound to the task
    the request names; checks that the peer may call the routed agent (the
    mapping's ``serve`` list plus an explicit ``a2a:serve:<agent>`` grant for
    the service principal); keeps contexts with the peer that opened them;
    and resolves the effective principal: the actor's access in this
    installation's policy intersected with the service principal's
    (:func:`intersect_access`, a pure function). An actor the policy does not
    know gets the ``a2a_guest`` role only.

Every decision is written with ``append_audit_event`` (actor and peer, agent,
task id, depth, decision). Refusals are also appended to
``$HERMES_HOME/logs/a2a-refused-events.jsonl`` as ``a2a_refused`` events in
the control plane's ``/v1/events`` shape, for the installation reporter.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Mapping, Optional

from gateway.synthpulse_identity import A2ABinding, a2a_mapping_for

from . import protocol, security
from .synthpulse_assertion import (
    ACTOR_HEADER,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_SKEW,
    ActorAssertionError,
    PairTokenError,
    parse_pair_token,
    verify_actor,
)

logger = logging.getLogger(__name__)

DEV_FLAG_ENV = "A2A_ALLOW_UNAUTHENTICATED_LOCAL"
GUEST_ROLE = "a2a_guest"
SERVE_PERMISSION_PREFIX = "a2a:serve:"
ROOT_AGENT_NAME = "default"

AUDIT_EVENT = "a2a_inbound"
CENTRAL_EVENT_KIND = "a2a_refused"
CENTRAL_EVENT_SERVICE = "engine"
CENTRAL_EVENTS_FILE = "a2a-refused-events.jsonl"
_CENTRAL_EVENTS_MAX_BYTES = 1_048_576

# Assertions older or newer than this are stale (Appendix F: 300 s).
MAX_ASSERTION_SKEW = DEFAULT_MAX_SKEW
# Upper bound for a configured depth cap; the default cap is 2.
_MAX_DEPTH_LIMIT = 5

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_PROXY_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "tailscale-user-login",
    "tailscale-user-name",
    "tailscale-headers-info",
)
_LEVEL_RANK = {"user": 0, "elevated": 1, "admin": 2}
_ADMIN_ROLES = frozenset({"owner", "admin", "admin_delivery"})


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """A refused call. ``reason`` is a stable audit code; ``message`` is safe
    to return to the caller (it never names a token, an email or a policy)."""

    http_status: int
    code: int
    reason: str
    message: str

    def jsonrpc(self, req_id: Any = None) -> dict:
        return protocol.jsonrpc_error(req_id, self.code, self.message)


@dataclass(frozen=True)
class PeerAdmission:
    """A peer that passed ``admit_peer``. Carries the authenticated token for
    the assertion check; never logged or serialised."""

    peer: str
    policy_mode: str
    org_scope: str
    mapping: Mapping[str, Any] = field(default_factory=dict)
    org_id: Optional[str] = None
    caller_installation: str = ""
    unauthenticated_local: bool = False
    token: str = field(default="", repr=False, compare=False)
    policy: Any = field(default=None, repr=False, compare=False)


def _refuse(status: int, code: int, reason: str, message: str) -> Refusal:
    return Refusal(http_status=status, code=code, reason=reason, message=message)


def _untrusted(reason: str) -> Refusal:
    return _refuse(403, protocol.ERR_UNTRUSTED_PEER, reason, "peer not trusted")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def dev_flag_enabled() -> bool:
    return os.getenv(DEV_FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _host_is_loopback(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def public_url_is_loopback(url: Optional[str] = None) -> bool:
    """True only for an explicit ``A2A_PUBLIC_URL`` whose host is loopback."""
    raw = os.getenv("A2A_PUBLIC_URL", "") if url is None else url
    raw = (raw or "").strip()
    if not raw:
        return False
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return _host_is_loopback(parsed.hostname or "")


def _host_header_is_loopback(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return True  # HTTP/1.0 without Host; the client address is still checked
    try:
        return _host_is_loopback(urllib.parse.urlsplit("//" + value).hostname or "")
    except ValueError:
        return False


def _header(headers: Any, name: str) -> str:
    """Case-insensitive header lookup (HTTP message headers or a plain dict)."""
    if headers is None:
        return ""
    try:
        value = headers.get(name)
        if value is None and isinstance(headers, Mapping):
            wanted = name.lower()
            value = next((v for k, v in headers.items() if str(k).lower() == wanted), None)
    except Exception:
        return ""
    return str(value) if value is not None else ""


def max_actor_depth(extra: Any = None) -> int:
    """The actor assertion depth cap: ``platforms.a2a.extra.max_actor_depth``,
    default 2, never above 5. Invalid values fall back to the default."""
    raw = (extra or {}).get("max_actor_depth") if isinstance(extra, Mapping) else None
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MAX_DEPTH
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DEPTH
    if value < 0 or value > _MAX_DEPTH_LIMIT:
        return DEFAULT_MAX_DEPTH
    return value


def agent_name(agent: Optional[Mapping[str, Any]]) -> str:
    """The name a served agent has in ``serve`` lists and ``a2a:serve:``."""
    slug = str((agent or {}).get("slug") or "").strip()
    return slug or ROOT_AGENT_NAME


def _load_policy():
    from hermes_cli.dashboard_governance.loader import load_governance_policy

    return load_governance_policy()


def _policy_org_id(policy) -> Optional[str]:
    raw = getattr(policy, "raw", None)
    organization = raw.get("organization") if isinstance(raw, Mapping) else None
    org_id = organization.get("id") if isinstance(organization, Mapping) else None
    if org_id is None:
        return None
    org_id = str(org_id).strip()
    return org_id or None


def _local_installation_id() -> Optional[str]:
    value = os.getenv("SP_INSTALLATION_ID", "").strip()
    return value or None


# ---------------------------------------------------------------------------
# Admission (right after authenticate)
# ---------------------------------------------------------------------------


def _admit_unauthenticated_local(identity: str, *, client_ip: str, headers: Any,
                                 bind_host: str) -> PeerAdmission | Refusal:
    if not dev_flag_enabled():
        return _refuse(401, protocol.ERR_UNAUTHORIZED, "tokens_required", "unauthorized")
    if not public_url_is_loopback():
        return _refuse(401, protocol.ERR_UNAUTHORIZED, "dev_flag_public_url", "unauthorized")
    if not _host_is_loopback(bind_host) or not _host_is_loopback(client_ip):
        return _refuse(401, protocol.ERR_UNAUTHORIZED, "dev_flag_not_loopback", "unauthorized")
    if any(_header(headers, name) for name in _PROXY_HEADERS):
        return _refuse(401, protocol.ERR_UNAUTHORIZED, "dev_flag_proxied", "unauthorized")
    if not _host_header_is_loopback(_header(headers, "Host")):
        return _refuse(401, protocol.ERR_UNAUTHORIZED, "dev_flag_not_loopback", "unauthorized")
    try:
        policy = _load_policy()
    except Exception:
        return _refuse(403, protocol.ERR_FORBIDDEN, "policy_unavailable", "call not allowed")
    if policy.enforce:
        return _refuse(401, protocol.ERR_UNAUTHORIZED, "dev_flag_under_enforce", "unauthorized")
    logger.warning(
        "A2A: accepting an unauthenticated local call from %s because %s is set; "
        "never set it outside local development",
        identity, DEV_FLAG_ENV,
    )
    return PeerAdmission(
        peer=identity,
        policy_mode=policy.mode,
        org_scope="unauthenticated_local",
        unauthenticated_local=True,
        policy=policy,
    )


def admit_peer(identity: Optional[str], *, auth_header: Optional[str], client_ip: str = "",
               headers: Any = None, bind_host: str = "127.0.0.1",
               security_context: Optional[security.A2ASecurityContext] = None) -> PeerAdmission | Refusal:
    """Admit an authenticated caller as a mapped A2A peer, or refuse it.

    ``identity`` is what ``security.authenticate`` returned for the request.
    """
    if not identity:
        return _refuse(401, protocol.ERR_UNAUTHORIZED, "unauthenticated", "unauthorized")

    security_context = security_context or security.A2ASecurityContext.capture()
    if security_context.localhost_only():
        return _admit_unauthenticated_local(identity, client_ip=client_ip, headers=headers,
                                            bind_host=bind_host)

    # Peer token names never contain ':'; ``ip:<addr>`` identities come from
    # the shared A2A_BEARER_TOKEN, which is not a pair token.
    if identity.startswith("ip:"):
        return _untrusted("shared_token")

    parts = (auth_header or "").split(None, 1)
    presented = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    tokens = dict(security_context.peer_tokens)
    if not presented or tokens.get(presented) != identity:
        return _untrusted("token_mismatch")

    try:
        claims = parse_pair_token(presented)
    except PairTokenError:
        return _untrusted("not_a_pair_token")

    mapping = a2a_mapping_for(identity)
    if not mapping:
        return _untrusted("unmapped_peer")

    if claims.caller_installation != mapping["caller_installation"]:
        return _untrusted("caller_mismatch")

    try:
        policy = _load_policy()
    except Exception:
        return _refuse(403, protocol.ERR_FORBIDDEN, "policy_unavailable", "call not allowed")

    local_org = _policy_org_id(policy)
    mapping_org = mapping.get("org_id")
    if local_org is None:
        # HQ: no organisation in the policy, so neither the mapping nor the
        # token may name one.
        if mapping_org is not None or claims.org_id is not None:
            return _untrusted("org_mismatch")
        org_scope = "org_unscoped"
    else:
        if claims.org_id != local_org or mapping_org != local_org:
            return _untrusted("org_mismatch")
        org_scope = "org"

    local_installation = _local_installation_id()
    if local_installation is not None:
        if claims.callee_installation != local_installation:
            return _untrusted("callee_mismatch")
    elif org_scope == "org":
        return _untrusted("installation_unknown")

    return PeerAdmission(
        peer=identity,
        policy_mode=policy.mode,
        org_scope=org_scope,
        mapping=dict(mapping),
        org_id=local_org,
        caller_installation=claims.caller_installation,
        token=presented,
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Effective principal (pure)
# ---------------------------------------------------------------------------


def _level(access) -> str:
    if "bootstrap_admin" in tuple(getattr(access, "grant_sources", ()) or ()):
        return "admin"
    level = getattr(access, "access_level", "") or ""
    return level if level in _LEVEL_RANK else "user"


def _combine_ceilings(left, right):
    from hermes_cli.dashboard_governance.models import intersect_grants

    if left is None:
        return right
    if right is None:
        return left
    return intersect_grants(left, right)


def _stricter_caps(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict:
    caps = dict(right or {})
    for key, value in (left or {}).items():
        other = caps.get(key)
        if key not in caps:
            caps[key] = value
        elif (isinstance(value, (int, float)) and not isinstance(value, bool)
              and isinstance(other, (int, float)) and not isinstance(other, bool)):
            caps[key] = min(value, other)
    return caps


def intersect_access(actor_access, service_access):
    """The effective principal of an A2A turn: never more than either side.

    Grants are intersected dimension by dimension (the resolver's own
    ``intersect_grants``), denies and approval requirements are united, role
    ceilings are combined, the access level is the lower of the two with an
    unset level read as ``user``, the mode is always ``whitelist``, and admin
    roles and the bootstrap marker never carry over. The subject stays the
    actor. Pure: no I/O and no policy lookups.
    """
    from hermes_cli.dashboard_governance.models import EffectiveAccess, intersect_grants

    grants = intersect_grants(actor_access.grants, service_access.grants)
    grants = replace(
        grants,
        cli_approval_commands=(actor_access.grants.cli_approval_commands
                               | service_access.grants.cli_approval_commands),
        usage_caps=_stricter_caps(actor_access.grants.usage_caps, service_access.grants.usage_caps),
    )
    level = min(_level(actor_access), _level(service_access), key=_LEVEL_RANK.__getitem__)
    sources = ("a2a:effective",) + tuple(
        f"actor:{s}" for s in actor_access.grant_sources if s != "bootstrap_admin"
    ) + tuple(f"service:{s}" for s in service_access.grant_sources if s != "bootstrap_admin")
    return EffectiveAccess(
        subject=actor_access.subject,
        mode=actor_access.mode,
        roles=frozenset(actor_access.roles & service_access.roles) - _ADMIN_ROLES,
        groups=frozenset(actor_access.groups & service_access.groups),
        permissions=grants.permissions,
        profiles=grants.profiles,
        routes=grants.routes,
        grants=grants,
        grant_sources=sources,
        permission_sources={},
        deny=actor_access.deny.merge(service_access.deny),
        role_ceiling=_combine_ceilings(actor_access.role_ceiling, service_access.role_ceiling),
        access_level=level,
        access_mode="whitelist",
        approval_mode="manual",
        approval_prompt="",
        approval_configured=bool(actor_access.approval_configured or service_access.approval_configured),
    )


def actor_is_known(policy, actor_email: str) -> bool:
    email = (actor_email or "").strip().lower()
    return bool(email) and (email in policy.users or email in policy.bootstrap_admins)


def resolve_principals(policy, *, service_email: str, actor_email: str, peer: str):
    """Resolve ``(service_access, actor_access, effective_access)``.

    The actor is resolved in this installation's policy; an actor the policy
    does not know is resolved with the ``a2a_guest`` role only.
    """
    from hermes_cli.dashboard_governance.models import GovernanceSubject
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access

    service_subject = GovernanceSubject(email=service_email, provider="a2a:service", user_id=peer)
    service_access = resolve_effective_access(policy, service_subject)
    known = actor_is_known(policy, actor_email)
    actor_subject = GovernanceSubject(
        email=actor_email,
        provider="a2a",
        user_id=peer,
        roles=() if known else (GUEST_ROLE,),
    )
    actor_access = resolve_effective_access(policy, actor_subject)
    return service_access, actor_access, intersect_access(actor_access, service_access)


def has_serve_permission(service_access, agent: str) -> bool:
    """``a2a:serve:<agent>`` must be granted explicitly.

    A bare ``*`` (a bootstrap admin, or a blacklist account's operational
    wildcard) never counts: the grant has to name the ``a2a:`` namespace.
    """
    permission = SERVE_PERMISSION_PREFIX + agent
    if not service_access.allows("permissions", permission):
        return False
    return any(
        isinstance(pattern, str) and pattern.startswith("a2a:") and fnmatchcase(permission, pattern)
        for pattern in service_access.grants.permissions
    )


# ---------------------------------------------------------------------------
# Per-method authorization (every JSON-RPC method)
# ---------------------------------------------------------------------------


def authorize_call(admission: PeerAdmission, *, rpc_method: str, operation: str, params: Any,
                   agent: Optional[Mapping[str, Any]], headers: Any, tasks: Any = None,
                   max_depth: int = DEFAULT_MAX_DEPTH, now: Optional[float] = None) -> A2ABinding | Refusal:
    """Authorize one JSON-RPC request of an admitted peer.

    Returns the A2ABinding the turn runs under, or a Refusal.
    """
    name = agent_name(agent)
    task_id = protocol.request_task_id(operation, params)
    local = bool((agent or {}).get("local", True))

    if admission.unauthenticated_local:
        return A2ABinding(peer=admission.peer, agent=name, org_scope=admission.org_scope,
                          task_id=task_id, unauthenticated_local=True)

    if now is None:
        now = time.time()
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        return _refuse(403, protocol.ERR_ACTOR_ASSERTION, "actor_clock_invalid", "actor assertion refused")
    if (isinstance(max_depth, bool) or not isinstance(max_depth, int)
            or max_depth < 0 or max_depth > _MAX_DEPTH_LIMIT):
        max_depth = DEFAULT_MAX_DEPTH
    try:
        claims = verify_actor(
            admission.token,
            _header(headers, ACTOR_HEADER) or None,
            now=float(now),
            max_skew=MAX_ASSERTION_SKEW,
            max_depth=max_depth,
            task_id=task_id or None,
        )
    except ActorAssertionError as exc:
        return _refuse(403, protocol.ERR_ACTOR_ASSERTION, "actor_" + exc.reason, "actor assertion refused")
    if claims["caller"] != admission.caller_installation:
        return _refuse(403, protocol.ERR_ACTOR_ASSERTION, "actor_caller_mismatch", "actor assertion refused")

    mapping = admission.mapping
    if name not in tuple(mapping.get("serve") or ()):
        return _refuse(403, protocol.ERR_FORBIDDEN, "agent_not_served", "call not allowed")

    policy = admission.policy
    try:
        service_access, _actor_access, effective = resolve_principals(
            policy, service_email=mapping["email"], actor_email=claims["actor"], peer=admission.peer)
    except Exception:
        logger.warning("A2A: could not resolve the principal for peer %s", admission.peer, exc_info=True)
        return _refuse(403, protocol.ERR_FORBIDDEN, "principal_unresolved", "call not allowed")

    if not has_serve_permission(service_access, name):
        return _refuse(403, protocol.ERR_FORBIDDEN, "serve_not_granted", "call not allowed")

    if operation in protocol.SEND_OPERATIONS:
        if not local and policy.enforce:
            # A forwarded profile runs in a separate CLI process that carries
            # no governance binding yet; in-process routing under the
            # effective principal replaces it (E4). Until then, refuse.
            return _refuse(403, protocol.ERR_FORBIDDEN, "forward_unbound", "call not allowed")
        context_id = protocol.extract_context_id(params) if isinstance(params, dict) else ""
        if context_id and tasks is not None and not tasks.claim_context(context_id, admission.peer):
            return _refuse(403, protocol.ERR_FORBIDDEN, "context_not_owned", "contextId is not available")

    return A2ABinding(
        peer=admission.peer,
        agent=name,
        service_email=mapping["email"],
        actor_email=claims["actor"],
        org_id=admission.org_id,
        org_scope=admission.org_scope,
        caller_installation=admission.caller_installation,
        depth=int(claims["depth"]),
        task_id=task_id,
        profile=str(mapping.get("profile") or ""),
        access=effective,
        service_access=service_access,
    )


# ---------------------------------------------------------------------------
# Audit and the central feed
# ---------------------------------------------------------------------------


def _central_events_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / CENTRAL_EVENTS_FILE


def _append_central_event(event: dict) -> None:
    path = _central_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > _CENTRAL_EVENTS_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass
    line = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)


def record_decision(decision: str, reason: str, *, peer: str = "", actor: str = "", agent: str = "",
                    task_id: str = "", depth: Optional[int] = None, rpc_method: str = "", path: str = "",
                    mode: str = "", org_scope: str = "", caller_installation: str = "",
                    http_status: Optional[int] = None) -> None:
    """Audit one decision; refusals also go to the central feed.

    Best effort: an unwritable sink never turns a refusal into an allow or
    breaks the request.
    """
    extra = {
        "decision": decision,
        "peer": peer,
        "agent": agent,
        "task_id": task_id,
        "depth": depth,
        "rpc_method": rpc_method,
        "org_scope": org_scope,
        "caller_installation": caller_installation,
    }
    if peer:
        try:
            from hermes_cli.dashboard_governance.audit import append_audit_event

            append_audit_event(
                AUDIT_EVENT,
                subject_email=actor,
                subject_user_id=peer,
                path=path,
                method="POST",
                reason=reason,
                mode=mode,
                extra=extra,
            )
        except Exception:
            logger.debug("A2A: governance audit write failed", exc_info=True)
    if decision == "allow":
        return
    logger.warning("A2A call refused: reason=%s peer=%s agent=%s method=%s",
                   reason, peer or "-", agent or "-", rpc_method or "-")
    try:
        _append_central_event({
            "ts": datetime.now(timezone.utc).isoformat(),
            "service": CENTRAL_EVENT_SERVICE,
            "kind": CENTRAL_EVENT_KIND,
            "level": "warning",
            "logger": __name__,
            "fingerprint": f"{CENTRAL_EVENT_KIND}:{reason}",
            "message": f"A2A call refused: {reason}",
            "context": {
                "reason": reason,
                "peer": peer,
                "agent": agent,
                "rpc_method": rpc_method,
                "org_scope": org_scope,
                "caller_installation": caller_installation,
                "http_status": http_status,
            },
        })
    except Exception:
        logger.debug("A2A: central refusal event write failed", exc_info=True)
