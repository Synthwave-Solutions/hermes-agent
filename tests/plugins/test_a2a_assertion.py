"""Contract tests for plugins/platforms/a2a/synthpulse_assertion.py.

Pair tokens and signed actor assertions (plan Appendix F). The helper
``_spec_header`` rebuilds the header straight from the written formula, so
these tests check the wire format itself and not only that sign and verify
agree with each other.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

from plugins.platforms.a2a import synthpulse_assertion as sa
from plugins.platforms.a2a.synthpulse_assertion import (
    ActorAssertionError,
    PairTokenClaims,
    PairTokenError,
    canonical_json,
    parse_pair_token,
    sign_actor,
    verify_actor,
)

SECRET = "Zq3vT0kP9xW2mN8rB4yL6cH1jF5dG7sA"
TOKEN = f"a2a1.org_acme.inst_a.inst_b.{SECRET}"
HQ_TOKEN = f"a2a1.-.inst_hq.inst_b.{SECRET}"
NOW = 1_790_000_000


def _claims(**overrides):
    claims = {
        "actor": "alice@acme.example",
        "caller": "inst_a",
        "depth": 0,
        "task": "",
        "ts": NOW,
    }
    claims.update(overrides)
    return claims


def _spec_header(pair_token: str, payload: bytes, *, version: str = "v1") -> str:
    """Appendix F, written out independently of the module under test."""
    part = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    key = hashlib.sha256(("sp-a2a-actor-v1|" + pair_token).encode("utf-8")).digest()
    mac = hmac.new(key, f"{version}.{part}".encode("ascii"), hashlib.sha256).hexdigest()
    return f"{version}.{part}.{mac}"


def _reason(pair_token, header, **kwargs):
    kwargs.setdefault("now", NOW)
    with pytest.raises(ActorAssertionError) as info:
        verify_actor(pair_token, header, **kwargs)
    return info.value.reason


# ---------------------------------------------------------------------------
# Pair tokens
# ---------------------------------------------------------------------------


def test_parse_pair_token_fields():
    assert parse_pair_token(TOKEN) == PairTokenClaims(
        org_id="org_acme", caller_installation="inst_a", callee_installation="inst_b"
    )


def test_parse_hq_token_has_no_organisation():
    claims = parse_pair_token(HQ_TOKEN)
    assert claims.org_id is None
    assert claims.caller_installation == "inst_hq"
    assert claims.callee_installation == "inst_b"


def test_pair_token_claims_never_hold_the_secret():
    claims = parse_pair_token(TOKEN)
    assert SECRET not in repr(claims)
    assert SECRET not in json.dumps(dataclasses.asdict(claims))
    with pytest.raises(dataclasses.FrozenInstanceError):
        claims.org_id = "org_other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        123,
        b"a2a1.org_acme.inst_a.inst_b." + SECRET.encode(),
        "tok1",
        f"a2a2.org_acme.inst_a.inst_b.{SECRET}",
        f"A2A1.org_acme.inst_a.inst_b.{SECRET}",
        "a2a1.org_acme.inst_a.inst_b",
        f"a2a1.org_acme.inst_a.inst_b.{SECRET}.extra",
        f"a2a1.org_acme.inst_a.inst_b.{SECRET[:8]}.{SECRET[8:]}",
        f"a2a1..inst_a.inst_b.{SECRET}",
        f"a2a1.org_acme..inst_b.{SECRET}",
        f"a2a1.org_acme.inst_a..{SECRET}",
        "a2a1.org_acme.inst_a.inst_b.",
        f"a2a1.--.inst_a.inst_b.{SECRET}",
        f"a2a1.-org.inst_a.inst_b.{SECRET}",
        f"a2a1.org acme.inst_a.inst_b.{SECRET}",
        f"a2a1.org_acme.inst:a.inst_b.{SECRET}",
        f"a2a1.org_acme.inst_a.inst_b/x.{SECRET}",
        f"a2a1.org_acme.{'i' * 65}.inst_b.{SECRET}",
        "a2a1.org_acme.inst_a.inst_b.tooshort",
        f"a2a1.org_acme.inst_a.inst_b.{SECRET[:-1]}+",
        f"a2a1.org_acme.inst_a.inst_b.{SECRET}=",
        f" {TOKEN}",
        f"{TOKEN}\n",
        f"a2a1.org_acme.inst_a.inst_b.{'s' * 2000}",
    ],
)
def test_parse_rejects_malformed_tokens(token):
    with pytest.raises(PairTokenError):
        parse_pair_token(token)


def test_pair_token_error_is_a_value_error_without_the_secret():
    bad = f"a2a1.org acme.inst_a.inst_b.{SECRET}"
    with pytest.raises(ValueError) as info:
        parse_pair_token(bad)
    assert SECRET not in str(info.value)
    assert bad not in str(info.value)


# ---------------------------------------------------------------------------
# Canonical form and the wire format
# ---------------------------------------------------------------------------


def test_canonical_json_sorts_keys_without_spaces():
    shuffled = {"ts": NOW, "task": "t1", "depth": 1, "caller": "inst_a", "actor": "alice@acme.example"}
    assert canonical_json(shuffled) == (
        '{"actor":"alice@acme.example","caller":"inst_a","depth":1,"task":"t1","ts":1790000000}'
    )


def test_canonical_json_is_ascii_only():
    text = canonical_json(_claims(actor="zo\u00eb@acme.example"))
    assert text.isascii()
    assert "\\u00eb" in text


def test_sign_matches_the_written_formula():
    header = sign_actor(TOKEN, _claims())
    expected_payload = (
        b'{"actor":"alice@acme.example","caller":"inst_a","depth":0,"task":"","ts":1790000000}'
    )
    assert header == _spec_header(TOKEN, expected_payload)


def test_header_shape():
    header = sign_actor(TOKEN, _claims(task="task-123", depth=1))
    version, payload, mac = header.split(".")
    assert version == "v1"
    assert "=" not in payload
    assert set(payload) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert len(mac) == 64 and mac == mac.lower()
    int(mac, 16)
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    assert json.loads(decoded) == _claims(task="task-123", depth=1)


def test_header_name_constant():
    assert sa.ACTOR_HEADER == "X-SP-A2A-Actor"


# ---------------------------------------------------------------------------
# sign_actor
# ---------------------------------------------------------------------------


def test_round_trip_returns_the_claims():
    claims = _claims(task="task-9", depth=2)
    assert verify_actor(TOKEN, sign_actor(TOKEN, claims), now=NOW) == claims


def test_round_trip_for_hq_tokens():
    claims = _claims(caller="inst_hq")
    assert verify_actor(HQ_TOKEN, sign_actor(HQ_TOKEN, claims), now=NOW) == claims


def test_sign_normalises_the_actor_email():
    header = sign_actor(TOKEN, _claims(actor="  Alice@ACME.example "))
    assert verify_actor(TOKEN, header, now=NOW)["actor"] == "alice@acme.example"


@pytest.mark.parametrize("task", [None, "missing"])
def test_sign_task_defaults_to_empty(task):
    claims = _claims()
    if task == "missing":
        del claims["task"]
    else:
        claims["task"] = task
    assert verify_actor(TOKEN, sign_actor(TOKEN, claims), now=NOW)["task"] == ""


def test_sign_does_not_mutate_claims():
    claims = _claims(actor="Alice@Acme.Example")
    del claims["task"]
    snapshot = dict(claims)
    sign_actor(TOKEN, claims)
    assert claims == snapshot


@pytest.mark.parametrize(
    "claims",
    [
        None,
        "alice@acme.example",
        {k: v for k, v in _claims().items() if k != "actor"},
        {k: v for k, v in _claims().items() if k != "caller"},
        {k: v for k, v in _claims().items() if k != "depth"},
        {k: v for k, v in _claims().items() if k != "ts"},
        _claims(extra="x"),
        _claims(actor=""),
        _claims(actor="alice"),
        _claims(actor="alice smith@acme.example"),
        _claims(actor="a" * 250 + "@acme.example"),
        _claims(actor=None),
        _claims(caller=""),
        _claims(caller="inst a"),
        _claims(caller=None),
        _claims(depth=-1),
        _claims(depth=True),
        _claims(depth=1.0),
        _claims(depth="1"),
        _claims(ts=0),
        _claims(ts=-5),
        _claims(ts=True),
        _claims(ts=float(NOW)),
        _claims(ts=str(NOW)),
        _claims(ts=2**60),
        _claims(task="has space"),
        _claims(task="line\nbreak"),
        _claims(task="t" * 257),
        _claims(task=7),
    ],
)
def test_sign_rejects_invalid_claims(claims):
    with pytest.raises(ValueError):
        sign_actor(TOKEN, claims)


def test_sign_refuses_a_caller_other_than_the_token_caller():
    with pytest.raises(ValueError, match="caller"):
        sign_actor(TOKEN, _claims(caller="inst_x"))


@pytest.mark.parametrize("token", ["tok1", "", None, "a2a1.org_acme.inst_a.inst_b.short"])
def test_sign_refuses_tokens_that_are_not_pair_tokens(token):
    with pytest.raises(PairTokenError):
        sign_actor(token, _claims())


# ---------------------------------------------------------------------------
# verify_actor: signature and token binding
# ---------------------------------------------------------------------------


def test_verify_refuses_another_secret():
    other = f"a2a1.org_acme.inst_a.inst_b.{SECRET[::-1]}"
    assert _reason(other, sign_actor(TOKEN, _claims())) == "bad_signature"


@pytest.mark.parametrize(
    "other",
    [
        f"a2a1.org_acme.inst_a.inst_c.{SECRET}",
        f"a2a1.org_beta.inst_a.inst_b.{SECRET}",
        f"a2a1.-.inst_a.inst_b.{SECRET}",
    ],
)
def test_signature_is_bound_to_the_whole_token(other):
    assert _reason(other, sign_actor(TOKEN, _claims())) == "bad_signature"


def test_verify_refuses_a_tampered_payload():
    header = sign_actor(TOKEN, _claims())
    _, _, mac = header.split(".")
    forged_payload = base64.urlsafe_b64encode(
        canonical_json(_claims(actor="admin@acme.example")).encode()
    ).rstrip(b"=").decode()
    assert _reason(TOKEN, f"v1.{forged_payload}.{mac}") == "bad_signature"


@pytest.mark.parametrize("position", [0, 31, 63])
def test_verify_refuses_a_tampered_mac(position):
    header = sign_actor(TOKEN, _claims())
    head, payload, mac = header.split(".")
    replacement = "0" if mac[position] != "0" else "1"
    flipped = mac[:position] + replacement + mac[position + 1:]
    assert _reason(TOKEN, f"{head}.{payload}.{flipped}") == "bad_signature"


def test_mac_is_checked_before_the_payload_is_parsed():
    junk = base64.urlsafe_b64encode(b"not json at all").rstrip(b"=").decode()
    assert _reason(TOKEN, f"v1.{junk}.{'0' * 64}") == "bad_signature"
    assert _reason(TOKEN, _spec_header(TOKEN, b"not json at all")) == "malformed"


@pytest.mark.parametrize("token", ["tok1", "", None, "a2a1.org_acme.inst_a.inst_b.short"])
def test_verify_refuses_tokens_that_are_not_pair_tokens(token):
    header = sign_actor(TOKEN, _claims())
    assert _reason(token, header) == "bad_token"


# ---------------------------------------------------------------------------
# verify_actor: header shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", [None, "", "   "])
def test_verify_missing_header(header):
    assert _reason(TOKEN, header) == "missing"


def _malformed_headers():
    good = sign_actor(TOKEN, _claims())
    version, payload, mac = good.split(".")
    padded = payload + "=" * (-len(payload) % 4 or 4)
    return [
        f"v2.{payload}.{mac}",
        f"V1.{payload}.{mac}",
        f"{payload}.{mac}",
        f"v1.{payload}.{mac}.extra",
        f"v1..{mac}",
        f"v1.{payload}.",
        f"v1.{padded}.{mac}",
        f"v1.{payload}.{mac.upper()}",
        f"v1.{payload}.{mac[:-2]}",
        f"v1.{payload}.{mac}00",
        f"v1.{payload.replace('-', '+')}+/.{mac}",
        f"v1.{payload} .{mac}",
        good.encode(),
        12345,
        "v1." + "A" * 5000 + "." + mac,
    ]


@pytest.mark.parametrize("index", range(len(_malformed_headers())))
def test_verify_malformed_headers(index):
    header = _malformed_headers()[index]
    assert _reason(TOKEN, header) == "malformed"


def test_verify_tolerates_surrounding_whitespace():
    header = sign_actor(TOKEN, _claims())
    assert verify_actor(TOKEN, f"  {header}\t", now=NOW) == _claims()


# ---------------------------------------------------------------------------
# verify_actor: canonical payload (MAC valid, content refused)
# ---------------------------------------------------------------------------

_CANON = canonical_json(_claims())


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps(_claims(), sort_keys=True).encode(),  # spaces
        json.dumps(dict(reversed(list(_claims().items()))), separators=(",", ":")).encode(),  # unsorted
        _CANON.replace('"ts":', '"extra":1,"ts":').encode(),
        _CANON.replace(',"task":""', "").encode(),
        _CANON.replace('{"actor"', '{"actor":"eve@acme.example","actor"').encode(),
        _CANON.replace('"depth":0', '"depth":false').encode(),
        _CANON.replace('"depth":0', '"depth":0.0').encode(),
        _CANON.replace('"ts":1790000000', '"ts":1790000000.0').encode(),
        _CANON.replace('"ts":1790000000', '"ts":NaN').encode(),
        _CANON.replace('"ts":1790000000', '"ts":"1790000000"').encode(),
        _CANON.replace('"task":""', '"task":{"id":"x"}').encode(),
        _CANON.replace("alice", "Alice").encode(),
        _CANON.replace("alice@acme.example", "zo\u00eb@acme.example").encode("utf-8"),
        b"[" + _CANON.encode() + b"]",
        b"null",
        _CANON.encode() + b"\n",
        b"\xff\xfe",
    ],
)
def test_verify_refuses_non_canonical_payloads(payload):
    assert _reason(TOKEN, _spec_header(TOKEN, payload)) == "malformed"


def test_verify_accepts_an_escaped_non_ascii_actor():
    claims = _claims(actor="zo\u00eb@acme.example")
    header = _spec_header(TOKEN, canonical_json(claims).encode("ascii"))
    assert verify_actor(TOKEN, header, now=NOW)["actor"] == "zo\u00eb@acme.example"


# ---------------------------------------------------------------------------
# verify_actor: freshness, caller, depth, task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [-300, -1, 0, 1, 300])
def test_timestamps_inside_the_window_pass(offset):
    header = sign_actor(TOKEN, _claims(ts=NOW + offset))
    assert verify_actor(TOKEN, header, now=NOW)["ts"] == NOW + offset


@pytest.mark.parametrize("offset", [-301, 301, -86400, 86400])
def test_timestamps_outside_the_window_are_stale(offset):
    assert _reason(TOKEN, sign_actor(TOKEN, _claims(ts=NOW + offset))) == "stale"


def test_custom_max_skew():
    header = sign_actor(TOKEN, _claims(ts=NOW - 61))
    assert _reason(TOKEN, header, max_skew=60) == "stale"
    assert verify_actor(TOKEN, header, now=NOW, max_skew=61)["ts"] == NOW - 61


def test_float_now_is_accepted():
    header = sign_actor(TOKEN, _claims())
    assert verify_actor(TOKEN, header, now=NOW + 299.5)["ts"] == NOW
    assert _reason(TOKEN, header, now=NOW + 300.5) == "stale"


def test_caller_must_equal_the_token_caller():
    # Signed with the right token, but claiming another caller installation.
    header = _spec_header(TOKEN, canonical_json(_claims(caller="inst_x")).encode())
    assert _reason(TOKEN, header) == "caller_mismatch"


def test_hq_token_caller_is_checked_too():
    header = _spec_header(HQ_TOKEN, canonical_json(_claims(caller="inst_a")).encode())
    assert _reason(HQ_TOKEN, header) == "caller_mismatch"


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_depth_up_to_the_default_cap_passes(depth):
    header = sign_actor(TOKEN, _claims(depth=depth))
    assert verify_actor(TOKEN, header, now=NOW)["depth"] == depth


@pytest.mark.parametrize("depth", [3, 10])
def test_depth_over_the_default_cap_is_refused(depth):
    assert sa.DEFAULT_MAX_DEPTH == 2
    assert _reason(TOKEN, sign_actor(TOKEN, _claims(depth=depth))) == "depth_exceeded"


def test_custom_depth_cap():
    header = sign_actor(TOKEN, _claims(depth=3))
    assert verify_actor(TOKEN, header, now=NOW, max_depth=3)["depth"] == 3
    header0 = sign_actor(TOKEN, _claims(depth=1))
    assert _reason(TOKEN, header0, max_depth=0) == "depth_exceeded"


def test_task_must_match_when_the_request_names_one():
    header = sign_actor(TOKEN, _claims(task="task-1"))
    assert verify_actor(TOKEN, header, now=NOW, task_id="task-1")["task"] == "task-1"
    assert _reason(TOKEN, header, task_id="task-2") == "task_mismatch"


def test_empty_task_claim_is_refused_for_a_named_task():
    header = sign_actor(TOKEN, _claims(task=""))
    assert _reason(TOKEN, header, task_id="task-1") == "task_mismatch"


@pytest.mark.parametrize("task_id", [None, ""])
def test_task_is_not_checked_when_the_request_names_none(task_id):
    header = sign_actor(TOKEN, _claims(task="task-1"))
    assert verify_actor(TOKEN, header, now=NOW, task_id=task_id)["task"] == "task-1"


def test_verify_requires_keyword_now():
    header = sign_actor(TOKEN, _claims())
    with pytest.raises(TypeError):
        verify_actor(TOKEN, header, NOW)  # type: ignore[misc]
    with pytest.raises(TypeError):
        verify_actor(TOKEN, header)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Errors never leak secrets
# ---------------------------------------------------------------------------


def test_errors_never_include_the_token_or_mac():
    good = sign_actor(TOKEN, _claims())
    mac = good.rsplit(".", 1)[1]
    cases = [
        (TOKEN, None, {}),
        (TOKEN, "garbage", {}),
        (f"a2a1.org_acme.inst_a.inst_b.{SECRET[::-1]}", good, {}),
        ("a2a1.bad token", good, {}),
        (TOKEN, good, {"now": NOW + 10_000}),
        (TOKEN, good, {"task_id": "other"}),
    ]
    for token, header, kwargs in cases:
        kwargs = {"now": NOW, **kwargs}
        with pytest.raises(ActorAssertionError) as info:
            verify_actor(token, header, **kwargs)
        message = str(info.value)
        assert SECRET not in message and SECRET[::-1] not in message
        assert mac not in message
        assert isinstance(info.value, ValueError)
        assert info.value.reason in {
            "missing",
            "malformed",
            "bad_token",
            "bad_signature",
            "stale",
            "caller_mismatch",
            "depth_exceeded",
            "task_mismatch",
        }


# ---------------------------------------------------------------------------
# Module shape
# ---------------------------------------------------------------------------


def test_module_imports_only_the_standard_library():
    tree = ast.parse(Path(sa.__file__).read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no package-relative imports"
            assert (node.module or "").split(".")[0] in stdlib, node.module


def test_public_names_are_exported():
    for name in sa.__all__:
        assert hasattr(sa, name), name
