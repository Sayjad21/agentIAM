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
from collections.abc import Mapping, Sequence
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
from agentiam_controlplane.errors import (
    AllocationError,
    LeaseNotActiveError,
    LeaseUnavailableError,
)
from agentiam_core.models import LeaseState

#: Clock-skew allowance (spec 04 §9). A PEP must stop using a lease at `expires_at - S`;
#: the reaper must not reclaim before `expires_at + S`. The `2S` gap is what keeps the two
#: views from ever overlapping. Fixed here until a later ticket needs it to be configurable
#: per deployment (spec 04 §12 lists it as a tunable, with this value as the default).
SKEW_ALLOWANCE = timedelta(seconds=5)


async def split_budget(
    session: AsyncSession,
    *,
    parent_budget_id: uuid.UUID,
    dimension: str,
    allocations: Mapping[str, Decimal],
) -> dict[str, uuid.UUID]:
    """Carve a pool into per-child allocations — spec 04 §13, the static half of INV-5.

    The whole division happens under the parent's row lock, in one transaction: the sum is
    checked against what the parent actually has left, then every child row is created and
    the parent's `allocated` is raised by the same total. Splitting in two steps would let
    a concurrent `ACQUIRE` slip between the check and the writes and lease out budget this
    call has already promised away.

    Args:
        session: A fresh session.
        parent_budget_id: The pool row being divided.
        dimension: The dimension being divided. Recorded on each child row.
        allocations: `{agent_id: amount}`. Every amount must be positive.

    Returns:
        `{agent_id: budget_id}` for the rows created.

    Raises:
        ValueError: No allocations given, or one is not positive. Both are programming
            errors: a child with no budget is expressed by a `BudgetCeiling` caveat of
            zero, not by a ledger row of zero.
        AllocationError: The split exceeds what the parent can promise. Nothing is
            created — the check runs before any insert, inside the lock.
    """
    if not allocations:
        raise ValueError("split_budget() needs at least one allocation")
    for agent_id, amount in allocations.items():
        if amount <= 0:
            raise ValueError(f"allocation for {agent_id!r} must be positive, got {amount}")

    requested = sum(allocations.values(), Decimal(0))

    async with session.begin():
        parent = (
            await session.execute(
                select(BudgetRow).where(BudgetRow.id == parent_budget_id).with_for_update()
            )
        ).scalar_one()

        if requested > parent.available:
            raise AllocationError(
                f"cannot allocate {requested} from budget {parent_budget_id}: "
                f"only {parent.available} of {parent.total} is unspoken for "
                f"(committed {parent.committed}, leased {parent.leased}, "
                f"already allocated {parent.allocated})"
            )

        created: dict[str, uuid.UUID] = {}
        for agent_id, amount in allocations.items():
            child = BudgetRow(
                mandate_id=parent.mandate_id,
                dimension=dimension,
                total=amount,
                parent_budget_id=parent.id,
                agent_id=agent_id,
            )
            session.add(child)
            await session.flush()
            created[agent_id] = child.id

        parent.allocated += requested

    return created


