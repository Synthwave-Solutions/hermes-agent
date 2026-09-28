"""Model gateway enforcement and identity (SynthPulse managed service, R2).

Every model call of an installation goes through its one model gateway (an
OpenAI-compatible router such as OmniRoute) with the caller's own key, and is
traced with organisation, installation, module and principal. This module is
the engine's single source of truth for that contract:

* it reads the ``model_gateway`` block of ``config.yaml`` (defaults in
  ``hermes_cli/config_defaults.py``) plus the installation env contract
  (``SP_MODEL_GATEWAY_URL``, ``SP_MODEL_GATEWAY_KEY_FILE``,
  ``SP_MODEL_GATEWAY_ENFORCE``, ``SP_AUDIT_ORG_SALT_FILE``);
* it answers the enforcement predicate (:func:`enforced`) that the model
  client entry points consult before they build a client;
* it owns the one tool-backend hook, :func:`tool_backend_allowed`, that the
  speech, transcription, image and video tools and the managed tool gateway
  call before they pick a backend;
* it computes the identity every gateway request carries: the pseudonymous
  OpenAI ``user`` field (``u_`` plus 12 hex characters of an HMAC over the
  person's e-mail with the organisation salt) and the session header.

With ``model_gateway.enforce: false`` (the default) and no base URL nothing
changes: requests to other endpoints are never decorated, and no route is
refused. With enforcement on, every model client resolves to the gateway, a
direct provider route raises :class:`ModelGatewayRefused`, and an unusable
gateway raises :class:`ModelGatewayUnavailable` instead of paying for a
direct lane.

This module never reads a provider key and never names a provider host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Chain label of the gateway lane in the auxiliary client.
GATEWAY_LABEL = "gateway"

#: Error code of every refusal (tools, clients, the WebUI's 409 contract).
REFUSAL_CODE = "gateway_required"

#: Tool capabilities that are model traffic. ``tool_gateway`` is the Nous
#: managed tool gateway, whose hosts are on the provider host list whatever
#: vendor it fronts. Web, browser and terminal backends are not model traffic.
MODEL_CAPABILITIES = frozenset({"tts", "stt", "image_gen", "video_gen", "tool_gateway"})

#: Backends that run inside the engine's own component and never leave it
#: (recorded as ``local`` in the traffic registry, not as an exception).
LOCAL_BACKENDS: Mapping[str, frozenset] = {
    "tts": frozenset({"neutts", "kittentts", "piper"}),
    "stt": frozenset({"local", "local_command", "none"}),
}

_TRUE = {"1", "true", "yes", "on"}
_SESSION_HEADER_DEFAULT = "X-SP-Session"
_SAFE_HEADER_VALUE = re.compile(r"[^A-Za-z0-9._:@/+=-]")
_SAFE_TAG_VALUE = re.compile(r"[^A-Za-z0-9._:@/+-]")
_WARNED: set = set()
_WARN_LOCK = threading.Lock()
_FILE_CACHE: Dict[str, Tuple[int, int, str]] = {}
_FILE_CACHE_LOCK = threading.Lock()


class ModelGatewayError(RuntimeError):
    """Base class for gateway enforcement errors."""

    code = REFUSAL_CODE


class ModelGatewayUnavailable(ModelGatewayError):
    """Enforcement is on but the gateway cannot serve the call.

    Raised when the gateway is not configured (no base URL or no key) or has
    been marked unhealthy. The caller never falls back to a direct lane.
    """

    status_code = 503


class ModelGatewayRefused(ValueError):
    """A direct provider route was refused under ``model_gateway.enforce``.

    A ``ValueError`` so the tools' existing selection-error paths (which
    catch ``ValueError`` and report the reason) surface it unchanged.
    """

    code = REFUSAL_CODE
    status_code = 403

    def __init__(self, capability: str, backend: str, detail: str = "") -> None:
        self.capability = str(capability or "model")
        self.backend = str(backend or "unknown")
        self.detail = detail
        message = (
            f"{REFUSAL_CODE}: {self.capability} backend '{self.backend}' is not "
            "allowed while model_gateway.enforce is on; only the installation's "
            "model gateway may be used"
        )
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": False,
            "error": str(self),
            "error_code": REFUSAL_CODE,
            "error_type": REFUSAL_CODE,
            "capability": self.capability,
            "backend": self.backend,
        }

    def tool_result(self) -> str:
        """The refusal as a tool result JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(frozen=True)
