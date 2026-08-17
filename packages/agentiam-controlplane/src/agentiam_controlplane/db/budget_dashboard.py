"""What the budget and lease dashboard shows — T-047, `PLAN.md` §1192.

Four numbers, and the fourth is the one a judge will look at:

1. **Per-mandate spend gauge.** A pool row's `total` split into `committed` (spent),
   `leased` (held by a PEP but unspent), `allocated` (carved out to a child) and what is
   left. Those four are exactly the terms of the pool invariant — `committed + leased +
   allocated <= total`, spec 04 §2.1 — so the gauge is a picture of the invariant rather
   than a separate accounting of it.
2. **Lease utilization.** Of what a PEP is holding, how much has actually been spent:
   `settled / granted` across that pool's active leases. Low utilization with high `leased`
   is the stranding failure mode spec 04 §7 exists to bound, and it is invisible in the
   spend gauge alone.
3. **Top-up rate.** `ACQUIRE`s per minute, from `leases.granted_at` over a window. Rising
   top-up rate against flat spend is a PEP thrashing on a lease that is too small (T-015,
   deferred) — the number that would tell you.
4. **Invariant checker status.** T-016's sweep, run live, as one boolean plus the
   violations. `PLAN.md` calls this "the single most persuasive thing in the demo".

Money stays `Decimal` end to end and is serialised as a string, never a float (Rule 4). A
gauge that rounded through binary floating point would be a gauge of the wrong number.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from agentiam_controlplane.db.invariants import check_in_session
from agentiam_controlplane.db.models import BudgetRow, LeaseRow
from agentiam_core.models import LeaseState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["Dashboard", "MandateGauge", "build_dashboard"]

#: The window the top-up rate is measured over. One minute reads naturally ("12 top-ups a
#: minute") and is short enough that the demo's three concurrent agents move it visibly.
DEFAULT_WINDOW = timedelta(minutes=1)

_ZERO = Decimal("0")


def _ratio(part: Decimal, whole: Decimal) -> str:
    """`part / whole` as a 4-place decimal string, and 0 when `whole` is 0.

    A zero total is a real state — a pool created but never funded — so it must render as
    0% rather than raise. Returned as a string for the same reason the amounts are: this
    goes to JSON, and a float here would undo the care taken everywhere else.
    """
    if whole == _ZERO:
        return "0.0000"
    return str((part / whole).quantize(Decimal("0.0001")))


@dataclass(frozen=True, slots=True)
class MandateGauge:
    """One pool's spend picture, plus how well its leases are being used."""

    budget_id: uuid.UUID
    mandate_id: uuid.UUID
    dimension: str
    total: Decimal
    committed: Decimal
    leased: Decimal
    allocated: Decimal
    #: `total - committed - leased - allocated`. Never negative while the pool invariant
    #: holds, which is why the checker's verdict sits beside this on the page.
    available: Decimal
    active_leases: int
    lease_granted: Decimal
    lease_settled: Decimal

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready. Every amount a string — Rule 4 reaches the wire, not just the DB."""
        return {
            "budget_id": str(self.budget_id),
            "mandate_id": str(self.mandate_id),
            "dimension": self.dimension,
            "total": str(self.total),
            "committed": str(self.committed),
            "leased": str(self.leased),
            "allocated": str(self.allocated),
            "available": str(self.available),
            "spend_fraction": _ratio(self.committed, self.total),
            "active_leases": self.active_leases,
            "lease_granted": str(self.lease_granted),
            "lease_settled": str(self.lease_settled),
            # Of what is held, how much has been spent. Distinct from `spend_fraction`:
            # this one exposes stranding, which the spend gauge cannot see.
            "lease_utilization": _ratio(self.lease_settled, self.lease_granted),
        }


@dataclass(frozen=True, slots=True)
class Dashboard:
    """Everything the page renders in one snapshot."""

    generated_at: datetime
    gauges: tuple[MandateGauge, ...]
    #: `ACQUIRE`s inside the window, and the window itself, so the page can label the rate
    #: rather than hard-coding "per minute" and being wrong if the window changes.
    topups_in_window: int
    window_seconds: float
    invariants_ok: bool
    budgets_checked: int
    violations: tuple[dict[str, Any], ...]
    check_duration_ms: float

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready."""
        per_minute = (
            self.topups_in_window * 60.0 / self.window_seconds if self.window_seconds else 0.0
        )
        return {
            "generated_at": self.generated_at.isoformat(),
            "gauges": [gauge.as_dict() for gauge in self.gauges],
            "topups_in_window": self.topups_in_window,
            "window_seconds": self.window_seconds,
            "topups_per_minute": round(per_minute, 2),
            "invariants_ok": self.invariants_ok,
            "budgets_checked": self.budgets_checked,
            "violations": list(self.violations),
            "check_duration_ms": round(self.check_duration_ms, 2),
        }


