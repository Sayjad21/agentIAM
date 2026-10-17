"""`MandateWatch` — a PEP noting a mandate it was told about (APICTA-02 Part B).

Only the message handling is tested here; the subscription loop is `PolicySync`'s, and the
round trip through a real Redis is `tests/integration/test_mandate_watch.py`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentiam_core.models import BudgetDimension
from agentiam_pep.mandate_watch import MandateWatch
from agentiam_pep.pool import LeasePoolRegistry, PoolSettings, PoolStatus

from .test_pep_pool import FakeLedger


class FakeRegistry:
    def __init__(self) -> None:
        """Record which mandates were noted."""
        self.noted: list[uuid.UUID] = []

    def note_created(self, mandate_id: uuid.UUID) -> None:
        self.noted.append(mandate_id)


MANDATE = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a5")
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
SPEND = BudgetDimension.SPEND_BDT


async def test_a_created_event_is_noted() -> None:
    registry = FakeRegistry()
    watch = MandateWatch(registry=registry, redis_client=None)
    raw = json.dumps({"event": "mandate:created", "mandate_id": str(MANDATE)})
    assert await watch.handle_push(raw)
    assert registry.noted == [MANDATE]
    assert watch.received == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        json.dumps(["a", "list"]),
        json.dumps({"event": "policy:reload", "mandate_id": str(MANDATE)}),
        json.dumps({"event": "mandate:created"}),
        json.dumps({"event": "mandate:created", "mandate_id": "not-a-uuid"}),
    ],
)
async def test_anything_else_is_ignored(raw: str | bytes) -> None:
    registry = FakeRegistry()
    watch = MandateWatch(registry=registry, redis_client=None)
    assert not await watch.handle_push(raw)
    assert registry.noted == []


async def test_start_and_aclose_without_redis_are_harmless() -> None:
    """A PEP with no Redis still converges: the first token creates the pool (ADR-078)."""
    watch = MandateWatch(registry=FakeRegistry(), redis_client=None)
    await watch.start()
    await watch.aclose()


def test_the_pep_listens_where_the_control_plane_announces() -> None:
    """Two constants in two packages — the PEP does not import the control plane."""
    from agentiam_controlplane.db.mandates import CHANNEL as ANNOUNCED
    from agentiam_pep.mandate_watch import CHANNEL as WATCHED

    assert ANNOUNCED == WATCHED


def _pep(ledger: FakeLedger, pep_id: str) -> LeasePoolRegistry:
    return LeasePoolRegistry(
        ledger,
        PoolSettings(pep_id=pep_id, lease_size=Decimal(1000), ttl=timedelta(seconds=60)),
        now=lambda: NOW,
    )


async def test_an_announcement_takes_no_budget_from_the_mandate() -> None:
    """Hearing about a mandate must not lease from it. The serving PEP was being starved.

    `handle_push` used to call `registry.ensure()`, which primes the pool with a real
    `ACQUIRE`. Every PEP on the channel therefore took `lease_size` from every mandate the
    moment it was announced, whether or not it would ever serve one. On a mandate smaller
    than the PEPs' combined lease size the ledger was empty before the first request:
    measured 1000 / 500 / 0 of 1500 across three PEPs, leaving the one that actually served
    the task refused `BUDGET_EXHAUSTED_MANDATE` - *the human-approved budget is spent* -
    while every taka of it sat unspent on two idle PEPs. Idle PEPs then renewed those
    leases forever, so it did not heal.
    """
    ledger = FakeLedger(available=Decimal(1500))
    peps = [_pep(ledger, f"pep-{i}") for i in (1, 2, 3)]
    raw = json.dumps({"event": "mandate:created", "mandate_id": str(MANDATE)})

    for registry in peps:
        assert await MandateWatch(registry=registry, redis_client=None).handle_push(raw)

    assert ledger.acquired == [], "an announcement leased budget before any request arrived"
    assert ledger.available == Decimal(1500)
    assert all(registry.get(MANDATE) is None for registry in peps)

    # The PEP that actually serves it still gets the lease it needs - the token is the
    # signal (ADR-078), and nothing was taken ahead of it.
    serving = peps[2]
    pool = await serving.ensure(MANDATE)
    assert pool is not None
    assert pool.remaining(SPEND) == Decimal(1000)


async def test_an_announcement_clears_a_cached_refusal() -> None:
    """What the watch is still for: a mandate refused as unknown is retried at once.

    A PEP that asked before the budget row existed caches `UNKNOWN` for `unknown_ttl`
    (30 s). The announcement is the control plane saying it exists now, so the next request
    should ask the ledger again rather than wait out the cache.
    """
    ledger = FakeLedger(available=Decimal(1500))
    registry = _pep(ledger, "pep-1")
    registry._refused_until[MANDATE] = (PoolStatus.UNKNOWN, NOW + timedelta(seconds=30))
    assert registry.lookup(MANDATE) is PoolStatus.UNKNOWN

    raw = json.dumps({"event": "mandate:created", "mandate_id": str(MANDATE)})
    await MandateWatch(registry=registry, redis_client=None).handle_push(raw)

    assert registry.lookup(MANDATE) is PoolStatus.PENDING
    assert await registry.ensure(MANDATE) is not None
