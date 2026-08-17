"""Decision counts for Grafana's Decisions dashboard — T-049, `PLAN.md` §1197.

One grouped query over `audit_records` — the same table `db/decisions.py` streams from —
not a second projection of `DecisionEvent`: a Prometheus gauge needs only the label values
(`outcome`, `reason_code`) and a count, never the rest of the record.

Cumulative rather than windowed. Prometheus already owns the window a panel is drawn over
(`rate()`, `increase()`); a query that pre-windowed here would fight it rather than feed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from agentiam_controlplane.db.models import AuditRecordRow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["OutcomeCount", "count_by_outcome_reason"]


@dataclass(frozen=True, slots=True)
class OutcomeCount:
    """One `(outcome, reason_code)` pair and how many decisions have carried it."""

    outcome: str
    reason_code: str
    count: int


async def count_by_outcome_reason(session: AsyncSession) -> Sequence[OutcomeCount]:
    """Every `(outcome, reason_code)` pair in the audit chain, with its running total."""
    outcome_col = AuditRecordRow.record["outcome"].as_string().label("outcome")
    reason_col = AuditRecordRow.record["reason_code"].as_string().label("reason_code")
    stmt = select(outcome_col, reason_col, func.count().label("n")).group_by(
        outcome_col, reason_col
    )
    rows = (await session.execute(stmt)).all()
    return [
        OutcomeCount(
            outcome=row.outcome or "unknown",
            reason_code=row.reason_code or "UNKNOWN",
            count=int(row.n),
        )
        for row in rows
    ]
