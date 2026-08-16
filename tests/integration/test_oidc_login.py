"""Real Keycloak, real authorization-code flow — T-043, ADR-046.

Everything else in this package's OIDC tests (`test_escalations_api.py`) fabricates a
session cookie directly rather than running Keycloak, because the escalation *contract*
(404/409/403 mapping) does not depend on how the session got there. This module is the one
place that proves the part that does depend on it: that `/auth/login` builds a redirect
Keycloak actually accepts, that `/auth/callback` can exchange a real authorization code for
a real, JWKS-verified ID token, and that the `sub` claim inside it — not anything a client
could claim — is what ends up as `principal_id`.

Three things this had to work around, found by running it rather than assuming it (`CLAUDE.md`):

1. Keycloak marks its own login-flow cookies (`AUTH_SESSION_ID`, `KC_RESTART`) `Secure` and
   `SameSite=None` unconditionally — realm `sslRequired: none` does not change this (measured
   against Keycloak 26). A real browser on `http://localhost` still sends them back (the W3C
   Secure Contexts spec treats loopback as trustworthy); httpx does not implement that
   exception, so this module threads those two cookies through by hand rather than relying on
   httpx's automatic jar. `agentiam_controlplane.auth`'s login flow itself is unaffected — a
   real browser never hits this wall — this is purely a test-harness workaround.
2. `KeycloakAdmin.create_user` does not honor a caller-supplied `id` — Keycloak always
   generates one. Only a realm-*import* JSON (`deploy/keycloak/realm-export.json`) can pin a
   user's `sub` to a known value, which is why this module imports that file rather than
   creating the realm/client/users through the admin API.
3. `httpx.ASGITransport` implements `AsyncBaseTransport`, not `BaseTransport` — it only
   plugs into `httpx.AsyncClient`, never the sync `httpx.Client`. Driving one client across
   both the in-process app and the real network (via `mounts`) therefore means every request
   in this module is awaited, including the ones that hit real Keycloak.
"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.keycloak import KeycloakContainer

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.settings import ControlPlaneSettings, OIDCSettings
from agentiam_core.tokens import generate_keypair

pytestmark = pytest.mark.integration

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

_REALM_EXPORT = Path(__file__).resolve().parents[2] / "deploy" / "keycloak" / "realm-export.json"
_MANAGER_SUB = "11111111-1111-1111-1111-111111111111"
_CLIENT_ID = "agentiam-console"
_CLIENT_SECRET = "dev-console-secret-change-me"  # noqa: S105 — dev realm-export literal, not a real secret
_APP_HOST = "http://localhost:8000"
_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
_INTENT_HASH = hashlib.sha256(b"an oidc-gated escalation").hexdigest()


@pytest.fixture(scope="module")
def keycloak() -> Generator[KeycloakContainer]:
    """A real Keycloak, realm/client/users pinned by `deploy/keycloak/realm-export.json`."""
    container = KeycloakContainer("quay.io/keycloak/keycloak:26.0").with_realm_import_file(
        str(_REALM_EXPORT)
    )
    with container as kc:
        yield kc


@pytest.fixture(scope="module")
def issuer(keycloak: KeycloakContainer) -> str:
    return f"{keycloak.get_url()}/realms/agentiam"


def _open_body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "decision_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "agent_id": "agt-1",
        "principal_id": "kc:alice",
        "intent_hash": _INTENT_HASH,
        "requested_scopes": ["payment:initiate"],
        "requested_amount": "1000",
        "reason": "real Keycloak round trip",
    }
    return base | over


def _build_client(app: FastAPI) -> httpx.AsyncClient:
    """One client, two transports: our app in-process, everything else over real loopback.

    `mounts` routes by URL prefix — `_APP_HOST` goes straight into the ASGI app, the
    Keycloak base URL (whatever port testcontainers published) goes out over the network.
    """
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {_APP_HOST: httpx.ASGITransport(app=app)}
    return httpx.AsyncClient(follow_redirects=False, mounts=mounts, base_url=_APP_HOST)


async def _drive_keycloak_login(
    client: httpx.AsyncClient, authorize_url: str, *, username: str, password: str
) -> str:
    """From an authorization-endpoint URL to the final `redirect_uri?...code=...` URL.

    Manual cookie threading — see the module docstring, point 1.
    """
    r = await client.get(authorize_url)
    assert r.status_code == 200, r.text
    kc_cookies = "; ".join(c.split(";")[0] for c in r.headers.get_list("set-cookie"))
    match = re.search(r'action="([^"]+)"', r.text)
    assert match, r.text[:2000]
    form_action = match.group(1).replace("&amp;", "&")

    login_request = client.build_request(
        "POST",
        form_action,
        data={"username": username, "password": password},
        headers={"Cookie": kc_cookies},
    )
    login_response = await client.send(login_request)
    location = login_response.headers.get("location")
    assert location, login_response.text[:2000]
    if "code=" not in location:
        # A required action (e.g. VERIFY_PROFILE) interposed — the realm-export users have
        # firstName/lastName/emailVerified set precisely so this branch should not trigger,
        # but following it rather than asserting its absence keeps the test honest about
        # what Keycloak actually did.
        follow_up = await client.get(location, headers={"Cookie": kc_cookies})
        location = follow_up.headers.get("location")
        assert location, follow_up.text[:2000]
    assert "code=" in location
    return str(location)


async def test_login_redirects_to_the_real_authorization_endpoint(
    keycloak: KeycloakContainer, issuer: str, migrated_engine: AsyncEngine
) -> None:
    settings = ControlPlaneSettings(
        root_private_key=generate_keypair().private_key,
        approvers=frozenset({f"kc:{_MANAGER_SUB}"}),
        session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
    )
    oidc = OIDCSettings(issuer=issuer, client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET)
    app = create_app(
        session_factory=make_session_factory(migrated_engine),
        escalation_settings=settings,
        oidc_settings=oidc,
    )

    async with _build_client(app) as client:
        response = await client.get(f"{_APP_HOST}/auth/login")
        assert response.status_code in (302, 307)
        location = response.headers["location"]
        parsed = urllib.parse.urlparse(location)
        qs = urllib.parse.parse_qs(parsed.query)
        assert location.startswith(f"{issuer}/protocol/openid-connect/auth")
        assert qs["client_id"] == [_CLIENT_ID]
        assert qs["response_type"] == ["code"]
        assert qs["redirect_uri"] == [f"{_APP_HOST}/auth/callback"]
        assert "state" in qs  # authlib's CSRF guard — confirms it's actually wired


async def test_full_login_derives_principal_from_the_real_sub_claim(
    keycloak: KeycloakContainer, issuer: str, migrated_engine: AsyncEngine
) -> None:
    factory = make_session_factory(migrated_engine)
    settings = ControlPlaneSettings(
        root_private_key=generate_keypair().private_key,
        approvers=frozenset({f"kc:{_MANAGER_SUB}"}),
        session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
    )
    oidc = OIDCSettings(issuer=issuer, client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET)
    app = create_app(
        session_factory=factory,
        escalation_settings=settings,
        oidc_settings=oidc,
        now=lambda: _NOW,
    )

    async with _build_client(app) as client:
        login_redirect = await client.get(f"{_APP_HOST}/auth/login")
        authorize_url = login_redirect.headers["location"]

        callback_url = await _drive_keycloak_login(
            client,
            authorize_url,
            username="manager",
            password="manager-pw",  # noqa: S106
        )
        assert callback_url.startswith(f"{_APP_HOST}/auth/callback")

        callback_response = await client.get(callback_url)
        assert callback_response.status_code == 303
        assert callback_response.headers["location"] == "/escalations"

        # The session now carries whatever `/auth/callback` derived from the *real*,
        # JWKS-verified ID token — prove it by using it for something session-gated: the
        # fixed realm-export sub is in `approvers`, so approval should succeed.
        opened = await client.post(f"{_APP_HOST}/v1/escalations", json=_open_body())
        assert opened.status_code == 201
        escalation_id = opened.json()["id"]

        approved = await client.post(f"{_APP_HOST}/v1/escalations/{escalation_id}/approve", json={})
        assert approved.status_code == 200, approved.text
        assert approved.json()["scopes"] == ["payment:initiate"]


async def test_a_real_but_unauthorized_principal_is_403(
    keycloak: KeycloakContainer, issuer: str, migrated_engine: AsyncEngine
) -> None:
    """The CFO user logs in for real, but only the manager sub is in `approvers`."""
    factory = make_session_factory(migrated_engine)
    settings = ControlPlaneSettings(
        root_private_key=generate_keypair().private_key,
        approvers=frozenset({f"kc:{_MANAGER_SUB}"}),  # CFO deliberately excluded
        session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
    )
    oidc = OIDCSettings(issuer=issuer, client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET)
    app = create_app(
        session_factory=factory,
        escalation_settings=settings,
        oidc_settings=oidc,
        now=lambda: _NOW,
    )

    async with _build_client(app) as client:
        login_redirect = await client.get(f"{_APP_HOST}/auth/login")
        authorize_url = login_redirect.headers["location"]
        callback_url = await _drive_keycloak_login(
            client,
            authorize_url,
            username="cfo",
            password="cfo-pw",  # noqa: S106
        )
        callback_response = await client.get(callback_url)
        assert callback_response.status_code == 303

        opened = await client.post(f"{_APP_HOST}/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]

        denied = await client.post(f"{_APP_HOST}/v1/escalations/{escalation_id}/approve", json={})
        assert denied.status_code == 403
