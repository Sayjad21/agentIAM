"""Integration tests for `/v1/escalations` and its console page — T-037, T-043.

Real Postgres via testcontainers, and a real in-process HTTP round trip via
`httpx.ASGITransport` — the router, the persistence layer and `mint_root` all have to agree
for `POST .../approve` to hand back a token that actually verifies.

T-043 (ADR-046) replaced the request-body `approver` field with a real session: these tests
authenticate by building a session cookie exactly the way `starlette.middleware.sessions
.SessionMiddleware` does (signed with the same secret `_SETTINGS` carries) rather than
running a real Keycloak — that full login round trip is `test_oidc_login.py`'s job. This
file is about the escalation contract (404/409/403/400 mapping, token minting), which does
not change depending on how the session got there.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import itsdangerous
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.tokens import RootKeySet, generate_keypair, verify

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
_INTENT_HASH = hashlib.sha256(b"an escalation intent").hexdigest()
_KEY_PAIR = generate_keypair()
_SETTINGS = ControlPlaneSettings(
    root_private_key=_KEY_PAIR.private_key,
    approvers=frozenset({"kc:manager", "kc:cfo"}),
    session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
)


def _session_cookie(principal_id: str, *, secret: str = _SETTINGS.session_secret_key) -> str:
    """A cookie value `SessionMiddleware` will accept as-is — same encoding, same signer."""
    payload = base64.b64encode(json.dumps({"principal_id": principal_id}).encode("utf-8"))
    signed = itsdangerous.TimestampSigner(secret).sign(payload)
    return signed.decode("utf-8")


def _open_body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "decision_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "agent_id": "agt-1",
        "principal_id": "kc:alice",
        "intent_hash": _INTENT_HASH,
        "requested_scopes": ["payment:initiate"],
        "requested_amount": "50000",
        "reason": "invoice exceeds the standing ceiling",
    }
    return base | over


async def _client(
    migrated_engine: AsyncEngine, *, signed_in_as: str | None = None
) -> httpx.AsyncClient:
    factory = make_session_factory(migrated_engine)
    app = create_app(session_factory=factory, escalation_settings=_SETTINGS, now=lambda: _NOW)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    if signed_in_as is not None:
        client.cookies.set("session", _session_cookie(signed_in_as))
    return client


async def test_open_list_and_approve_mints_a_verifiable_token(
    migrated_engine: AsyncEngine,
) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        assert opened.status_code == 201
        escalation_id = opened.json()["id"]

        listed = await client.get("/v1/escalations", params={"state": "pending"})
        assert listed.status_code == 200
        assert [e["id"] for e in listed.json()] == [escalation_id]

        approved = await client.post(
            f"/v1/escalations/{escalation_id}/approve",
            json={"elevation_ttl_s": 300},
        )
        assert approved.status_code == 200
        body = approved.json()
        assert body["scopes"] == ["payment:initiate"]
        assert body["amount"] == "50000.0000"

        key_set = RootKeySet([_KEY_PAIR.public_key])
        verified = verify(body["elevated_token"], key_set, now=_NOW + timedelta(seconds=1))
        assert verified.scopes == frozenset({"payment:initiate"})
        assert verified.budget.spend_bdt == Decimal("50000.0000")

        # No longer pending.
        listed_again = await client.get("/v1/escalations", params={"state": "pending"})
        assert listed_again.json() == []


async def test_approving_without_a_session_is_401(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]

    async with await _client(migrated_engine) as anon_client:
        result = await anon_client.post(f"/v1/escalations/{escalation_id}/approve", json={})
        assert result.status_code == 401


async def test_denying_without_a_session_is_401(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]

    async with await _client(migrated_engine) as anon_client:
        result = await anon_client.post(
            f"/v1/escalations/{escalation_id}/deny", json={"reason": "no session"}
        )
        assert result.status_code == 401


async def test_a_forged_session_cookie_is_rejected_like_no_session(
    migrated_engine: AsyncEngine,
) -> None:
    """Wrong secret -> `SessionMiddleware` can't verify the signature -> empty session -> 401.

    Not a 403 (`ApproverNotAuthorized`, which only fires once a session *did* resolve to
    some identity) — a bad signature never reaches identity resolution at all.
    """
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]

    async with await _client(migrated_engine) as forged_client:
        forged_client.cookies.set(
            "session",
            _session_cookie("kc:manager", secret="wrong-secret"),  # noqa: S106
        )
        result = await forged_client.post(f"/v1/escalations/{escalation_id}/approve", json={})
        assert result.status_code == 401


async def test_approving_twice_is_409(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as manager_client:
        opened = await manager_client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]
        first = await manager_client.post(f"/v1/escalations/{escalation_id}/approve", json={})
        assert first.status_code == 200

    async with await _client(migrated_engine, signed_in_as="kc:cfo") as cfo_client:
        second = await cfo_client.post(f"/v1/escalations/{escalation_id}/approve", json={})
        assert second.status_code == 409


async def test_an_unauthorized_approver_is_403(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]

    async with await _client(migrated_engine, signed_in_as="kc:intruder") as intruder_client:
        result = await intruder_client.post(f"/v1/escalations/{escalation_id}/approve", json={})
        assert result.status_code == 403


async def test_approving_an_unknown_id_is_404(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        result = await client.post(f"/v1/escalations/{uuid.uuid4()}/approve", json={})
        assert result.status_code == 404


async def test_narrowing_beyond_the_request_is_400(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post(
            "/v1/escalations", json=_open_body(requested_scopes=["invoice:read"])
        )
        escalation_id = opened.json()["id"]
        result = await client.post(
            f"/v1/escalations/{escalation_id}/approve",
            json={"narrowed_scopes": ["invoice:read", "payment:initiate"]},
        )
        assert result.status_code == 400


async def test_narrowing_the_amount_mints_a_smaller_grant(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post("/v1/escalations", json=_open_body(requested_amount="50000"))
        escalation_id = opened.json()["id"]
        result = await client.post(
            f"/v1/escalations/{escalation_id}/approve",
            json={"max_amount": "20000"},
        )
        assert result.status_code == 200
        body = result.json()
        # `max_amount` bypasses the escalation's stored (DB `Numeric(20,4)`-quantized)
        # `requested_amount` entirely, so the grant carries the request body's own Decimal
        # precision rather than the zero-padded form `str(escalation.requested_amount)`
        # would have. Compare numerically, not as the exact string.
        assert Decimal(body["amount"]) == Decimal("20000")

        key_set = RootKeySet([_KEY_PAIR.public_key])
        verified = verify(body["elevated_token"], key_set, now=_NOW + timedelta(seconds=1))
        assert verified.budget.spend_bdt == Decimal("20000")


async def test_narrowing_the_amount_above_the_request_is_400(
    migrated_engine: AsyncEngine,
) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as client:
        opened = await client.post("/v1/escalations", json=_open_body(requested_amount="50000"))
        escalation_id = opened.json()["id"]
        result = await client.post(
            f"/v1/escalations/{escalation_id}/approve",
            json={"max_amount": "50001"},
        )
        assert result.status_code == 400


async def test_deny_records_the_reason(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:cfo") as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]
        result = await client.post(
            f"/v1/escalations/{escalation_id}/deny",
            json={"reason": "vendor not on the approved list"},
        )
        assert result.status_code == 200
        body = result.json()
        assert body["state"] == "denied"
        assert body["resolution_reason"] == "vendor not on the approved list"


async def test_the_console_queue_page_lists_a_pending_escalation(
    migrated_engine: AsyncEngine,
) -> None:
    async with await _client(migrated_engine) as client:
        await client.post("/v1/escalations", json=_open_body(agent_id="agt-visible"))
        page = await client.get("/escalations")
        assert page.status_code == 200
        assert "agt-visible" in page.text


async def test_the_console_queue_page_shows_signed_in_state(
    migrated_engine: AsyncEngine,
) -> None:
    async with await _client(migrated_engine, signed_in_as="kc:manager") as signed_in:
        page = await signed_in.get("/escalations")
        assert page.status_code == 200
        assert "kc:manager" in page.text
        assert "Sign out" in page.text

    async with await _client(migrated_engine) as anon:
        page = await anon.get("/escalations")
        assert page.status_code == 200
        assert "Sign out" not in page.text


async def test_the_console_queue_page_offers_narrowing_controls(
    migrated_engine: AsyncEngine,
) -> None:
    """T-050: a scope checkbox per requested scope, and an amount field capped at it.

    Both narrowing inputs are always emitted, even for an anonymous viewer — the page's
    own approve/deny actions are what require a session (T-043), not viewing the queue.
    """
    async with await _client(migrated_engine) as client:
        opened = await client.post(
            "/v1/escalations",
            json=_open_body(
                agent_id="agt-narrow",
                requested_scopes=["invoice:read", "payment:initiate"],
                requested_amount="75000",
            ),
        )
        escalation_id = opened.json()["id"]
        page = await client.get("/escalations")
        assert page.status_code == 200
        assert f'id="scopes-{escalation_id}"' in page.text
        assert 'type="checkbox" value="invoice:read"' in page.text
        assert 'type="checkbox" value="payment:initiate"' in page.text
        assert f'id="amount-{escalation_id}"' in page.text
        assert 'value="75000.0000"' in page.text
        assert 'max="75000.0000"' in page.text


async def test_the_console_queue_page_without_a_database_reports_the_gap() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        page = await client.get("/escalations")
        assert page.status_code == 200
        assert "no database configured" in page.text
