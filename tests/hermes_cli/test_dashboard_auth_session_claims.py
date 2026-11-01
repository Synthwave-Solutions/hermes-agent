"""SSO claims → Session → request.state → governance subject (plan Task 1.4).

Covers the three layers the claims pipeline crosses:

1. ``Session`` backwards compatibility: the new ``groups`` / ``roles`` /
   ``claims`` fields are optional, so every existing 8-field constructor
   (nous, basic, drain, stub) keeps working and yields empty facets.
2. Self-hosted OIDC claim extraction: ``_session_from_tokens`` maps the
   verified ID-token payload onto the Session facets: configurable group
   claim (default ``groups``), Google ``hd`` + email pseudo-groups, an
   opt-in role claim (default: roles are NOT extracted, because role names
   map directly onto governance policy role names), and a
   protocol-claims-stripped identity dict. No tokens ever leak into
   ``Session.claims``.
3. Middleware attachment: ``gated_auth_middleware`` sets
   ``request.state.groups`` / ``roles`` / ``claims`` alongside
   ``request.state.session`` on BOTH the verify path and the transparent
   refresh path, and ``dashboard_governance.enforcement.subject_from_request``
   picks them up unchanged.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.cookies import (
    SESSION_AT_COOKIE,
    SESSION_RT_COOKIE,
)
from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
from hermes_cli.dashboard_governance.enforcement import subject_from_request
from plugins.dashboard_auth.self_hosted import SelfHostedOIDCProvider
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider, _sign


_LEGACY_KWARGS = dict(
    user_id="u-1",
    email="user@example.test",
    display_name="User One",
    org_id="org-1",
    provider="stub",
    expires_at=4102444800,
    access_token="at",
    refresh_token="rt",
)


# ---------------------------------------------------------------------------
# 1. Session dataclass backwards compatibility
# ---------------------------------------------------------------------------


class TestSessionBackwardsCompat:
    def test_legacy_eight_field_construction_still_works(self):
        """Old-style sessions (every non-OIDC provider) deserialize unchanged."""
        session = Session(**_LEGACY_KWARGS)
        assert session.groups == ()
        assert session.roles == ()
        assert session.claims == {}

    def test_facets_round_trip(self):
        session = Session(
            **_LEGACY_KWARGS,
            groups=("engineering", "example.test"),
            roles=("admin",),
            claims={"email": "user@example.test", "hd": "example.test"},
        )
        assert session.groups == ("engineering", "example.test")
        assert session.roles == ("admin",)
        assert session.claims["hd"] == "example.test"

    def test_session_stays_frozen(self):
        session = Session(**_LEGACY_KWARGS)
        with pytest.raises(Exception):
            session.groups = ("nope",)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Self-hosted OIDC claim extraction (fake verified id_token payload;
#    _session_from_tokens never touches the network)
# ---------------------------------------------------------------------------


def _oidc_provider(**kwargs) -> SelfHostedOIDCProvider:
    return SelfHostedOIDCProvider(
        issuer="https://auth.example.com/application/o/hermes",
        client_id="hermes-dashboard",
        **kwargs,
    )


def _base_claims(**extra) -> dict:
    claims = {
        "sub": "oidc-user-1",
        "email": "dev@example.test",
        "name": "Dev User",
        "iss": "https://auth.example.com/application/o/hermes",
        "aud": "hermes-dashboard",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "nonce": "abc123",
        "at_hash": "deadbeef",
    }
    claims.update(extra)
    return claims


class TestOIDCClaimExtraction:
    def test_groups_list_claim_reaches_session(self):
        provider = _oidc_provider()
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="",
            claims=_base_claims(groups=["engineering", "platform"]),
        )
        assert "engineering" in session.groups
        assert "platform" in session.groups
        # Email is always appended as a pseudo-group for policy matching.
        assert "dev@example.test" in session.groups

    def test_configurable_group_claim_name(self):
        provider = _oidc_provider(group_claim="hermes_teams")
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="",
            claims=_base_claims(
                hermes_teams=["ops"], groups=["ignored-default"]
            ),
        )
        assert "ops" in session.groups
        assert "ignored-default" not in session.groups

    def test_google_hd_and_email_become_pseudo_groups(self):
        """Google ID tokens carry no groups claim, only hd + email."""
        provider = _oidc_provider()
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="",
            claims=_base_claims(hd="synthwave.solutions"),
        )
        assert session.groups == (
            "synthwave.solutions",
            "dev@example.test",
        )

    def test_comma_separated_string_groups_are_split(self):
        provider = _oidc_provider()
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="",
            claims=_base_claims(groups="alpha, beta gamma"),
        )
        for group in ("alpha", "beta", "gamma"):
            assert group in session.groups

    def test_roles_claim_reaches_session_when_opted_in(self):
        provider = _oidc_provider(role_claim="roles")
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="",
            claims=_base_claims(roles=["admin", "operator"]),
        )
        assert session.roles == ("admin", "operator")

    def test_roles_claim_is_ignored_without_optin(self):
        """Role names map 1:1 onto governance policy roles, so an IDP-emitted
        ``roles`` claim (e.g. Azure AD app roles) must never grant policy
        roles unless the operator explicitly configured a role claim."""
        provider = _oidc_provider()
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="",
            claims=_base_claims(roles=["admin", "operator"]),
        )
        assert session.roles == ()

    def test_protocol_claims_stripped_and_no_tokens_stored(self):
        provider = _oidc_provider()
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="refresh-secret",
            claims=_base_claims(groups=["engineering"], hd="example.test"),
        )
        # Identity claims survive.
        assert session.claims["email"] == "dev@example.test"
        assert session.claims["hd"] == "example.test"
        assert session.claims["groups"] == ["engineering"]
        # Protocol plumbing is stripped.
        for protocol_claim in ("exp", "iat", "aud", "iss", "nonce", "at_hash"):
            assert protocol_claim not in session.claims
        # Never any token material in the claims dict.
        serialized = str(session.claims)
        assert "fake-id-token" not in serialized
        assert "refresh-secret" not in serialized

    def test_no_group_claims_yields_email_only(self):
        provider = _oidc_provider()
        session = provider._session_from_tokens(
            id_token="fake-id-token",
            refresh_token="",
            claims=_base_claims(),
        )
        assert session.groups == ("dev@example.test",)
        assert session.roles == ()


# ---------------------------------------------------------------------------
# 3. Middleware attaches request.state.groups/roles/claims; the governance
#    subject builder picks them up
# ---------------------------------------------------------------------------


class ClaimsStubProvider(StubAuthProvider):
    """Stub whose sessions carry SSO facets, like an OIDC provider's would."""

    name = "claims-stub"
    display_name = "Claims Stub (test only)"

    def _with_facets(self, session: Session) -> Session:
        return Session(
            user_id=session.user_id,
            email=session.email,
            display_name=session.display_name,
            org_id=session.org_id,
            provider=session.provider,
            expires_at=session.expires_at,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            groups=("engineering", "stub@example.test"),
            roles=("operator",),
            claims={"email": session.email, "hd": "example.test"},
        )

    def verify_session(self, *, access_token: str):
        session = super().verify_session(access_token=access_token)
        return self._with_facets(session) if session is not None else None

    def refresh_session(self, *, refresh_token: str) -> Session:
        return self._with_facets(
            super().refresh_session(refresh_token=refresh_token)
        )


