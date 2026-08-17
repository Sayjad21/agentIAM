"""Audit explorer search — T-048, `PLAN.md` line 1194.

Search reads the same `audit_records` table as `db/audit.py` (T-023, the chain and
`custody()`) and `db/decisions.py` (T-046, the live stream) — spec 08's append-only chain is
the one source of "what happened," so a search surface reading anything else could show a
judge a result `verify_chain` disagrees with.

Reuses `decisions.explain()` and `decisions.project_record()` rather than a second
cause-rendering path: a search hit and a live-stream row explaining the same denial two
different ways would just be a second place for that logic to drift out of sync.

Newest-first, unlike `read_since`'s oldest-first: the stream needs a cursor a client can
resume from, but a search result is a one-shot snapshot a judge reads top to bottom, and the
most recent hit is the one worth seeing without scrolling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from agentiam_controlplane.db.decisions import DecisionEvent, project_record
from agentiam_controlplane.db.models import AuditRecordRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["AuditSearchFilter", "AuditSearchResult", "search"]

#: A page of search results. Small: the explorer is read by a human scanning a table, not a
#: client draining a feed.
DEFAULT_LIMIT = 25
MAX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class AuditSearchFilter:
    """What the explorer asked to see. Every field `None` means unfiltered.

    Distinct from `decisions.DecisionFilter`: search additionally keys on `decision_id` and
    `task_id`, the two identifiers a judge or operator is actually handed — "show me this
    exact decision" and "show me what happened to this task" are the two questions T-048's
    accept criterion is written around, and neither is in the live-stream's filter set.
    """

    decision_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    agent_id: str | None = None
    principal_id: str | None = None
    scope: str | None = None
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class AuditSearchResult:
    """One page of hits, newest first, plus the total so the console can paginate."""

    events: list[DecisionEvent]
    total: int


async def search(
    session: AsyncSession,
    *,
    filters: AuditSearchFilter | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> AuditSearchResult:
    """Every audit record matching `filters`, newest first.

    `total` is counted separately from the page fetched, so the console can show "37 results"
    while only ever rendering `limit` rows.
    """
    active = filters or AuditSearchFilter()
    conditions = []
    if active.decision_id is not None:
        conditions.append(AuditRecordRow.decision_id == active.decision_id)
    if active.task_id is not None:
        conditions.append(AuditRecordRow.record["task_id"].astext == str(active.task_id))
    if active.agent_id is not None:
        conditions.append(AuditRecordRow.record["agent_id"].astext == active.agent_id)
    if active.principal_id is not None:
        conditions.append(AuditRecordRow.record["principal_id"].astext == active.principal_id)
    if active.scope is not None:
        conditions.append(AuditRecordRow.record["scope"].astext == active.scope)
    if active.outcome is not None:
        conditions.append(AuditRecordRow.record["outcome"].astext == active.outcome)

    count_stmt = select(func.count()).select_from(AuditRecordRow)
    stmt = select(AuditRecordRow)
    for condition in conditions:
        count_stmt = count_stmt.where(condition)
        stmt = stmt.where(condition)

    total = (await session.execute(count_stmt)).scalar_one()

    bounded_limit = min(limit, MAX_LIMIT)
    stmt = stmt.order_by(AuditRecordRow.seq.desc()).limit(bounded_limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return AuditSearchResult(events=[project_record(row) for row in rows], total=total)