async def build_dashboard(
    session: AsyncSession,
    *,
    now: datetime,
    window: timedelta = DEFAULT_WINDOW,
) -> Dashboard:
    """Assemble the snapshot.

    Pool rows only. An allocation row's `total` is a slice of its parent's, so including
    both would double-count the same money and show a mandate as over-committed when it is
    not — the kind of wrong number that makes an operator distrust the whole page.
    """
    pools = (
        (
            await session.execute(
                select(BudgetRow)
                .where(BudgetRow.parent_budget_id.is_(None))
                .order_by(BudgetRow.mandate_id, BudgetRow.dimension)
            )
        )
        .scalars()
        .all()
    )

    # One grouped query for lease figures rather than one per pool: the demo has few pools
    # but a chaos run has many, and N+1 on a page that polls is a self-inflicted load test.
    lease_rows = (
        await session.execute(
            select(
                LeaseRow.budget_id,
                func.count().label("n"),
                func.coalesce(func.sum(LeaseRow.granted), _ZERO).label("granted"),
                func.coalesce(func.sum(LeaseRow.settled), _ZERO).label("settled"),
            )
            .where(LeaseRow.state == LeaseState.ACTIVE.value)
            .group_by(LeaseRow.budget_id)
        )
    ).all()
    by_budget = {row.budget_id: row for row in lease_rows}

    gauges: list[MandateGauge] = []
    for pool in pools:
        leases = by_budget.get(pool.id)
        gauges.append(
            MandateGauge(
                budget_id=pool.id,
                mandate_id=pool.mandate_id,
                dimension=str(pool.dimension),
                total=pool.total,
                committed=pool.committed,
                leased=pool.leased,
                allocated=pool.allocated,
                available=pool.total - pool.committed - pool.leased - pool.allocated,
                active_leases=int(leases.n) if leases else 0,
                lease_granted=leases.granted if leases else _ZERO,
                lease_settled=leases.settled if leases else _ZERO,
            )
        )

    topups = (
        await session.execute(
            select(func.count()).select_from(LeaseRow).where(LeaseRow.granted_at >= now - window)
        )
    ).scalar_one()

    report = await check_in_session(session)

    return Dashboard(
        generated_at=now,
        gauges=tuple(gauges),
        topups_in_window=int(topups),
        window_seconds=window.total_seconds(),
        invariants_ok=not report.violations,
        budgets_checked=report.budgets_checked,
        violations=tuple(
            {
                "budget_id": str(v.budget_id),
                "mandate_id": str(v.mandate_id),
                "dimension": v.dimension,
                "kind": v.kind.value,
                "expected": str(v.expected),
                "actual": str(v.actual),
                "delta": str(v.delta),
                # `Violation.__str__` is already the one-line form written for "a log, a
                # chaos run's output, and the console" — this *is* the console, so reuse it
                # rather than inventing a second phrasing that could drift from it.
                "detail": str(v),
            }
            for v in report.violations
        ),
        check_duration_ms=report.duration_ms,
    )
