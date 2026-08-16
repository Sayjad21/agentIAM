"""Integration tests for `RedisRevocationSet` — T-038, spec 07 §5, EC-R06.

Real Postgres (the control plane), a real in-process control-plane app reached over
`httpx.ASGITransport`, and a real Redis testcontainer. EC-R06 ("a PEP misses the pub/sub
message... converges on the next pull") is proven here by pointing the consumer's *push*
connection at a Redis port nothing is listening on — its subscribe loop fails and retries
forever, exactly as if every message were dropped — while its *pull* path talks to the real
control plane. If pull alone still converges, spec 07 §5.2's claim ("push is an optimization;
pull is the source of truth") is load-bearing rather than asserted.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.revocation_publisher import RedisRevocationPublisher
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.decision import OracleUnavailable
from agentiam_core.tokens import generate_keypair
from agentiam_pep.revocation import RedisRevocationSet

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
_KEY_PAIR = generate_keypair()
_SETTINGS = ControlPlaneSettings(
    root_private_key=_KEY_PAIR.private_key,
    approvers=frozenset({"kc:manager"}),
    session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
)


async def _revoke(client: httpx.AsyncClient, block_id: str) -> None:
    response = await client.post(
        "/v1/revocations",
        json={
            "block_id": block_id,
            "scope": "token",
            "reason": "test",
            "revoked_by": "kc:manager",
            "expires_at": (_NOW + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201


async def test_pull_alone_converges_when_the_push_connection_is_entirely_broken(
    migrated_engine: AsyncEngine, redis_url: str
) -> None:
    factory = make_session_factory(migrated_engine)
    real_redis = redis_asyncio.Redis.from_url(redis_url)
    app = create_app(
        session_factory=factory,
        escalation_settings=_SETTINGS,
        revocation_publisher=RedisRevocationPublisher(real_redis),
        now=lambda: _NOW,
    )
    control_plane_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    )

    # A Redis client pointed at a port nothing listens on: every subscribe attempt fails,
    # forever — the push path is not merely slow, it never works for this consumer.
    broken_redis = redis_asyncio.Redis(
        host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2
    )

    oracle = RedisRevocationSet(
        redis_client=broken_redis,
        control_plane_client=control_plane_client,
        pull_interval_s=0.2,
        staleness_limit_s=30.0,
        resubscribe_delay_s=0.1,
        now=lambda: datetime.now(UTC),
    )

    try:
        await oracle.start()
        assert oracle.is_revoked("z" * 128) is False  # nothing revoked yet

        await _revoke(control_plane_client, "z" * 128)

        # No push will ever arrive (the subscribe connection cannot succeed); only a pull
        # tick can pick this up. Give it more than one `pull_interval_s`.
        for _ in range(20):
            await asyncio.sleep(0.2)
            if oracle.is_revoked("z" * 128):
                break

        assert oracle.is_revoked("z" * 128) is True
    finally:
        await oracle.aclose()
        await control_plane_client.aclose()
        await broken_redis.aclose()
        await real_redis.aclose()


async def test_a_working_push_delivers_faster_than_the_pull_interval(
    migrated_engine: AsyncEngine, redis_url: str
) -> None:
    """The happy path, for contrast.

    With a working Redis, the push arrives almost immediately rather than waiting for the
    next pull tick.
    """
    factory = make_session_factory(migrated_engine)
    publish_redis = redis_asyncio.Redis.from_url(redis_url)
    subscribe_redis = redis_asyncio.Redis.from_url(redis_url)
    app = create_app(
        session_factory=factory,
        escalation_settings=_SETTINGS,
        revocation_publisher=RedisRevocationPublisher(publish_redis),
        now=lambda: _NOW,
    )
    control_plane_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    )

    oracle = RedisRevocationSet(
        redis_client=subscribe_redis,
        control_plane_client=control_plane_client,
        pull_interval_s=30.0,  # deliberately long: only push can win this race
        staleness_limit_s=60.0,
        now=lambda: datetime.now(UTC),
    )

    try:
        await oracle.start()
        # Give the subscribe loop a moment to finish `SUBSCRIBE` before publishing —
        # a message published before anyone is listening is exactly the EC-R06 case above.
        await asyncio.sleep(0.3)

        await _revoke(control_plane_client, "y" * 128)

        for _ in range(20):
            await asyncio.sleep(0.1)
            if oracle.is_revoked("y" * 128):
                break

        assert oracle.is_revoked("y" * 128) is True
    finally:
        await oracle.aclose()
        await control_plane_client.aclose()
        await subscribe_redis.aclose()
        await publish_redis.aclose()


async def _always_fails(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("control plane unreachable", request=request)


async def test_the_oracle_fails_closed_when_the_control_plane_never_answers(
    redis_url: str,
) -> None:
    """`start()` does not raise; `is_revoked()` must.

    `_pull_once` swallows its own failure per §5.2/§5.3's split between "slow" and "wrong",
    but `is_revoked()` must raise since no pull has ever succeeded — `decide()` turns that
    into `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` (rule 6).
    """
    redis_client = redis_asyncio.Redis.from_url(redis_url)
    dead_control_plane = httpx.AsyncClient(
        transport=httpx.MockTransport(_always_fails), base_url="http://cp"
    )

    oracle = RedisRevocationSet(
        redis_client=redis_client,
        control_plane_client=dead_control_plane,
        pull_interval_s=0.1,
        staleness_limit_s=0.3,
        resubscribe_delay_s=0.1,
        now=lambda: datetime.now(UTC),
    )

    try:
        await oracle.start()  # does not raise
        with pytest.raises(OracleUnavailable, match="no revocation pull"):
            oracle.is_revoked("z" * 128)
    finally:
        await oracle.aclose()
        await redis_client.aclose()
        await dead_control_plane.aclose()
