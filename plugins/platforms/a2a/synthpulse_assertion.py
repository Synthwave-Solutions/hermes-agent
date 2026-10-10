"""SynthPulse A2A pair tokens and signed actor assertions.

Pair token (minted by the cockpit, delivered through enrollment)::

    a2a1.<org_id>.<caller_installation>.<callee_installation>.<secret>

HQ tokens use ``-`` as ``org_id``. The callee first authenticates the whole
token as the plugin already does (constant-time compare against
``A2A_PEER_TOKENS``); only after that does it call :func:`parse_pair_token`
and compare the organisation and installation fields with the peer mapping
and the local policy. Parsing never authenticates anything.

Actor assertion, sent as the ``X-SP-A2A-Actor`` header on every call::

    v1.<base64url(canonical_json)>.<hex>

``canonical_json`` is ``{"actor", "caller", "depth", "task", "ts"}`` encoded
with sorted keys and no spaces (``json.dumps(..., sort_keys=True,
separators=(",", ":"))``, ASCII only). The base64url part carries no ``=``
padding. The MAC is lowercase hex of::

    HMAC-SHA256(key=sha256("sp-a2a-actor-v1|" + pair_token).digest(),
                msg="v1." + base64url_part)

The callee checks the MAC before it parses the payload, then ``ts`` within
``max_skew`` seconds of ``now``, ``caller`` equal to the token's caller
installation, ``depth`` at most the cap (default 2) and, when the JSON-RPC
request names a task, ``task`` equal to it.

Standard library only. Errors never include the token, its secret or a MAC.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

__all__ = [
    "ACTOR_HEADER",
    "ASSERTION_VERSION",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_SKEW",
    "HQ_ORG_ID",
    "PAIR_TOKEN_PREFIX",
    "ActorAssertionError",
    "PairTokenClaims",
    "PairTokenError",
    "canonical_json",
    "parse_pair_token",
    "sign_actor",
    "verify_actor",
]

ACTOR_HEADER = "X-SP-A2A-Actor"
ASSERTION_VERSION = "v1"
PAIR_TOKEN_PREFIX = "a2a1"
HQ_ORG_ID = "-"
DEFAULT_MAX_SKEW = 300
DEFAULT_MAX_DEPTH = 2

_KEY_CONTEXT = "sp-a2a-actor-v1|"
_CLAIM_KEYS = frozenset({"actor", "caller", "depth", "task", "ts"})

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{16,512}")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")
_MAC_RE = re.compile(r"[0-9a-f]{64}")
_CONTROL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]")

_MAX_TOKEN_LEN = 1024
_MAX_HEADER_LEN = 4096
_MAX_ACTOR_LEN = 254
_MAX_TASK_LEN = 256
_MAX_TS = 2**53  # exact as a float, so time arithmetic cannot overflow


class PairTokenError(ValueError):
    """The pair token does not have the a2a1 shape. Never carries the token."""


class ActorAssertionError(ValueError):
    """An actor assertion was refused.

    ``reason`` is a stable code for audit rows: ``missing``, ``malformed``,
    ``bad_token``, ``bad_signature``, ``stale``, ``caller_mismatch``,
    ``depth_exceeded`` or ``task_mismatch``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PairTokenClaims:
    """The identity fields of a pair token. The secret is never kept here.

    ``org_id`` is None for HQ tokens (``-`` on the wire).
    """

    org_id: str | None
    caller_installation: str
    callee_installation: str


# ---------------------------------------------------------------------------
# Pair tokens
# ---------------------------------------------------------------------------


def parse_pair_token(token) -> PairTokenClaims:
    """Split an a2a1 pair token into its identity fields.

    Raises PairTokenError (a ValueError) for anything that is not exactly
    ``a2a1.<org_id or ->.<caller>.<callee>.<secret>``. Structural only: call
    it after the whole token has been authenticated.
    """
    if not isinstance(token, str) or not token:
        raise PairTokenError("pair token is missing")
    if len(token) > _MAX_TOKEN_LEN:
        raise PairTokenError("pair token is too long")
    parts = token.split(".")
    if len(parts) != 5:
        raise PairTokenError("pair token must have five dot separated fields")
    prefix, org_id, caller, callee, secret = parts
    if prefix != PAIR_TOKEN_PREFIX:
        raise PairTokenError("pair token has an unknown version")
    if org_id != HQ_ORG_ID and not _ID_RE.fullmatch(org_id):
        raise PairTokenError("pair token has an invalid organisation field")
    if not _ID_RE.fullmatch(caller):
        raise PairTokenError("pair token has an invalid caller installation field")
    if not _ID_RE.fullmatch(callee):
        raise PairTokenError("pair token has an invalid callee installation field")
    if not _SECRET_RE.fullmatch(secret):
        raise PairTokenError(
            "pair token secret must be 16 to 512 URL safe characters"
        )
    return PairTokenClaims(
        org_id=None if org_id == HQ_ORG_ID else org_id,
        caller_installation=caller,
        callee_installation=callee,
    )


# ---------------------------------------------------------------------------
# Actor assertions
# ---------------------------------------------------------------------------


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _claims_problem(claims) -> str | None:
    """Return a description of what is wrong with complete claims, or None."""
    if not isinstance(claims, dict):
        return "claims must be an object"
    keys = set(claims)
    if keys != _CLAIM_KEYS:
        return "claims must have exactly actor, caller, depth, task and ts"
    actor = claims["actor"]
    if (
        not isinstance(actor, str)
        or not actor
        or len(actor) > _MAX_ACTOR_LEN
        or "@" not in actor
        or actor != actor.lower()
        or _CONTROL_OR_SPACE_RE.search(actor)
    ):
        return "actor must be a lowercase email address"
    caller = claims["caller"]
    if not isinstance(caller, str) or not _ID_RE.fullmatch(caller):
        return "caller must be an installation id"
    depth = claims["depth"]
    if not _is_int(depth) or depth < 0:
        return "depth must be a non negative integer"
    task = claims["task"]
    if (
        not isinstance(task, str)
        or len(task) > _MAX_TASK_LEN
        or _CONTROL_OR_SPACE_RE.search(task)
    ):
        return "task must be a task id or empty"
    ts = claims["ts"]
    if not _is_int(ts) or ts <= 0 or ts > _MAX_TS:
        return "ts must be a positive integer of unix seconds"
    return None


