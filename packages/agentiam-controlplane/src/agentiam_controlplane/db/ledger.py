"""`ACQUIRE`, `RELEASE`, `REAP`, `LEDGER_COMMIT` — spec 04 §4.1, §4.4, §4.5, §4.6.

`RESERVE` and `COMMIT` are PEP-side, local, no-network operations (spec 04 §4.2, §4.3) and
live in `agentiam_pep.lease` instead — this module only holds the operations that mutate the
database. Each operation here runs in its own serialized transaction on the rows it touches,
matching spec 04 §4's "everything inside BEGIN … COMMIT runs in one serialized transaction."
Callers pass a fresh `AsyncSession` per call (concurrent operations must not share a session
— SQLAlchemy sessions are not safe for concurrent use) and an explicit `now`, so every
operation is deterministic and testable without a real clock.

**No `max_fraction` clamp on `ACQUIRE`.** Spec 04 §4.1's pseudocode computes
`grant = min(requested, available, max_fraction * available)`, but that clamp belongs to
adaptive lease sizing (spec 04 §12, T-015 — deferred) rather than to a caller's explicit
`requested` amount. Applying it here is also mathematically incompatible with this
ticket's own acceptance test: it shrinks each subsequent grant by 25% of what remains
instead of ever reaching zero, so a fixed `requested` value never triggers `Insufficient`
in any finite number of calls. See ADR-015.

**`ledger_commit()` checks idempotency after locking the lease, not before.** Spec 04 §4.4's
own pseudocode writes the `reservation_id` dedup check *before* `SELECT lease FOR UPDATE`.
Taken literally that is a TOCTOU race: two concurrent commits carrying the same
`reservation_id` can both read "not yet settled" before either takes the lock, and both then
apply. Checking after the lock closes it — the postcondition (a duplicate is a no-op) is
identical, only the order of two independent reads changed. See ADR-016.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentiam_controlplane.db.models import (
    BudgetRow,
    LeaseRow,
    ReconciliationAnomalyRow,
    ReservationRow,
)
from agentiam_controlplane.errors import LeaseNotActiveError, LeaseUnavailableError
from agentiam_core.models import LeaseState

#: Clock-skew allowance (spec 04 §9). A PEP must stop using a lease at `expires_at - S`;
#: the reaper must not reclaim before `expires_at + S`. The `2S` gap is what keeps the two
#: views from ever overlapping. Fixed here until a later ticket needs it to be configurable
#: per deployment (spec 04 §12 lists it as a tunable, with this value as the default).
SKEW_ALLOWANCE = timedelta(seconds=5)


async def acquire(
    session: AsyncSession,
    *,
    mandate_id: uuid.UUID,
    dimension: str,
    requested: Decimal,
    pep_id: str,
    ttl: timedelta,
    now: datetime,
) -> LeaseRow:
    """`ACQUIRE` — spec 04 §4.1.

    Raises:
        LeaseUnavailableError: `grant <= 0` — the pool has nothing left to give.
    """
    async with session.begin():
        result = await session.execute(
            select(BudgetRow)
            .where(BudgetRow.mandate_id == mandate_id, BudgetRow.dimension == dimension)
            .with_for_update()
        )
        budget = result.scalar_one()
        available = budget.total - budget.committed - budget.leased
        grant = min(requested, available)
        if grant <= 0:
            raise LeaseUnavailableError(
                f"no budget available for mandate {mandate_id} dimension {dimension!r}"
            )
        budget.leased += grant
        lease = LeaseRow(
            budget_id=budget.id,
            pep_id=pep_id,
            granted=grant,
            settled=Decimal("0"),
            granted_at=now,
            expires_at=now + ttl,
            state=LeaseState.ACTIVE.value,
        )
        session.add(lease)
        await session.flush()
    return lease


async def release(session: AsyncSession, *, lease_id: uuid.UUID) -> bool:
    """`RELEASE` — spec 04 §4.5.

    Returns:
        `True` if this call performed the decrement, `False` if the lease was already in a
        terminal state (idempotent no-op — spec 04 §3: each exit decrements `leased` by
        `outstanding` exactly once).
    """
    async with session.begin():
        return await _retire(session, lease_id=lease_id, next_state=LeaseState.RELEASED)


async def reap(session: AsyncSession, *, now: datetime) -> list[uuid.UUID]:
    """`REAP` — spec 04 §4.6. One full pass; returns the ids of leases reclaimed.

    Only reclaims leases past `expires_at + S`, not bare `expires_at` — spec 04 §9's
    clock-skew margin. Reclaiming early can return budget to the pool and re-issue it
    while a lagging PEP still believes its lease is live (spec 04 §9, measured).
    """
    cutoff = now - SKEW_ALLOWANCE
    async with session.begin():
        result = await session.execute(
            select(LeaseRow.id).where(
                LeaseRow.state == LeaseState.ACTIVE.value,
                LeaseRow.expires_at < cutoff,
            )
        )
        candidate_ids = list(result.scalars().all())

    reclaimed: list[uuid.UUID] = []
    for lease_id in candidate_ids:
        async with session.begin():
            if await _retire(session, lease_id=lease_id, next_state=LeaseState.EXPIRED):
                reclaimed.append(lease_id)
    return reclaimed


async def ledger_commit(
    session: AsyncSession,
    *,
    lease_id: uuid.UUID,
    reservation_id: uuid.UUID,
    amount: Decimal,
    now: datetime,
) -> bool:
    """`LEDGER_COMMIT` — spec 04 §4.4.

    Idempotent by `reservation_id` (G4, spec 04 §10): a row in `reservations` carrying that
    id is the entire dedup mechanism, so a replay finds it already there and applies nothing.
    `amount` is clamped to `lease.outstanding` (G2) rather than trusted — a compromised or
    buggy PEP cannot drive `leased` negative by over-reporting.

    Returns:
        `True` if this call mutated the ledger, `False` if it was a no-op — either the
        `reservation_id` was already settled (idempotent replay) or the clamped amount was
        `<= 0` (the lease was already fully settled by a prior, different commit).

    Raises:
        LeaseNotActiveError: the lease is not `active` (G3, spec 04 §11 / ADR-009) — a late
            commit against a lease already released, reaped, or revoked. Rejected rather than
            applied, and recorded as a `ReconciliationAnomalyRow` so the divergence is
            surfaced instead of silently corrupting `leased` (TM-21).
    """
    reject_state: str | None = None
    async with session.begin():
        lease_result = await session.execute(
            select(LeaseRow).where(LeaseRow.id == lease_id).with_for_update()
        )
        lease = lease_result.scalar_one()

        # Checked after the lock, not before — see this module's docstring and ADR-016.
        dup = await session.execute(
            select(ReservationRow.id).where(ReservationRow.id == reservation_id)
        )
        if dup.scalar_one_or_none() is not None:
            return False

        if lease.state != LeaseState.ACTIVE.value:
            session.add(
                ReconciliationAnomalyRow(
                    lease_id=lease_id,
                    reservation_id=reservation_id,
                    reported_amount=amount,
                    lease_state=lease.state,
                    created_at=now,
                )
            )
            reject_state = lease.state
        else:
            applied = min(amount, lease.outstanding)
            if applied <= 0:
                return False
            budget_result = await session.execute(
                select(BudgetRow).where(BudgetRow.id == lease.budget_id).with_for_update()
            )
            budget = budget_result.scalar_one()
            budget.committed += applied
            budget.leased -= applied
            lease.settled += applied
            session.add(
                ReservationRow(id=reservation_id, lease_id=lease_id, amount=applied, created_at=now)
            )

    if reject_state is not None:
        raise LeaseNotActiveError(
            f"lease {lease_id} is {reject_state!r}, not active — commit rejected (spec 04 §11)"
        )
    return True


async def _retire(session: AsyncSession, *, lease_id: uuid.UUID, next_state: LeaseState) -> bool:
    """Shared body of `RELEASE` and `REAP`'s per-lease step — spec 04 §3, §6.

    Both exits are the same shape: lock the lease, no-op if it already left `active`,
    otherwise lock the budget row and return `outstanding` to the pool in the same
    transaction as the state transition. Must be called inside a caller-managed
    `session.begin()` block.
    """
    lease_result = await session.execute(
        select(LeaseRow).where(LeaseRow.id == lease_id).with_for_update()
    )
    lease = lease_result.scalar_one()
    if lease.state != LeaseState.ACTIVE.value:
        return False

    budget_result = await session.execute(
        select(BudgetRow).where(BudgetRow.id == lease.budget_id).with_for_update()
    )
    budget = budget_result.scalar_one()
    budget.leased -= lease.outstanding
    lease.state = next_state.value
    return True
