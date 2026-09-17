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
_OPERATOR_TOKEN = "operator-secret-for-the-manager"  # noqa: S105 — throwaway test credential
_SETTINGS = ControlPlaneSettings(
    root_private_key=_KEY_PAIR.private_key,
    approvers=frozenset({"kc:manager", "kc:cfo"}),
    session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
    operator_tokens={_OPERATOR_TOKEN: "kc:manager"},
)
#: Authenticates as `kc:manager`. Every revoking test sends this, because since ADR-046 was
#: extended to this route the identity comes from the credential and never from the body.
_AUTH = {"Authorization": f"Bearer {_OPERATOR_TOKEN}"}


def _revoke_body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "block_id": "a" * 128,
        "scope": "token",
        "reason": "stolen",
        "expires_at": (_NOW + timedelta(hours=1)).isoformat(),
    }
    return base | over


async def _client(
    migrated_engine: AsyncEngine, *, settings: ControlPlaneSettings = _SETTINGS
) -> httpx.AsyncClient:
    factory = make_session_factory(migrated_engine)
    app = create_app(session_factory=factory, escalation_settings=settings, now=lambda: _NOW)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")


async def test_revoke_then_pull(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        revoked = await client.post("/v1/revocations", json=_revoke_body(), headers=_AUTH)
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
        revoked = await client.post("/v1/revocations", json=_revoke_body(), headers=_AUTH)
        next_seq = revoked.json()["seq"]
        pulled = await client.get("/v1/revocations", params={"since": next_seq})
        assert pulled.json() == {"entries": [], "next_seq": next_seq}


async def test_revoking_twice_is_idempotent_over_http(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        first = await client.post("/v1/revocations", json=_revoke_body(), headers=_AUTH)
        second = await client.post(
            "/v1/revocations", json=_revoke_body(reason="different"), headers=_AUTH
        )
        assert first.json()["seq"] == second.json()["seq"]
        assert second.json()["reason"] == "stolen"  # first call's row wins


async def test_an_unauthenticated_caller_is_401(migrated_engine: AsyncEngine) -> None:
    """The bug this route shipped with: a `POST` with no credential at all wrote a row.

    Measured against the running demo stack before the fix — 201, and the whole agent tree
    one request away from `ANCESTOR_REVOKED`.
    """
    async with await _client(migrated_engine) as client:
        result = await client.post("/v1/revocations", json=_revoke_body())
        assert result.status_code == 401


async def test_a_body_revoked_by_field_cannot_authorize(migrated_engine: AsyncEngine) -> None:
    """ADR-046 for this route: naming yourself in the body is a claim, not evidence."""
    async with await _client(migrated_engine) as client:
        result = await client.post("/v1/revocations", json=_revoke_body(revoked_by="kc:manager"))
        assert result.status_code == 401


async def test_the_stored_revoker_comes_from_the_credential(migrated_engine: AsyncEngine) -> None:
    """Authenticated as `kc:manager` while the body claims `kc:cfo` — the token wins.

    `kc:cfo` is *also* an approver, so this fails only if the body is being read: a test
    naming an unauthorized id would pass just as well by being rejected.
    """
    async with await _client(migrated_engine) as client:
        result = await client.post(
            "/v1/revocations", json=_revoke_body(revoked_by="kc:cfo"), headers=_AUTH
        )
        assert result.status_code == 201
        assert result.json()["revoked_by"] == "kc:manager"


async def test_an_unknown_operator_token_is_401(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        result = await client.post(
            "/v1/revocations",
            json=_revoke_body(),
            headers={"Authorization": "Bearer not-the-operator-token"},
        )
        assert result.status_code == 401


async def test_a_bare_token_without_the_bearer_scheme_is_401(
    migrated_engine: AsyncEngine,
) -> None:
    """Matches the PEP, which refuses a schemeless `Authorization` for the same reason."""
    async with await _client(migrated_engine) as client:
        result = await client.post(
            "/v1/revocations", json=_revoke_body(), headers={"Authorization": _OPERATOR_TOKEN}
        )
        assert result.status_code == 401


async def test_an_authenticated_non_approver_is_403(migrated_engine: AsyncEngine) -> None:
    """Authenticating is not being allowed to revoke.

    `from_env` refuses to bind a token to a non-approver, so this builds the settings
    directly to reach the route's own check rather than the loader's.
    """
    settings = ControlPlaneSettings(
        root_private_key=_KEY_PAIR.private_key,
        approvers=frozenset({"kc:manager", "kc:cfo"}),
        session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
        operator_tokens={_OPERATOR_TOKEN: "kc:intruder"},
    )
    async with await _client(migrated_engine, settings=settings) as client:
        result = await client.post("/v1/revocations", json=_revoke_body(), headers=_AUTH)
        assert result.status_code == 403


async def test_an_invalid_scope_is_400(migrated_engine: AsyncEngine) -> None:
    async with await _client(migrated_engine) as client:
        result = await client.post(
            "/v1/revocations", json=_revoke_body(scope="bogus"), headers=_AUTH
        )
        assert result.status_code == 400


async def test_revoking_a_nonexistent_block_id_still_succeeds(
    migrated_engine: AsyncEngine,
) -> None:
    """EC-R04: the service cannot know a block id was never minted, and doesn't need to."""
    async with await _client(migrated_engine) as client:
        result = await client.post(
            "/v1/revocations",
            json=_revoke_body(block_id="0" * 128, reason="never existed"),
            headers=_AUTH,
        )
        assert result.status_code == 201


async def test_without_a_database_the_router_is_absent() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        result = await client.get("/v1/revocations")
        assert result.status_code == 404
