"""The ledger's invariant checker — T-016, `PLAN.md` §9.

Four things must always be true of every budget row, and they are not equally defended.

| Invariant | Enforced by |
|---|---|
| `committed + leased + allocated <= total` | a `CHECK` (T-012, T-017) **and** this checker |
| `committed == Σ settled reservations` | this checker only |
| `leased == Σ outstanding of *active* leases` | this checker only |
| `allocated == Σ child allocations' totals` | this checker only |

Measured while writing this (T-016 probe): a plain `UPDATE` breaking the first is refused
by Postgres with an `IntegrityError`, while one breaking the second is accepted without
complaint — the `CHECK` compares columns of a single row and cannot see a sum over other
tables, or over other rows of its own. So the pool invariant is belt-and-braces here, and
the three *books* invariants are the reason the tool exists. That is exactly the split
ADR-010 describes: idempotency protects the books, not the pool, and nothing protected the
books.

The fourth arrived with T-017's proportional split, and has the same shape as the other
two: `allocated` records what a pool row has carved out into child rows, and only a sum
over `budgets` itself can confirm the two agree.

**Everything is read in one statement.** Not a style choice. The quantities are compared
against each other, so they have to come from one snapshot; read them in separate
statements and an `ACQUIRE` landing between two of them reports a violation that was never
real. A checker that cries wolf gets muted, which ends in the same place as one that never
fires — `tests/integration/test_invariant_checker.py` sweeps under concurrent load to keep
that honest.

The checker never writes and never locks. It is safe to run as a sidecar against a live
ledger, which is what T-052's chaos scenarios do and what Beat 4 puts on screen.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from agentiam_controlplane.db.base import make_session_factory
from agentiam_core.models import LeaseState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine


@unique
class InvariantKind(StrEnum):
    """Which invariant broke. Closed set — the console filters on these."""

    POOL = "pool"
    COMMITTED_VS_RESERVATIONS = "committed_vs_reservations"
    LEASED_VS_ACTIVE_LEASES = "leased_vs_active_leases"
    ALLOCATED_VS_CHILDREN = "allocated_vs_children"
    NEGATIVE_BALANCE = "negative_balance"

    @property
    def description(self) -> str:
        """One clause an operator can read without opening the spec."""
        return _DESCRIPTIONS[self]


_DESCRIPTIONS: Final[dict[InvariantKind, str]] = {
    InvariantKind.POOL: "committed + leased + allocated exceeds total",
    InvariantKind.COMMITTED_VS_RESERVATIONS: (
        "committed disagrees with the sum of settled reservations"
    ),
    InvariantKind.LEASED_VS_ACTIVE_LEASES: (
        "leased disagrees with the outstanding total of active leases"
    ),
    InvariantKind.ALLOCATED_VS_CHILDREN: (
        "allocated disagrees with the total of this budget's child allocations"
    ),
    InvariantKind.NEGATIVE_BALANCE: "committed, leased or allocated is negative",
}


@dataclass(frozen=True, slots=True)
class Violation:
    """One budget row failing one invariant.

    Carries `mandate_id` as well as `budget_id` because the operator reading this thinks
    in mandates — "whose task is broken?" — while the repair needs the row.
    """

    kind: InvariantKind
    budget_id: uuid.UUID
    mandate_id: uuid.UUID
    dimension: str
    expected: Decimal
    actual: Decimal

    @property
    def delta(self) -> Decimal:
        """`actual - expected`. The sign distinguishes over- from under-counting."""
        return self.actual - self.expected

    def __str__(self) -> str:
        """One line, for a log, a chaos run's output, and the console."""
        return (
            f"{self.kind.value}: mandate={self.mandate_id} dimension={self.dimension} "
            f"expected={self.expected} actual={self.actual} delta={self.delta:+} "
            f"({self.kind.description})"
        )


@dataclass(frozen=True, slots=True)
class CheckReport:
    """The result of one full sweep."""

    budgets_checked: int
    violations: tuple[Violation, ...]
    duration_ms: float

    @property
    def holds(self) -> bool:
        """True if every invariant held. An empty ledger holds — nothing to check is fine."""
        return not self.violations

    @property
    def exit_code(self) -> int:
        """0 when green, 1 when red. A chaos run and CI both branch on this."""
        return 0 if self.holds else 1

    def summary(self) -> str:
        """One line suitable for a status bar."""
        if self.holds:
            return (
                f"OK  {self.budgets_checked} budgets, all invariants hold "
                f"({self.duration_ms:.1f} ms)"
            )
        return (
            f"FAIL  {len(self.violations)} violation(s) across {self.budgets_checked} "
            f"budgets ({self.duration_ms:.1f} ms)"
        )


