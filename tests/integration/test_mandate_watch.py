"""A mandate announced on Redis is pooled by a PEP before its first token — APICTA-02 Part B.

Real Redis (testcontainers), the control plane's real publisher, the PEP's real subscriber.
The ledger is the unit suite's fake: what is under test is that the announcement reaches the
registry, not the ledger the registry then calls.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from agentiam_controlplane.db.mandates import CHANNEL, RedisMandatePublisher
from agentiam_pep.mandate_watch import MandateWatch
from agentiam_pep.pool import LeasePool, LeasePoolRegistry, PoolSettings
from tests.unit.test_pep_pool import MandateLedger

pytestmark = pytest.mark.integration


async def test_an_announced_mandate_is_pooled_without_a_request(redis_url: str) -> None:
    ledger = MandateLedger()
    registry = LeasePoolRegistry(
        ledger,
        PoolSettings(pep_id="pep-watch", lease_size=Decimal(100)),
        now=lambda: datetime.now(UTC),
    )
    subscriber = Redis.from_url(redis_url)
    publisher_client = Redis.from_url(redis_url)
    watch = MandateWatch(registry=registry, redis_client=subscriber)
    await watch.start()
    try:
        mandate_id = uuid.uuid4()
        message = json.dumps({"event": "mandate:created", "mandate_id": str(mandate_id)})
        # The subscription is asynchronous; publish until a subscriber has heard it.
        for _ in range(50):
            await RedisMandatePublisher(publisher_client).publish(CHANNEL, message)
            await asyncio.sleep(0.05)
            if watch.received:
                break
        await registry.drain()
        assert isinstance(registry.lookup(mandate_id), LeasePool)
        assert ledger.asked[0] == mandate_id
    finally:
        await watch.aclose()
        await registry.aclose()
        await subscriber.aclose()
        await publisher_client.aclose()