class GatewaySettings:
    """The effective ``model_gateway`` settings for the active profile."""

    base_url: str = ""
    enforce: bool = False
    org_id: str = ""
    installation_id: str = ""
    module: str = "engine"
    user_field: str = "pseudonym"
    session_header: str = _SESSION_HEADER_DEFAULT
    realtime: bool = False
    tool_exceptions: Tuple[str, ...] = ()
    key_file: str = ""
    key_env: str = ""
    principal: str = ""
    salt_file: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


def _text(value: Any) -> str:
    return str(value or "").strip() if not isinstance(value, (dict, list)) else ""


def _warn_once(key: str, message: str, *args: Any) -> None:
    with _WARN_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    logger.warning(message, *args)


def _load_config() -> Mapping[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception:
        logger.debug("model_gateway: config unreadable", exc_info=True)
        return {}
    return config if isinstance(config, Mapping) else {}


def _config_block(config: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    if config is None:
        config = _load_config()
    block = config.get("model_gateway") if isinstance(config, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _managed_installation() -> bool:
    return _truthy(os.environ.get("SP_MANAGED", ""))


def settings(config: Optional[Mapping[str, Any]] = None) -> GatewaySettings:
    """Return the effective settings; never raises.

    ``SP_MODEL_GATEWAY_ENFORCE`` can only switch enforcement on, never off,
    so a profile config edit cannot lift it on a managed installation.
    ``SP_MODEL_GATEWAY_URL`` fills an empty ``base_url``. On a managed
    installation (``SP_MANAGED=1``) ``tool_exceptions`` are ignored: the
    render refuses them there, and the engine does not trust a hand edit.
    """
    block = _config_block(config)
    base_url = _text(block.get("base_url")) or _text(os.environ.get("SP_MODEL_GATEWAY_URL"))
    enforce = _truthy(block.get("enforce")) or _truthy(os.environ.get("SP_MODEL_GATEWAY_ENFORCE", ""))
    raw_exceptions = block.get("tool_exceptions") or ()
    exceptions: list = []
    if isinstance(raw_exceptions, (list, tuple)):
        for item in raw_exceptions:
            if isinstance(item, Mapping):
                cap = _text(item.get("capability")).lower()
                backend = _text(item.get("backend")).lower()
                if backend:
                    exceptions.append(f"{cap}:{backend}" if cap else backend)
            elif _text(item):
                exceptions.append(_text(item).lower())
    if exceptions and _managed_installation():
        _warn_once(
            "managed-exceptions",
            "model_gateway.tool_exceptions ignored on a managed installation (%d entries)",
            len(exceptions),
        )
        exceptions = []
    return GatewaySettings(
        base_url=base_url.rstrip("/"),
        enforce=enforce,
        org_id=_text(block.get("org_id")),
        installation_id=_text(block.get("installation_id")),
        module=_text(block.get("module")) or "engine",
        user_field=(_text(block.get("user_field")) or "pseudonym").lower(),
        session_header=_text(block.get("session_header")) or _SESSION_HEADER_DEFAULT,
        realtime=_truthy(block.get("realtime")),
        tool_exceptions=tuple(exceptions),
        key_file=_text(block.get("key_file")),
        key_env=_text(block.get("key_env")),
        principal=_text(block.get("principal")),
        salt_file=_text(block.get("salt_file")),
    )


def enforced(config: Optional[Mapping[str, Any]] = None) -> bool:
    """The enforcement predicate: True when direct provider routes are refused."""
    return settings(config).enforce


def gateway_base_url(config: Optional[Mapping[str, Any]] = None) -> str:
    """The gateway's OpenAI-compatible base URL (normally ending in ``/v1``)."""
    return settings(config).base_url


def anthropic_base_url(config: Optional[Mapping[str, Any]] = None) -> str:
    """The gateway root for Anthropic-wire clients, which append ``/v1/messages``."""
    base = gateway_base_url(config)
    if base.endswith("/v1"):
        return base[: -len("/v1")]
    return base


def _origin(url: str) -> Optional[Tuple[str, str, int]]:
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw if "://" in raw else f"http://{raw}")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return None
        scheme = (parts.scheme or "http").lower()
        port = parts.port or (443 if scheme in {"https", "wss"} else 80)
    except ValueError:
        return None
    if scheme == "wss":
        scheme = "https"
    elif scheme == "ws":
        scheme = "http"
    return scheme, host, port


def is_gateway_url(url: Any, config: Optional[Mapping[str, Any]] = None) -> bool:
    """True when ``url`` is on the configured gateway (same scheme, host and port).

    Every path on the gateway's origin belongs to the gateway: on
    installations its route filter passes only the audited ``/v1`` routes,
    so a path check here would add nothing but false negatives.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    base = gateway_base_url(config)
    if not base:
        return False
    target = _origin(url)
    return target is not None and target == _origin(base)


# ---------------------------------------------------------------------------
# Key and salt (never logged)
# ---------------------------------------------------------------------------


def _read_secret_file(path: str) -> str:
    """Read a 0600 secret file; cached on (mtime, size); never logs the value."""
    if not path:
        return ""
    expanded = os.path.expanduser(path)
    try:
        st = os.stat(expanded)
    except OSError:
        _warn_once(f"missing:{expanded}", "model_gateway: secret file %s is not readable", expanded)
        return ""
    key = (st.st_mtime_ns, st.st_size)
    with _FILE_CACHE_LOCK:
        cached = _FILE_CACHE.get(expanded)
        if cached and (cached[0], cached[1]) == key:
            return cached[2]
    try:
        value = Path(expanded).read_text(encoding="utf-8").strip()
    except OSError:
        _warn_once(f"unreadable:{expanded}", "model_gateway: secret file %s is not readable", expanded)
        return ""
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[expanded] = (key[0], key[1], value)
    return value


def _scoped_env(name: str) -> str:
    if not name:
        return ""
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret

        try:
            return (get_secret(name, "") or "").strip()
        except UnscopedSecretError:
            return ""
    except Exception:
        return (os.environ.get(name) or "").strip()


def _custom_provider_entries(config: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    custom = config.get("custom_providers") if isinstance(config, Mapping) else None
    if isinstance(custom, list):
        for entry in custom:
            if isinstance(entry, Mapping):
                yield entry
    providers = config.get("providers") if isinstance(config, Mapping) else None
    if isinstance(providers, Mapping):
        for entry in providers.values():
            if isinstance(entry, Mapping):
                yield entry


def _key_from_gateway_provider_entry(config: Optional[Mapping[str, Any]]) -> str:
    """The key of a configured custom provider entry whose base URL is the gateway.

    At HQ each profile reaches OmniRoute through a ``custom_providers`` entry
    whose ``key_env`` names the profile's own gateway key; that key is the
    profile's gateway key, so it is used when no key file is configured.
    """
    if config is None:
        config = _load_config()
    for entry in _custom_provider_entries(config):
        base = _text(entry.get("base_url") or entry.get("api") or entry.get("url"))
        if not base or not is_gateway_url(base, config):
            continue
        key_env = _text(entry.get("key_env") or entry.get("api_key_env"))
        value = _scoped_env(key_env) if key_env else ""
        if not value:
            value = _text(entry.get("api_key"))
        if value:
            return value
    return ""


def gateway_api_key(*, fallback: str = "", config: Optional[Mapping[str, Any]] = None) -> str:
    """The caller's own gateway key, or ``""`` when none is configured.

    Order: ``SP_MODEL_GATEWAY_KEY_FILE``, ``model_gateway.key_file``,
    ``model_gateway.key_env``, a custom provider entry on the gateway, then
    ``fallback`` (a key the caller already holds for the gateway URL).
    """
    if config is None:
        config = _load_config()
    s = settings(config)
    for candidate in (
        _read_secret_file(_text(os.environ.get("SP_MODEL_GATEWAY_KEY_FILE"))),
        _read_secret_file(s.key_file),
        _scoped_env(s.key_env),
        _key_from_gateway_provider_entry(config),
        _text(fallback) if not callable(fallback) else "",
    ):
        if candidate and candidate not in {"no-key-required", "no-key"}:
            return candidate
    return ""


def require_gateway(*, fallback_key: str = "", config: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    """Return ``(base_url, key)`` or raise :class:`ModelGatewayUnavailable`."""
    if config is None:
        config = _load_config()
    base = gateway_base_url(config)
    if not base:
        raise ModelGatewayUnavailable(
            "model_gateway.enforce is on but model_gateway.base_url is empty; "
            "no model call can be made"
        )
    key = gateway_api_key(fallback=fallback_key, config=config)
    if not key:
        raise ModelGatewayUnavailable(
            "model_gateway.enforce is on but no gateway key is configured "
            "(SP_MODEL_GATEWAY_KEY_FILE or model_gateway.key_file)"
        )
    return base, key


def _org_salt(config: Optional[Mapping[str, Any]] = None) -> str:
    s = settings(config)
    return _read_secret_file(_text(os.environ.get("SP_AUDIT_ORG_SALT_FILE"))) or _read_secret_file(s.salt_file)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def pseudonym(email: str, salt: Any) -> str:
    """The OpenAI ``user`` field for a person: ``u_`` plus 12 hex characters.

    Byte for byte the function of STACK ``modules/omniroute/key_registry.py``:
    HMAC-SHA256 keyed with the organisation salt over the trimmed, lowercased
    e-mail address, so the exporter and the engine agree on every pseudonym.
    """
    if not isinstance(email, str) or "@" not in email:
        raise ValueError("pseudonym needs an e-mail address")
    key = salt.encode("utf-8") if isinstance(salt, str) else bytes(salt)
    if not key:
        raise ValueError("pseudonym needs a non-empty salt")
    digest = hmac.new(key, email.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
    return "u_" + digest[:12]


def _current_person_email() -> str:
    try:
        from hermes_cli.dashboard_governance.context import current_governance_context

        ctx = current_governance_context()
    except Exception:
        return ""
    subject = getattr(ctx, "subject", None) if ctx is not None else None
    email = getattr(subject, "email", "") if subject is not None else ""
    return email.strip() if isinstance(email, str) else ""


def request_user(config: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """The pseudonymous ``user`` value for the current call, or None.

    The person is the bound governance subject (a WebUI or dashboard turn),
    else the profile's configured ``model_gateway.principal``. Without a
    person there is no ``user`` field: the key identifies module traffic.
    The e-mail never leaves the engine; without a salt nothing is sent.
    """
    if config is None:
        config = _load_config()
    s = settings(config)
    if s.user_field in {"none", "off", "false", "0"}:
        return None
    email = _current_person_email() or s.principal
    if not email or "@" not in email:
        return None
    salt = _org_salt(config)
    if not salt:
        _warn_once(
            "no-salt",
            "model_gateway: no organisation salt (SP_AUDIT_ORG_SALT_FILE); "
            "the pseudonymous user field is not sent",
        )
        return None
    try:
        return pseudonym(email, salt)
    except ValueError:
        return None


def current_session_id() -> str:
    try:
        from gateway.session_context import get_session_env

        return (get_session_env("HERMES_SESSION_ID", "") or "").strip()
    except Exception:
        return ""


def _safe_header_value(value: Any) -> str:
    text = str(value or "").strip()
    return _SAFE_HEADER_VALUE.sub("", text)[:128]


def identity_headers(session_id: Any = None, config: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """The per-request identity headers (today the session header)."""
    s = settings(config)
    value = _safe_header_value(session_id if session_id else current_session_id())
    if not value or not s.session_header:
        return {}
    return {s.session_header: value}


def apply_request_identity(
    kwargs: Dict[str, Any],
    *,
    base_url: Any,
    api_mode: str = "chat_completions",
    session_id: Any = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Add the ``user`` field and the session header to a gateway request.

    A request to any other endpoint is returned unchanged, so nothing changes
    while ``model_gateway.base_url`` is empty.
    """
    if not isinstance(kwargs, dict):
        return kwargs
    if config is None:
        config = _load_config()
    if not is_gateway_url(str(base_url or ""), config):
        return kwargs
    mode = (api_mode or "chat_completions").strip().lower()
    if mode == "bedrock_converse":
        return kwargs
    user = request_user(config)
    if user:
        if mode == "anthropic_messages":
            metadata = dict(kwargs.get("metadata") or {})
            metadata["user_id"] = user
            kwargs["metadata"] = metadata
        elif mode == "codex_responses":
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["user"] = user
            kwargs["extra_body"] = extra_body
        else:
            kwargs["user"] = user
    headers = identity_headers(session_id, config)
    if headers:
        merged = dict(kwargs.get("extra_headers") or {})
        merged.update(headers)
        kwargs["extra_headers"] = merged
    return kwargs


def active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _tag(value: Any) -> str:
    return _SAFE_TAG_VALUE.sub("", str(value or "").strip())[:120]


def trace_identity(config: Optional[Mapping[str, Any]] = None, *, environment: str = "") -> Dict[str, Any]:
    """User, profile, organisation, module and installation for a trace.

    ``user_id`` is the pseudonym when a person is behind the call, otherwise
    ``module:<module>`` (the exporter's convention for service traffic).
    """
    if config is None:
        config = _load_config()
    s = settings(config)
    profile = active_profile_name()
    user = request_user(config)
    user_id = user or f"module:{s.module}"
    tags = []
    for prefix, value in (
        ("org", s.org_id),
        ("inst", s.installation_id),
        ("mod", s.module),
        ("profile", profile),
        ("env", environment),
    ):
        cleaned = _tag(value)
        if cleaned:
            tags.append(f"{prefix}:{cleaned}")
    metadata = {
        "user": user_id,
        "profile": profile,
        "org_id": s.org_id,
        "installation_id": s.installation_id,
        "module": s.module,
        "gateway_enforced": "true" if s.enforce else "false",
    }
    return {"user_id": user_id, "tags": tags, "metadata": metadata}


# ---------------------------------------------------------------------------
# Tool backend hook
# ---------------------------------------------------------------------------

#: Nous tool gateway vendors and the model capability each one serves.
TOOL_GATEWAY_VENDOR_CAPABILITY: Mapping[str, str] = {
    "openai-audio": "tts",
    "fal-queue": "image_gen",
    "fal-video": "video_gen",
    "krea": "image_gen",
}


def _excepted(s: GatewaySettings, capability: str, backend: str) -> bool:
    if not s.tool_exceptions:
        return False
    wanted = {backend, f"{capability}:{backend}"}
    if capability == "tool_gateway":
        vendor = backend.split(":", 1)[1] if backend.startswith("nous:") else backend
        wanted |= {"nous", f"nous:{vendor}", f"tool_gateway:{vendor}"}
    return bool(wanted & set(s.tool_exceptions))


def tool_backend_allowed(
    capability: str,
    backend: Any,
    base_url: Any = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> bool:
    """The one tool-backend hook: may this backend carry model traffic now?

    True when enforcement is off, for capabilities that are not model
    traffic, for backends that run locally, for a backend whose base URL is
    on the gateway, and for a backend that ``model_gateway.tool_exceptions``
    names (HQ only, D58). Everything else is refused. Never raises.
    """
    try:
        if config is None:
            config = _load_config()
        s = settings(config)
        if not s.enforce:
            return True
        cap = str(capability or "").strip().lower()
        if cap not in MODEL_CAPABILITIES:
            return True
        name = str(backend or "").strip().lower()
        if name in LOCAL_BACKENDS.get(cap, frozenset()):
            return True
        if _excepted(s, cap, name):
            return True
        if isinstance(base_url, str) and base_url and is_gateway_url(base_url, config):
            return True
        return False
    except Exception:
        logger.debug("model_gateway: tool backend check failed; refusing", exc_info=True)
        return False


def check_tool_backend(
    capability: str,
    backend: Any,
    base_url: Any = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> None:
    """Raise :class:`ModelGatewayRefused` when :func:`tool_backend_allowed` says no."""
    if tool_backend_allowed(capability, backend, base_url, config=config):
        return
    _warn_once(
        f"refused:{capability}:{backend}",
        "model_gateway: refused %s backend %r under enforcement",
        capability,
        str(backend or ""),
    )
    raise ModelGatewayRefused(str(capability or ""), str(backend or ""))


def tool_gateway_allowed(vendor: str, *, config: Optional[Mapping[str, Any]] = None) -> bool:
    """May the Nous managed tool gateway serve ``vendor`` now?"""
    return tool_backend_allowed("tool_gateway", f"nous:{vendor}", None, config=config)


#: Registry providers built on the OpenAI SDK default client, whose base URL
#: is the SDK's ``OPENAI_BASE_URL`` variable when nothing else sets it.
_SDK_DEFAULT_BASE_URL_PROVIDERS = frozenset({"openai"})


def registry_provider_base_url(
    section: str, provider: Any, *, config: Optional[Mapping[str, Any]] = None
) -> str:
    """The base URL an image or video provider plugin will call, or ``""``.

    From the provider's own ``gateway_base_url``/``base_url`` attribute, else
    ``<section>.<name>.base_url`` or ``<section>.base_url`` in config, else,
    for providers on the OpenAI SDK default client, ``OPENAI_BASE_URL``. An
    unknown base URL is ``""``, which the hook refuses under enforcement.
    """
    for attr in ("gateway_base_url", "base_url"):
        try:
            value = getattr(provider, attr, None)
            if callable(value):
                value = value()
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    if config is None:
        config = _load_config()
    name = str(getattr(provider, "name", "") or "").strip()
    block = config.get(section) if isinstance(config, Mapping) else None
    if isinstance(block, Mapping):
        per_provider = block.get(name)
        if isinstance(per_provider, Mapping) and _text(per_provider.get("base_url")):
            return _text(per_provider.get("base_url"))
        if _text(block.get("base_url")):
            return _text(block.get("base_url"))
    if name in _SDK_DEFAULT_BASE_URL_PROVIDERS:
        return _scoped_env("OPENAI_BASE_URL")
    return ""


def registry_provider_refusal(
    section: str, provider: Any, *, config: Optional[Mapping[str, Any]] = None
) -> Optional[ModelGatewayRefused]:
    """The refusal for an active image or video provider, or None when allowed."""
    if provider is None:
        return None
    if config is None:
        config = _load_config()
    if not settings(config).enforce:
        return None
    name = str(getattr(provider, "name", "") or "unknown")
    base_url = registry_provider_base_url(section, provider, config=config)
    if tool_backend_allowed(section, name, base_url, config=config):
        return None
    _warn_once(
        f"refused:{section}:{name}",
        "model_gateway: refused %s provider %r under enforcement",
        section,
        name,
    )
    return ModelGatewayRefused(section, name, "its base URL is not the model gateway")


# ---------------------------------------------------------------------------
# Terminal children
# ---------------------------------------------------------------------------

#: Provider credential variables a child process must not see under
#: enforcement, beyond the ones the provider registry declares. Names only.
_EXTRA_PROVIDER_CREDENTIAL_ENV = frozenset({
    "OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_API_BASE",
    "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "VOICE_TOOLS_OPENAI_KEY", "FAL_KEY", "ELEVENLABS_API_KEY", "KREA_API_KEY",
    "TOOL_GATEWAY_USER_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "HF_TOKEN",
})


def provider_credential_env_names() -> frozenset:
    """Names of provider credential variables (from the provider registry)."""
    names = set(_EXTRA_PROVIDER_CREDENTIAL_ENV)
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        for pconfig in PROVIDER_REGISTRY.values():
            names.update(getattr(pconfig, "api_key_env_vars", ()) or ())
    except Exception:
        logger.debug("model_gateway: provider registry unavailable", exc_info=True)
    return frozenset(n for n in names if isinstance(n, str) and n)


def terminal_child_env(
    *, force_prefix: str = "_HERMES_FORCE_", config: Optional[Mapping[str, Any]] = None
) -> Dict[str, str]:
    """Environment overrides for a terminal child under enforcement.

    ``OPENAI_BASE_URL`` and ``ANTHROPIC_BASE_URL`` point at the gateway
    (``/v1`` and its root, so Anthropic clients reach ``/v1/messages``) with
    the profile's own gateway key, and every provider credential variable
    present in the process is blanked. Entries carry the force prefix of
    ``tools.environments.local`` so they pass its provider-variable filter.
    Returns ``{}`` when enforcement is off. Never contains identity or
    ceiling variables, so the child keeps whatever the environment layer
    injects for them.
    """
    if config is None:
        config = _load_config()
    s = settings(config)
    if not s.enforce:
        return {}
    base = s.base_url
    key = gateway_api_key(config=config) if base else ""
    env: Dict[str, str] = {}
    for name in provider_credential_env_names():
        if name in os.environ:
            env[f"{force_prefix}{name}"] = ""
    # Present even when unset in os.environ: provider CLIs read these.
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        env[f"{force_prefix}{name}"] = ""
    env[f"{force_prefix}OPENAI_BASE_URL"] = base
    env[f"{force_prefix}OPENAI_API_KEY"] = key
    env[f"{force_prefix}ANTHROPIC_BASE_URL"] = anthropic_base_url(config) if base else ""
    env[f"{force_prefix}ANTHROPIC_AUTH_TOKEN"] = key
    env[f"{force_prefix}ANTHROPIC_API_KEY"] = ""
    return env


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def cache_token(config: Optional[Mapping[str, Any]] = None) -> Tuple[str, ...]:
    """A cache-key suffix that changes when enforcement or the gateway changes.

    Empty while enforcement is off, so cache keys stay exactly as before.
    """
    s = settings(config)
    if not s.enforce:
        return ()
    return (GATEWAY_LABEL, s.base_url)


def reset_for_tests() -> None:
    """Clear the warn-once and secret file caches. Test-only."""
    with _WARN_LOCK:
        _WARNED.clear()
    with _FILE_CACHE_LOCK:
        _FILE_CACHE.clear()
