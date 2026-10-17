"""One PEP, many mandates, against a real ledger — APICTA-02.

The pipeline is the unit suite's (`tests/unit/test_pep_pipeline.py`); the ledger client is the
deployed one (`scripts/pep_service.SessionLedger`), the settlement path is the deployed one
(`LedgerSettlementSink`), and the budgets are real rows. What is asserted is where the money
lands: each mandate's spend on its own row, nothing on a row whose token did not pay, and the
pool invariant holding on every row afterwards.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.invariants import check_invariants
from agentiam_controlplane.db.models import BudgetRow
from agentiam_controlplane.db.settlement_sink import LedgerSettlementSink
from agentiam_core.errors import ReasonCode
from agentiam_core.models import BudgetDimension
from agentiam_pep.pipeline import Authorized, Refused
from agentiam_pep.pool import LeasePool, LeasePoolRegistry, PoolSettings
from agentiam_pep.settlement import SettlementQueue, SettlementSettings
from scripts.pep_service import SessionLedger
from tests.integration.conftest import make_budget
from tests.unit.test_pep_pipeline import NOW, a_mandate, a_pipeline, bearer

pytestmark = pytest.mark.integration

TOTAL = Decimal("1000.0000")
LEASE = Decimal("500.0000")


def _pay(amount: str) -> bytes:
    return f'{{"amount": "{amount}", "recipient": {{"account_id": "acct_1"}}}}'.encode()


async def _row(engine: AsyncEngine, mandate_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """`(committed, leased)` on the mandate's spend pool."""
    async with make_session_factory(engine)() as session:
        row = (
            await session.execute(
                select(BudgetRow).where(
                    BudgetRow.mandate_id == mandate_id,
                    BudgetRow.dimension == "spend_bdt",
                    BudgetRow.agent_id.is_(None),
                )
            )
        ).scalar_one()
        return row.committed, row.leased


def _settlement(engine: AsyncEngine) -> SettlementQueue:
    return SettlementQueue(
        LedgerSettlementSink(make_session_factory(engine)),
        SettlementSettings(flush_interval_s=0.01),
        now=lambda: NOW,
    )


def _registry(engine: AsyncEngine, settlement: SettlementQueue) -> LeasePoolRegistry:
    return LeasePoolRegistry(
        SessionLedger(make_session_factory(engine)),
        PoolSettings(pep_id="pep-multi", lease_size=LEASE),
        now=lambda: NOW,
        before_release=settlement.drain,
    )


async def test_two_mandates_spend_through_one_pep_each_on_its_own_row(
    migrated_engine: AsyncEngine,
) -> None:
    mandate_a, mandate_b = uuid.uuid4(), uuid.uuid4()
    await make_budget(migrated_engine, mandate_id=mandate_a, total=TOTAL)
    await make_budget(migrated_engine, mandate_id=mandate_b, total=TOTAL)
    settlement = _settlement(migrated_engine)
    registry = _registry(migrated_engine, settlement)
    pipeline, _, _ = await a_pipeline(pools=registry, settlement=settlement)

    # The first request for each mandate creates its pool off the request path.
    for mandate_id in (mandate_a, mandate_b):
        first = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate(mandate_id=mandate_id)),
            body=_pay("1.0000"),
        )
        assert isinstance(first, Refused)
        assert first.reason_code is ReasonCode.LEASE_UNAVAILABLE
    await registry.drain()

    paid_a = await pipeline.authorize(
        method="POST",
        path="/payments",
        headers=bearer(a_mandate(mandate_id=mandate_a)),
        body=_pay("250.0000"),
    )
    paid_b = await pipeline.authorize(
        method="POST",
        path="/payments",
        headers=bearer(a_mandate(mandate_id=mandate_b)),
        body=_pay("40.0000"),
    )
    assert isinstance(paid_a, Authorized) and isinstance(paid_b, Authorized)
    pipeline.settle(paid_a)
    pipeline.settle(paid_b)
    await settlement.flush()
    await registry.aclose()

    assert await _row(migrated_engine, mandate_a) == (Decimal("250.0000"), Decimal(0))
    assert await _row(migrated_engine, mandate_b) == (Decimal("40.0000"), Decimal(0))
    report = await check_invariants(migrated_engine)
    assert report.holds, f"{report.summary()}: {[str(v) for v in report.violations]}"


async def test_a_token_under_another_mandate_does_not_spend_this_pep_s_pool(
    migrated_engine: AsyncEngine,
) -> None:
    """The ticket's step 1, in the pre-APICTA-02 shape: one PEP bound to one mandate.

    A token minted under mandate B, through a PEP whose only pool is A's, used to spend A's
    money: the pipeline never compared them. It must be refused, and A's row must not move.
    """
    mandate_a, mandate_b = uuid.uuid4(), uuid.uuid4()
    await make_budget(migrated_engine, mandate_id=mandate_a, total=TOTAL)
    await make_budget(migrated_engine, mandate_id=mandate_b, total=TOTAL)
    settlement = _settlement(migrated_engine)
    pool_a = LeasePool(
        SessionLedger(make_session_factory(migrated_engine)),
        PoolSettings(pep_id="pep-a", lease_size=LEASE),
        mandate_id=mandate_a,
        now=lambda: NOW,
        before_release=settlement.drain,
    )
    pipeline, _, _ = await a_pipeline(pools=LeasePoolRegistry.fixed(pool_a), settlement=settlement)
    assert await pool_a.prime(BudgetDimension.SPEND_BDT)

    refused = await pipeline.authorize(
        method="POST",
        path="/payments",
        headers=bearer(a_mandate(mandate_id=mandate_b)),
        body=_pay("250.0000"),
    )
    assert isinstance(refused, Refused)
    assert refused.reason_code is ReasonCode.MANDATE_UNKNOWN

    await settlement.flush()
    await pool_a.aclose()
    assert await _row(migrated_engine, mandate_a) == (Decimal(0), Decimal(0))
    assert await _row(migrated_engine, mandate_b) == (Decimal(0), Decimal(0))


async def test_a_mandate_with_no_budget_row_is_refused_as_unknown(
    migrated_engine: AsyncEngine,
) -> None:
    """The deployed ledger client's `NoResultFound` reaches the pipeline as MANDATE_UNKNOWN.

    Before APICTA-02 it was an outage to the pool (`acquire_failures`), indistinguishable from
    a dead database, and would have been retried on every request.
    """
    settlement = _settlement(migrated_engine)
    registry = _registry(migrated_engine, settlement)
    pipeline, _, _ = await a_pipeline(pools=registry, settlement=settlement)
    unfunded = a_mandate(mandate_id=uuid.uuid4())

    first = await pipeline.authorize(
        method="POST", path="/payments", headers=bearer(unfunded), body=_pay("1.0000")
    )
    assert isinstance(first, Refused)
    assert first.reason_code is ReasonCode.LEASE_UNAVAILABLE
    await registry.drain()

    second = await pipeline.authorize(
        method="POST", path="/payments", headers=bearer(unfunded), body=_pay("1.0000")
    )
    assert isinstance(second, Refused)
    assert second.reason_code is ReasonCode.MANDATE_UNKNOWN
    assert second.status == 403
    await registry.aclose()
