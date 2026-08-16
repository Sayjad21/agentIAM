"""Integration tests for `/v1/revocations` — T-038.

Real Postgres via testcontainers and a real in-process HTTP round trip via
`httpx.ASGITransport`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.tokens import generate_keypair

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
_KEY_PAIR = generate_keypair()
_SETTINGS = ControlPlaneSettings(
    root_private_key=_KEY_PAIR.private_key,
    approvers=frozenset({"kc:manager", "kc:cfo"}),
)


def _revoke_body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "block_id": "a" * 128,
        "scope": "token",
        "reason": "stolen",
        "revoked_by": "kc:manager",
        "expires_at": (_NOW + timedelta(hours=1)).isoformat(),
    }
    return base | over


async def _client(migrated_engine: AsyncEngine) -> httpx.AsyncClient:
    factory = make_session_factory(migrated_engine)
    app = create_app(session_factory=factory, escalation_settings=_SETTINGS, now=lambda: _NOW)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")


async def test_revoke_then_pull(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        revoked = await client.post("/v1/revocations", json=_revoke_body())
        assert revoked.status_code == 201
        body = revoked.json()
        assert body["block_id"] == "a" * 128
        assert body["seq"] >= 1

        pulled = await client.get("/v1/revocations", params={"since": 0})
        assert pulled.status_code == 200
        pulled_body = pulled.json()
        assert [e["block_id"] for e in pulled_body["entries"]] == ["a" * 128]
        assert pulled_body["next_seq"] == body["seq"]


async def test_pulling_with_the_returned_next_seq_yields_nothing_new(
    migrated_engine: AsyncEngine,
) -> None:
    async with await _client(migrated_engine) as client:
        revoked = await client.post("/v1/revocations", json=_revoke_body())
        next_seq = revoked.json()["seq"]
        pulled = await client.get("/v1/revocations", params={"since": next_seq})
        assert pulled.json() == {"entries": [], "next_seq": next_seq}


async def test_revoking_twice_is_idempotent_over_http(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        first = await client.post("/v1/revocations", json=_revoke_body())
        second = await client.post("/v1/revocations", json=_revoke_body(reason="different"))
        assert first.json()["seq"] == second.json()["seq"]
        assert second.json()["reason"] == "stolen"  # first call's row wins


async def test_an_unauthorized_revoker_is_403(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        result = await client.post("/v1/revocations", json=_revoke_body(revoked_by="kc:intruder"))
        assert result.status_code == 403


async def test_an_invalid_scope_is_400(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        result = await client.post("/v1/revocations", json=_revoke_body(scope="bogus"))
        assert result.status_code == 400


async def test_revoking_a_nonexistent_block_id_still_succeeds(
    migrated_engine: AsyncEngine,
) -> None:
    """EC-R04: the service cannot know a block id was never minted, and doesn't need to."""
    async with await _client(migrated_engine) as client:
        result = await client.post(
            "/v1/revocations", json=_revoke_body(block_id="0" * 128, reason="never existed")
        )
        assert result.status_code == 201


async def test_without_a_database_the_router_is_absent() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        result = await client.get("/v1/revocations")
        assert result.status_code == 404
