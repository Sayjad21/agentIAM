"""P-10 and P-20 (spec 04 §16), against real Postgres via testcontainers.

**Scope, and why it's partial.** Spec 04 §16 says P-10's rule machine "MUST include:
acquire, reserve, commit, refund, release, expire, crash, revoke — and late commit." Of
those, `reserve`/`commit`/`refund` and `late commit` need `LEDGER_COMMIT` (T-014); `revoke`
has no owning ticket yet. `crash` needs no rule of its own — it's just "never call release
before expire," which any random interleaving of this machine's rules already produces.

So this machine has exactly the three rules T-013 owns: `acquire`, `release`, `expire`
(REAP). **T-014 must extend this same machine with reserve/commit/refund/late-commit
rules, not write a second one** — the point of a stateful property test is one model
accumulating coverage, not two partial ones that each look complete.

The property under test is the ledger invariant from spec 04 §2.2:
`leased == Σ over active leases (granted - settled)`. `settled` stays 0 throughout (no
`LEDGER_COMMIT` yet), so this reduces to `leased == Σ outstanding of currently-active
leases` — but that reduction is exactly what future rules must not get to assume silently;
the assertion is written against the general form on purpose.

`committed + leased <= total` (P-10) and `leased >= 0` (P-20) are already proven at the
single-transaction level by the database `CHECK` (`test_lease_schema.py`); what this test
adds is that the *sequence* of operations never lets the ledger's derived accounting drift
from what `test_ledger.py`'s worked examples show for one or two operations at a time.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import acquire, reap, release
from agentiam_controlplane.db.models import BudgetRow, LeaseRow
from agentiam_controlplane.errors import LeaseUnavailableError

pytestmark = pytest.mark.integration

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_TOTAL = Decimal("20")
_TTL = timedelta(seconds=60)


@dataclass(frozen=True)
class Acquire:
    amount: Decimal


@dataclass(frozen=True)
class Release:
    index: int


@dataclass(frozen=True)
class Expire:
    advance_seconds: int


_operations = st.lists(
    st.one_of(
        st.builds(Acquire, amount=st.sampled_from([Decimal("1"), Decimal("2"), Decimal("5")])),
        st.builds(Release, index=st.integers(min_value=0, max_value=9)),
        st.builds(Expire, advance_seconds=st.sampled_from([1, 10, 70])),
    ),
    min_size=1,
    max_size=15,
)


async def _run_sequence(engine: AsyncEngine, ops: list[Acquire | Release | Expire]) -> None:
    factory = make_session_factory(engine)
    mandate_id = uuid.uuid4()
    dimension = "spend_bdt"

    async with factory() as s, s.begin():
        budget = BudgetRow(mandate_id=mandate_id, dimension=dimension, total=_TOTAL)
        s.add(budget)
        await s.flush()
        budget_id = budget.id

    now = _EPOCH
    active_lease_ids: list[uuid.UUID] = []

    for op in ops:
        if isinstance(op, Acquire):
            async with factory() as s:
                try:
                    lease = await acquire(
                        s,
                        mandate_id=mandate_id,
                        dimension=dimension,
                        requested=op.amount,
                        pep_id="pep",
                        ttl=_TTL,
                        now=now,
                    )
                    active_lease_ids.append(lease.id)
                except LeaseUnavailableError:
                    pass
        elif isinstance(op, Release):
            if active_lease_ids:
                lease_id = active_lease_ids.pop(op.index % len(active_lease_ids))
                async with factory() as s:
                    await release(s, lease_id=lease_id)
        else:
            now += timedelta(seconds=op.advance_seconds)
            async with factory() as s:
                reclaimed = set(await reap(s, now=now))
            active_lease_ids = [lid for lid in active_lease_ids if lid not in reclaimed]

        async with factory() as s:
            result = await s.execute(select(BudgetRow).where(BudgetRow.id == budget_id))
            current_budget = result.scalar_one()

            outstanding_sum = Decimal("0")
            for lease_id in active_lease_ids:
                lease_result = await s.execute(select(LeaseRow).where(LeaseRow.id == lease_id))
                outstanding_sum += lease_result.scalar_one().outstanding

        # spec 04 §2.2's ledger invariant, after every single operation.
        assert current_budget.leased == outstanding_sum, (
            f"leased={current_budget.leased} but active leases sum to {outstanding_sum} "
            f"after {op!r}"
        )
        # P-10 and P-20 — redundant with the DB CHECK per row, asserted again here because
        # the point is that no *sequence* of operations can violate it either.
        assert current_budget.leased >= 0
        assert current_budget.committed + current_budget.leased <= current_budget.total


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(ops=_operations)
def test_ledger_invariant_holds_under_acquire_release_expire(
    postgres_url: str,
    migrated_engine: AsyncEngine,  # triggers `alembic upgrade head` once; not used directly
    ops: list[Acquire | Release | Expire],
) -> None:
    """P-10 / P-20, restricted to this ticket's rules — see module docstring."""
    engine = make_engine(postgres_url, poolclass=NullPool)
    try:
        asyncio.run(_run_sequence(engine, ops))
    finally:
        asyncio.run(engine.dispose())