#: One statement, one snapshot. The two `LEFT JOIN`ed subqueries fold the reservation and
#: lease sums per budget; `LEFT` rather than inner so a budget with no leases yet is still
#: checked, with its sums coalesced to zero rather than dropping out of the result.
_SWEEP: Final = text(
    """
    SELECT
        b.id                                AS budget_id,
        b.mandate_id                        AS mandate_id,
        b.dimension                         AS dimension,
        b.total                             AS total,
        b.committed                         AS committed,
        b.leased                            AS leased,
        b.allocated                         AS allocated,
        COALESCE(settled.amount, 0)         AS settled_reservations,
        COALESCE(active.outstanding, 0)     AS active_outstanding,
        COALESCE(children.allocated, 0)     AS child_totals
    FROM budgets b
    LEFT JOIN (
        SELECT l.budget_id, SUM(r.amount) AS amount
        FROM reservations r
        JOIN leases l ON l.id = r.lease_id
        GROUP BY l.budget_id
    ) settled ON settled.budget_id = b.id
    LEFT JOIN (
        SELECT l.budget_id, SUM(l.granted - l.settled) AS outstanding
        FROM leases l
        WHERE l.state = :active_state
        GROUP BY l.budget_id
    ) active ON active.budget_id = b.id
    LEFT JOIN (
        SELECT c.parent_budget_id, SUM(c.total) AS allocated
        FROM budgets c
        WHERE c.parent_budget_id IS NOT NULL
        GROUP BY c.parent_budget_id
    ) children ON children.parent_budget_id = b.id
    """
)


def _violations_for(row: object) -> list[Violation]:
    """Every invariant this row breaks — all of them, not just the first.

    A partial diagnosis sends whoever is holding the pager down exactly one hole.
    """
    # `row` is a SQLAlchemy Row; attribute access is by label from `_SWEEP`.
    budget_id: uuid.UUID = row.budget_id  # type: ignore[attr-defined]
    mandate_id: uuid.UUID = row.mandate_id  # type: ignore[attr-defined]
    dimension: str = row.dimension  # type: ignore[attr-defined]
    total: Decimal = row.total  # type: ignore[attr-defined]
    committed: Decimal = row.committed  # type: ignore[attr-defined]
    leased: Decimal = row.leased  # type: ignore[attr-defined]
    settled: Decimal = row.settled_reservations  # type: ignore[attr-defined]
    outstanding: Decimal = row.active_outstanding  # type: ignore[attr-defined]
    allocated: Decimal = row.allocated  # type: ignore[attr-defined]
    child_totals: Decimal = row.child_totals  # type: ignore[attr-defined]

    def violation(kind: InvariantKind, expected: Decimal, actual: Decimal) -> Violation:
        return Violation(
            kind=kind,
            budget_id=budget_id,
            mandate_id=mandate_id,
            dimension=dimension,
            expected=expected,
            actual=actual,
        )

    found: list[Violation] = []

    if committed < 0 or leased < 0 or allocated < 0:
        # Reported against whichever went negative; `leased < 0` is the TM-21 signature.
        negative = next(v for v in (leased, committed, allocated) if v < 0)
        found.append(violation(InvariantKind.NEGATIVE_BALANCE, Decimal(0), negative))

    if committed + leased + allocated > total:
        found.append(violation(InvariantKind.POOL, total, committed + leased + allocated))

    if committed != settled:
        found.append(violation(InvariantKind.COMMITTED_VS_RESERVATIONS, settled, committed))

    if leased != outstanding:
        found.append(violation(InvariantKind.LEASED_VS_ACTIVE_LEASES, outstanding, leased))

    if allocated != child_totals:
        found.append(violation(InvariantKind.ALLOCATED_VS_CHILDREN, child_totals, allocated))

    return found


async def check_invariants(engine: AsyncEngine) -> CheckReport:
    """Sweep every budget once and report what does not hold.

    Read-only and lock-free — safe against a live ledger, which is the point: T-052 runs it
    as a sidecar through every chaos scenario, and the demo puts it on screen.

    Args:
        engine: An engine bound to the ledger database.

    Returns:
        A report naming every violation found, and how long the sweep took.
    """
    factory = make_session_factory(engine)
    started = time.perf_counter()
    async with factory() as session:
        rows: Sequence[object] = (
            await session.execute(_SWEEP, {"active_state": LeaseState.ACTIVE.value})
        ).all()
    duration_ms = (time.perf_counter() - started) * 1000

    violations: list[Violation] = []
    for row in rows:
        violations.extend(_violations_for(row))

    return CheckReport(
        budgets_checked=len(rows),
        violations=tuple(violations),
        duration_ms=duration_ms,
    )


__all__ = [
    "CheckReport",
    "InvariantKind",
    "Violation",
    "check_invariants",
]
