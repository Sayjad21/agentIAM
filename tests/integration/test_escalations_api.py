"""Integration tests for `/v1/escalations` and its console page — T-037.

Real Postgres via testcontainers, and a real in-process HTTP round trip via
`httpx.ASGITransport` — the router, the persistence layer and `mint_root` all have to agree
for `POST .../approve` to hand back a token that actually verifies.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
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
)


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


async def _client(migrated_engine: AsyncEngine) -> httpx.AsyncClient:
    factory = make_session_factory(migrated_engine)
    app = create_app(session_factory=factory, escalation_settings=_SETTINGS, now=lambda: _NOW)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")


async def test_open_list_and_approve_mints_a_verifiable_token(
    migrated_engine: AsyncEngine,
) -> None:
    async with await _client(migrated_engine) as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        assert opened.status_code == 201
        escalation_id = opened.json()["id"]

        listed = await client.get("/v1/escalations", params={"state": "pending"})
        assert listed.status_code == 200
        assert [e["id"] for e in listed.json()] == [escalation_id]

        approved = await client.post(
            f"/v1/escalations/{escalation_id}/approve",
            json={"approver": "kc:manager", "elevation_ttl_s": 300},
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


async def test_approving_twice_is_409(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]
        first = await client.post(
            f"/v1/escalations/{escalation_id}/approve", json={"approver": "kc:manager"}
        )
        assert first.status_code == 200
        second = await client.post(
            f"/v1/escalations/{escalation_id}/approve", json={"approver": "kc:cfo"}
        )
        assert second.status_code == 409


async def test_an_unauthorized_approver_is_403(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]
        result = await client.post(
            f"/v1/escalations/{escalation_id}/approve", json={"approver": "kc:intruder"}
        )
        assert result.status_code == 403


async def test_approving_an_unknown_id_is_404(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        result = await client.post(
            f"/v1/escalations/{uuid.uuid4()}/approve", json={"approver": "kc:manager"}
        )
        assert result.status_code == 404


async def test_narrowing_beyond_the_request_is_400(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        opened = await client.post(
            "/v1/escalations", json=_open_body(requested_scopes=["invoice:read"])
        )
        escalation_id = opened.json()["id"]
        result = await client.post(
            f"/v1/escalations/{escalation_id}/approve",
            json={
                "approver": "kc:manager",
                "narrowed_scopes": ["invoice:read", "payment:initiate"],
            },
        )
        assert result.status_code == 400


async def test_deny_records_the_reason(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        opened = await client.post("/v1/escalations", json=_open_body())
        escalation_id = opened.json()["id"]
        result = await client.post(
            f"/v1/escalations/{escalation_id}/deny",
            json={"approver": "kc:cfo", "reason": "vendor not on the approved list"},
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


async def test_the_console_queue_page_without_a_database_reports_the_gap() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        page = await client.get("/escalations")
        assert page.status_code == 200
        assert "no database configured" in page.text
