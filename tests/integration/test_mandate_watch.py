"""A mandate announced on Redis is noted by a PEP — APICTA-02 Part B, ADR-081.

Real Redis (testcontainers), the control plane's real publisher, the PEP's real subscriber.
The ledger is the unit suite's fake: what is under test is that the announcement reaches the
registry, not the ledger the registry then calls.

The announcement must *not* lease. It used to, and three PEPs hearing about one mandate
emptied it before any request arrived (ADR-081); the starvation itself is pinned by
`tests/unit/test_pep_mandate_watch.py`. Here the observable effect is a cached refusal being
forgotten, which is also the proof the message reached the registry rather than only the
watch.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from agentiam_controlplane.db.mandates import CHANNEL, RedisMandatePublisher
from agentiam_pep.mandate_watch import MandateWatch
from agentiam_pep.pool import LeasePoolRegistry, PoolSettings, PoolStatus
from tests.unit.test_pep_pool import MandateLedger

pytestmark = pytest.mark.integration


async def test_an_announced_mandate_is_noted_but_not_leased(redis_url: str) -> None:
    ledger = MandateLedger()
    now = datetime.now(UTC)
    registry = LeasePoolRegistry(
        ledger,
        PoolSettings(pep_id="pep-watch", lease_size=Decimal(100)),
        now=lambda: now,
    )
    subscriber = Redis.from_url(redis_url)
    publisher_client = Redis.from_url(redis_url)
    watch = MandateWatch(registry=registry, redis_client=subscriber)
    await watch.start()
    try:
        mandate_id = uuid.uuid4()
        # As if this PEP had asked before the budget row existed.
        registry._refused_until[mandate_id] = (PoolStatus.UNKNOWN, now + timedelta(minutes=5))
        assert registry.lookup(mandate_id) is PoolStatus.UNKNOWN

        message = json.dumps({"event": "mandate:created", "mandate_id": str(mandate_id)})
        # The subscription is asynchronous; publish until a subscriber has heard it.
        for _ in range(50):
            await RedisMandatePublisher(publisher_client).publish(CHANNEL, message)
            await asyncio.sleep(0.05)
            if watch.received:
                break
        assert watch.received, "the announcement never reached the PEP"

        # Reached the registry: the refusal is forgotten, so the next request asks again.
        # Read directly rather than through `lookup()`, which would schedule the creation
        # this test is about not having happened yet.
        assert mandate_id not in registry._refused_until
        # ...and hearing about it took nothing from the mandate.
        assert ledger.asked == [], "the announcement leased budget before any request"
        assert registry.get(mandate_id) is None

        # The lease waits for a token, which is the signal (ADR-078).
        assert await registry.ensure(mandate_id) is not None
        assert ledger.asked == [mandate_id]
    finally:
        await watch.aclose()
        await registry.aclose()
        await subscriber.aclose()
        await publisher_client.aclose()