def canonical_json(claims: dict) -> str:
    """The canonical claims encoding: sorted keys, no spaces, ASCII only.

    Raises ValueError when the claims are incomplete or invalid.
    """
    problem = _claims_problem(claims)
    if problem:
        raise ValueError(problem)
    return json.dumps(
        {key: claims[key] for key in sorted(_CLAIM_KEYS)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Strict unpadded base64url decode; the encoding must be canonical."""
    if not _B64URL_RE.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64url_encode(raw) != text:
        raise ValueError("non canonical base64url")
    return raw


def _actor_key(pair_token: str) -> bytes:
    return hashlib.sha256((_KEY_CONTEXT + pair_token).encode("utf-8")).digest()


def _mac(pair_token: str, signed_part: str) -> str:
    return hmac.new(
        _actor_key(pair_token), signed_part.encode("ascii"), hashlib.sha256
    ).hexdigest()


def sign_actor(pair_token: str, claims: dict) -> str:
    """Build the X-SP-A2A-Actor header value for one outbound call.

    ``claims`` holds ``actor`` (email; stripped and lowercased here),
    ``caller`` (must equal the token's caller installation), ``depth``
    (int, 0 for a direct call), ``ts`` (int unix seconds) and optionally
    ``task`` (JSON-RPC task id; missing or None means empty). Raises ValueError (or
    PairTokenError) for an invalid token or claims.
    """
    token_claims = parse_pair_token(pair_token)
    if not isinstance(claims, dict):
        raise ValueError("claims must be an object")
    unknown = set(claims) - _CLAIM_KEYS
    if unknown:
        raise ValueError("claims accept only actor, caller, depth, task and ts")
    prepared = dict(claims)
    if prepared.get("task") is None:
        prepared["task"] = ""
    if isinstance(prepared.get("actor"), str):
        prepared["actor"] = prepared["actor"].strip().lower()
    body = canonical_json(prepared)
    if prepared["caller"] != token_claims.caller_installation:
        raise ValueError("caller must equal the pair token's caller installation")
    payload = _b64url_encode(body.encode("ascii"))
    signed_part = ASSERTION_VERSION + "." + payload
    return signed_part + "." + _mac(pair_token, signed_part)


def _reject_constant(name: str):
    raise ValueError("non finite number")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def verify_actor(
    pair_token: str,
    header,
    *,
    now: float,
    max_skew: int = DEFAULT_MAX_SKEW,
    max_depth: int = DEFAULT_MAX_DEPTH,
    task_id: str | None = None,
) -> dict:
    """Check an X-SP-A2A-Actor header against the authenticated pair token.

    Returns the verified claims ``{"actor", "caller", "depth", "task", "ts"}``.
    Raises ActorAssertionError (a ValueError) with a ``reason`` code when the
    header is missing, malformed, not signed with this token, older or newer
    than ``max_skew`` seconds, from another caller installation, deeper than
    ``max_depth``, or bound to another task than ``task_id`` (checked only
    when ``task_id`` is given and not empty).
    """
    try:
        token_claims = parse_pair_token(pair_token)
    except PairTokenError as exc:
        raise ActorAssertionError("bad_token", str(exc)) from None

    if header is None or (isinstance(header, str) and not header.strip()):
        raise ActorAssertionError("missing", "actor assertion is missing")
    if not isinstance(header, str) or len(header) > _MAX_HEADER_LEN:
        raise ActorAssertionError("malformed", "actor assertion is malformed")
    parts = header.strip().split(".")
    if len(parts) != 3 or parts[0] != ASSERTION_VERSION:
        raise ActorAssertionError("malformed", "actor assertion is malformed")
    _, payload, presented_mac = parts
    if not _B64URL_RE.fullmatch(payload) or not _MAC_RE.fullmatch(presented_mac):
        raise ActorAssertionError("malformed", "actor assertion is malformed")

    # Authenticate before looking at the payload.
    expected_mac = _mac(pair_token, ASSERTION_VERSION + "." + payload)
    if not hmac.compare_digest(presented_mac, expected_mac):
        raise ActorAssertionError("bad_signature", "actor assertion signature is invalid")

    try:
        body = _b64url_decode(payload).decode("ascii")
        claims = json.loads(
            body,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
        canonical = canonical_json(claims)
    except (ValueError, UnicodeDecodeError, binascii.Error, TypeError, RecursionError):
        raise ActorAssertionError("malformed", "actor assertion claims are malformed") from None
    if canonical != body:
        raise ActorAssertionError("malformed", "actor assertion claims are not canonical")

    if abs(float(now) - claims["ts"]) > max_skew:
        raise ActorAssertionError("stale", "actor assertion is outside the allowed time window")
    if claims["caller"] != token_claims.caller_installation:
        raise ActorAssertionError(
            "caller_mismatch", "actor assertion caller does not match the pair token"
        )
    if claims["depth"] > max_depth:
        raise ActorAssertionError("depth_exceeded", "actor assertion depth exceeds the limit")
    if task_id and claims["task"] != task_id:
        raise ActorAssertionError("task_mismatch", "actor assertion is bound to another task")

    return {key: claims[key] for key in sorted(_CLAIM_KEYS)}
