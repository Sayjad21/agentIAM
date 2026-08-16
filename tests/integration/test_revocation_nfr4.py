"""NFR-4 measurement — T-039, `PLAN.md` line 1157: 3 PEP instances all deny within 2 s p99.

Three independent `RedisRevocationSet` oracles, standing in for 3 PEP processes, each with
its own Redis subscribe connection and its own `httpx.AsyncClient` against one in-process
control-plane app backed by a real Postgres and a real Redis testcontainer — the same
infrastructure `test_revocation_consumer.py` (EC-R06/EC-R07) already proves correctness
against. This module measures *latency*, not correctness: how long from a successful
`REVOKE` until each oracle's `is_revoked()` first reports `True`.

`RedisRevocationSet.is_revoked()` now checks the T-039 Bloom filter before falling through to
the exact set (ADR-044) — this test is what confirms that extra check does not cost the
latency budget it exists to protect.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.revocation_publisher import RedisRevocationPublisher
from agentiam_controlplane.settings import ControlPlaneSettings
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

#: 20 revokes x 3 oracles = 60 real samples. `PLAN.md`'s own measurement method for NFR-4
#: is literally "integration test with 3 PEP instances" — this is that test.
_ITERATIONS = 20
_NFR4_LIMIT_S = 2.0


async def _revoke(client: httpx.AsyncClient, block_id: str) -> None:
    response = await client.post(
        "/v1/revocations",
        json={
            "block_id": block_id,
            "scope": "token",
            "reason": "T-039 NFR-4 measurement",
            "revoked_by": "kc:manager",
            "expires_at": (_NOW + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201


async def _time_to_deny(oracle: RedisRevocationSet, block_id: str, deadline_s: float) -> float:
    """Seconds from now until `oracle.is_revoked(block_id)` first returns `True`.

    Polls rather than blocks on a single event, since the id may arrive via either the
    push subscription or the next pull tick — the same ambiguity `decide()` itself never
    has to resolve (spec 07 §5.2's asymmetry: pull is the source of truth regardless of
    which path wins the race).
    """
    start = time.perf_counter()
    while True:
        if oracle.is_revoked(block_id):
            return time.perf_counter() - start
        elapsed = time.perf_counter() - start
        if elapsed >= deadline_s:
            raise AssertionError(f"{block_id} not denied within {deadline_s}s")
        await asyncio.sleep(0.005)


async def test_nfr4_three_pep_instances_all_deny_within_2s_p99(
    migrated_engine: AsyncEngine, redis_url: str
) -> None:
    """`PLAN.md` T-039 accept criterion, verbatim: 3 PEP instances all deny within 2 s p99."""
    factory = make_session_factory(migrated_engine)
    publish_redis = redis_asyncio.Redis.from_url(redis_url)
    app = create_app(
        session_factory=factory,
        escalation_settings=_SETTINGS,
        revocation_publisher=RedisRevocationPublisher(publish_redis),
        now=lambda: _NOW,
    )
    revoke_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")

    oracles: list[RedisRevocationSet] = []
    subscribe_clients: list[redis_asyncio.Redis] = []
    oracle_http_clients: list[httpx.AsyncClient] = []
    for _ in range(3):
        subscribe_redis = redis_asyncio.Redis.from_url(redis_url)
        oracle_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cp"
        )
        oracles.append(
            RedisRevocationSet(
                redis_client=subscribe_redis,
                control_plane_client=oracle_http,
                pull_interval_s=1.0,
                staleness_limit_s=30.0,
                now=lambda: datetime.now(UTC),
            )
        )
        subscribe_clients.append(subscribe_redis)
        oracle_http_clients.append(oracle_http)

    try:
        await asyncio.gather(*(o.start() for o in oracles))
        # Let every subscribe loop finish SUBSCRIBE before the first revoke — a message
        # published before anyone is listening is EC-R06, proven separately in
        # `test_revocation_consumer.py`, not what this test measures.
        await asyncio.sleep(0.3)

        samples: list[float] = []
        for i in range(_ITERATIONS):
            block_id = f"nfr4-{i:04d}".rjust(128, "0")
            await _revoke(revoke_client, block_id)
            waits = await asyncio.gather(
                *(_time_to_deny(o, block_id, deadline_s=_NFR4_LIMIT_S * 2) for o in oracles)
            )
            samples.extend(waits)

        samples.sort()
        p99_index = max(0, int(len(samples) * 0.99) - 1)
        p99 = samples[p99_index]
        print(
            f"\nNFR-4 (T-039): {len(samples)} samples across 3 oracles — "
            f"min={samples[0] * 1e6:.0f}us max={samples[-1] * 1e6:.0f}us "
            f"p99={p99 * 1e6:.0f}us (limit {_NFR4_LIMIT_S * 1000:.0f}ms)"
        )
        assert p99 < _NFR4_LIMIT_S, f"p99 {p99:.3f}s exceeds NFR-4's {_NFR4_LIMIT_S}s"
    finally:
        await asyncio.gather(*(o.aclose() for o in oracles))
        await asyncio.gather(*(c.aclose() for c in subscribe_clients))
        await asyncio.gather(*(c.aclose() for c in oracle_http_clients))
        await revoke_client.aclose()
        await publish_redis.aclose()