async def acquire(
    session: AsyncSession,
    *,
    mandate_id: uuid.UUID,
    dimension: str,
    requested: Decimal,
    pep_id: str,
    ttl: timedelta,
    now: datetime,
    agent_id: str | None = None,
) -> LeaseRow:
    """`ACQUIRE` — spec 04 §4.1.

    Args:
        session: A fresh session; concurrent acquires must not share one.
        mandate_id: Which mandate's budget to draw from.
        dimension: Which budget dimension.
        requested: How much the caller wants. The grant is `min(requested, available)`.
        pep_id: Which PEP instance is asking.
        ttl: How long the lease lives.
        now: The caller's instant; `expires_at` is `now + ttl`.
        agent_id: Draw from this agent's **allocation** instead of the shared pool. `None`
            — the default and spec 04 §13's default mode — means the pool row itself, so
            every sibling competes for one balance.

    Returns:
        The new lease.

    Raises:
        LeaseUnavailableError: `grant <= 0` — nothing left to give.
    """
    async with session.begin():
        # A split gives one mandate several rows for the same dimension, so this lookup has
        # to say *which*. Before T-017 it was `scalar_one()` on `(mandate_id, dimension)`;
        # unqualified, that now raises `MultipleResultsFound` the moment a split exists.
        row_filter = (
            BudgetRow.agent_id == agent_id
            if agent_id is not None
            else BudgetRow.parent_budget_id.is_(None)
        )
        result = await session.execute(
            select(BudgetRow)
            .where(
                BudgetRow.mandate_id == mandate_id,
                BudgetRow.dimension == dimension,
                row_filter,
            )
            .with_for_update()
        )
        budget = result.scalar_one()
        available = budget.available
        grant = min(requested, available)
        if grant <= 0:
            held_by = f" allocation {agent_id!r}" if agent_id is not None else " pool"
            raise LeaseUnavailableError(
                f"no budget available in the{held_by} for mandate {mandate_id} "
                f"dimension {dimension!r}"
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

    A batch of one, so every race and idempotency test written against this function is
    also a test of `ledger_commit_batch`, which is the only implementation.

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
    applied = await ledger_commit_batch(
        session, lease_id=lease_id, items=((reservation_id, amount, now),)
    )
    return applied[0]


async def ledger_commit_batch(
    session: AsyncSession,
    *,
    lease_id: uuid.UUID,
    items: Sequence[tuple[uuid.UUID, Decimal, datetime]],
) -> list[bool]:
    """Settle several reservations against **one** lease in a single transaction — T-053.

    Semantically identical to calling `ledger_commit` once per item, and that is the bar:
    every guard keeps its meaning per item. G4 still dedups each `reservation_id`
    individually, G2 still clamps each amount, and G3 still refuses the whole batch if the
    lease has left `active`.

    **Why it exists.** `LEDGER_COMMIT` takes `FOR UPDATE` on the lease row and then on the
    budget row, and the budget row is *shared by every PEP leasing from that mandate*. One
    settlement per transaction therefore serialises every instance against every other, and
    T-052's CH-10 measured what that costs: with three instances under load the queue could
    not keep up, and because `LeasePool._release` drains before retiring a lease, a backlog
    larger than a lease's spending stalled the top-up behind it — 7,053 of 21,006 requests
    refused, all `LEASE_UNAVAILABLE`, the PEP failing closed correctly the whole time.
    ADR-049 deferred the fix to here.

    Batching turns N lock acquisitions into one. It does not make the lock shorter in
    absolute terms, but it removes the per-item transaction overhead and, more importantly,
    the N round trips between them.

    **The clamp is cumulative, and it has to be.** `outstanding` shrinks as items apply
    within the batch, so each item is clamped against what is left *after* its predecessors
    rather than against the value read at the top. Clamping every item against the opening
    `outstanding` would let a batch drive `leased` negative — exactly what G2 exists to
    prevent — and no single-item test would ever catch it.

    Args:
        session: A fresh session; the whole batch runs in one transaction.
        lease_id: The lease every item settles against. Callers group by lease.
        items: `(reservation_id, amount, now)` per settlement.

    Returns:
        One `True`/`False` per item, in order, meaning the same as `ledger_commit`'s.

    Raises:
        LeaseNotActiveError: The lease is not `active`. Every item is recorded as a
            reconciliation anomaly first, so a batch does not lose the divergence detail a
            sequence of single commits would have written (TM-21).
    """
    if not items:
        return []

    results = [False] * len(items)
    reject_state: str | None = None

    async with session.begin():
        lease = (
            await session.execute(select(LeaseRow).where(LeaseRow.id == lease_id).with_for_update())
        ).scalar_one()

        # Checked after the lock, not before — see this module's docstring and ADR-016.
        # One statement for the whole batch rather than one per item: the dedup set is a
        # read, and N round trips to answer a question the database can answer once is the
        # cost this function exists to remove.
        already = set(
            (
                await session.execute(
                    select(ReservationRow.id).where(
                        ReservationRow.id.in_([reservation for reservation, _, _ in items])
                    )
                )
            )
            .scalars()
            .all()
        )

        if lease.state != LeaseState.ACTIVE.value:
            for reservation_id, amount, now in items:
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
            budget: BudgetRow | None = None
            for index, (reservation_id, amount, now) in enumerate(items):
                if reservation_id in already:
                    continue
                # Added *before* the insert, so a `reservation_id` repeated **within** this
                # batch is a no-op exactly as a replay across batches is. Without it the
                # duplicate reaches the primary key, the insert raises, and the whole
                # transaction rolls back — so a batch containing one accidental repeat
                # silently applies *nothing*. Caught by
                # `test_a_replayed_settlement_applies_once`, which enqueues the same
                # outcome three times and used to see them land in three transactions.
                already.add(reservation_id)
                # `lease.outstanding` is derived from `granted - settled`, and `settled` is
                # mutated below, so this re-reads the *running* remainder each time.
                applied = min(amount, lease.outstanding)
                if applied <= 0:
                    continue
                if budget is None:
                    budget = (
                        await session.execute(
                            select(BudgetRow)
                            .where(BudgetRow.id == lease.budget_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                budget.committed += applied
                budget.leased -= applied
                lease.settled += applied
                session.add(
                    ReservationRow(
                        id=reservation_id,
                        lease_id=lease_id,
                        amount=applied,
                        created_at=now,
                    )
                )
                results[index] = True

    if reject_state is not None:
        raise LeaseNotActiveError(
            f"lease {lease_id} is {reject_state!r}, not active — commit rejected (spec 04 §11)"
        )
    return results


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
