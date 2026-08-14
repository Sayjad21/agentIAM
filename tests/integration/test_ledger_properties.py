"""P-10, P-12, P-20 (spec 04 §16), against real Postgres via testcontainers.

**Scope, and why it's still partial.** Spec 04 §16 says P-10's rule machine "MUST include:
acquire, reserve, commit, refund, release, expire, crash, revoke — and late commit." `revoke`
has no owning ticket yet. `crash` needs no rule of its own — it's just "never call release
before expire," which any random interleaving already produces. `reserve`/`commit`/`refund`
are PEP-local and pure (spec 04 §4.2, §4.3 — no network, no ledger mutation, no lock); they
never touch this machine's database at all, and are covered exhaustively at exact `Decimal`
precision by `tests/unit/test_pep_lease.py` instead — a hypothesis sequence over in-memory
state would test the same three lines of arithmetic this machine cannot usefully add to.

**T-014 extends this same machine with one rule: `LedgerCommit`** — the one PEP-facing
operation of the seven that *does* touch the database (spec 04 §4.4). It is what turns
`Commit` from spec 04's list into something this machine can actually exercise, and it is
also what makes `late commit` (spec 04 §11) reachable: `LedgerCommit` targets any lease this
run has ever acquired, not only ones still tracked as active, so a commit landing after that
lease's `Release`/`Expire` already ran is a normal outcome of random interleaving, not a
special case requiring its own rule. When it also replays a previously-used
`reservation_id`, it exercises G4/P-12 (ADR-010): the idempotency return value (`False` on
replay) is asserted directly, in addition to the general invariant check every rule shares.

The property under test is the ledger invariant from spec 04 §2.2:
`leased == Σ over active leases (granted - settled)`. Now that `LedgerCommit` moves `settled`
off zero, this is the first time that reduction the old docstring warned about is actually
exercised for real.

`committed + leased <= total` (P-10) and `leased >= 0` (P-20) are already proven at the
single-transaction level by the database `CHECK` (`test_lease_schema.py`, `test_ledger.py`'s
G2/G3 guard-proofs); what this test adds is that the *sequence* of operations never lets the
ledger's derived accounting drift from what worked examples show for one or two operations
at a time.
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
from agentiam_controlplane.db.ledger import acquire, ledger_commit, reap, release
from agentiam_controlplane.db.models import BudgetRow, LeaseRow
from agentiam_controlplane.errors import LeaseNotActiveError, LeaseUnavailableError

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


@dataclass(frozen=True)
class LedgerCommit:
    """`LEDGER_COMMIT` against any lease this run has ever acquired — active or not.

    Targeting the full history rather than only `active_lease_ids` is what makes late
    commits (spec 04 §11) part of ordinary interleaving instead of a scenario that has to be
    engineered by hand. `reuse_reservation`, when `True` and a prior commit has actually
    applied, replays that commit's exact `(lease_id, reservation_id)` pair — P-12/G4.
    """

    lease_index: int
    amount: Decimal
    reuse_reservation: bool


_operations = st.lists(
    st.one_of(
        st.builds(Acquire, amount=st.sampled_from([Decimal("1"), Decimal("2"), Decimal("5")])),
        st.builds(Release, index=st.integers(min_value=0, max_value=9)),
        st.builds(Expire, advance_seconds=st.sampled_from([1, 10, 70])),
        st.builds(
            LedgerCommit,
            lease_index=st.integers(min_value=0, max_value=9),
            amount=st.sampled_from([Decimal("1"), Decimal("2"), Decimal("5")]),
            reuse_reservation=st.booleans(),
        ),
    ),
    min_size=1,
    max_size=15,
)

_Op = Acquire | Release | Expire | LedgerCommit


async def _run_sequence(engine: AsyncEngine, ops: list[_Op]) -> None:
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
    all_lease_ids: list[uuid.UUID] = []  # never pruned — late commits target this
    applied_reservations: list[tuple[uuid.UUID, uuid.UUID]] = []  # (lease_id, reservation_id)

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
                    all_lease_ids.append(lease.id)
                except LeaseUnavailableError:
                    pass
        elif isinstance(op, Release):
            if active_lease_ids:
                lease_id = active_lease_ids.pop(op.index % len(active_lease_ids))
                async with factory() as s:
                    await release(s, lease_id=lease_id)
        elif isinstance(op, Expire):
            now += timedelta(seconds=op.advance_seconds)
            async with factory() as s:
                reclaimed = set(await reap(s, now=now))
            active_lease_ids = [lid for lid in active_lease_ids if lid not in reclaimed]
        else:
            target: tuple[uuid.UUID, uuid.UUID, bool] | None = None
            if op.reuse_reservation and applied_reservations:
                prior_lease_id, prior_reservation_id = applied_reservations[
                    op.lease_index % len(applied_reservations)
                ]
                target = (prior_lease_id, prior_reservation_id, True)
            elif all_lease_ids:
                target = (all_lease_ids[op.lease_index % len(all_lease_ids)], uuid.uuid4(), False)

            if target is not None:
                lease_id, reservation_id, replaying = target
                async with factory() as s:
                    try:
                        applied: bool | None = await ledger_commit(
                            s,
                            lease_id=lease_id,
                            reservation_id=reservation_id,
                            amount=op.amount,
                            now=now,
                        )
                    except LeaseNotActiveError:
                        applied = None  # late commit — rejected and recorded, not a failure
                if replaying:
                    # P-12 / G4 (ADR-010): a replayed reservation_id never re-applies.
                    assert applied is not True, f"replay of {reservation_id} re-applied"
                elif applied is True:
                    applied_reservations.append((lease_id, reservation_id))

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
def test_ledger_invariant_holds_under_acquire_release_expire_commit(
    postgres_url: str,
    migrated_engine: AsyncEngine,  # triggers `alembic upgrade head` once; not used directly
    ops: list[_Op],
) -> None:
    """P-10 / P-12 / P-20 — see module docstring."""
    engine = make_engine(postgres_url, poolclass=NullPool)
    try:
        asyncio.run(_run_sequence(engine, ops))
    finally:
        asyncio.run(engine.dispose())