def _echo_app() -> FastAPI:
    app = FastAPI()
    app.state.auth_required = True

    @app.get("/api/echo-governance")
    async def echo(request: Request):  # pragma: no cover - exercised via client
        subject = subject_from_request(request)
        return {
            "state_groups": list(getattr(request.state, "groups", ())),
            "state_roles": list(getattr(request.state, "roles", ())),
            "state_claims": dict(getattr(request.state, "claims", {}) or {}),
            "subject_groups": list(subject.groups),
            "subject_roles": list(subject.roles),
            "subject_email": subject.email,
        }

    app.middleware("http")(gated_auth_middleware)
    return app


@pytest.fixture
def claims_client():
    clear_providers()
    register_provider(ClaimsStubProvider())
    client = TestClient(_echo_app())
    yield client
    clear_providers()


@pytest.fixture
def legacy_client():
    clear_providers()
    register_provider(StubAuthProvider())
    client = TestClient(_echo_app())
    yield client
    clear_providers()


def _valid_access_token(ttl: int = 3600) -> str:
    exp = int(time.time()) + ttl
    return _sign({
        "sub": "stub-user-1",
        "email": "stub@example.test",
        "name": "Stub User",
        "org_id": "stub-org-1",
        "exp": exp,
    })


def _valid_refresh_token() -> str:
    return _sign({
        "sub": "stub-user-1",
        "kind": "refresh",
        "exp": int(time.time()) + 30 * 86400,
    })


class TestRequestStateAttachment:
    def test_verify_path_populates_state_and_subject(self, claims_client):
        claims_client.cookies.set(SESSION_AT_COOKIE, _valid_access_token())
        r = claims_client.get("/api/echo-governance")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state_groups"] == ["engineering", "stub@example.test"]
        assert body["state_roles"] == ["operator"]
        assert body["state_claims"]["hd"] == "example.test"
        # enforcement.subject_from_request consumed them unchanged.
        assert body["subject_groups"] == ["engineering", "stub@example.test"]
        assert body["subject_roles"] == ["operator"]
        assert body["subject_email"] == "stub@example.test"

    def test_refresh_path_populates_state(self, claims_client):
        # Only a refresh-token cookie: middleware takes the transparent
        # refresh path (the second attach point).
        claims_client.cookies.set(SESSION_RT_COOKIE, _valid_refresh_token())
        r = claims_client.get("/api/echo-governance")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state_groups"] == ["engineering", "stub@example.test"]
        assert body["subject_roles"] == ["operator"]

    def test_legacy_provider_yields_empty_facets(self, legacy_client):
        """Providers without facet support degrade to empty, not errors."""
        legacy_client.cookies.set(SESSION_AT_COOKIE, _valid_access_token())
        r = legacy_client.get("/api/echo-governance")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state_groups"] == []
        assert body["state_roles"] == []
        assert body["state_claims"] == {}
        assert body["subject_groups"] == []
        assert body["subject_email"] == "stub@example.test"
