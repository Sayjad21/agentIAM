"""Audit explorer + custody view — T-048, `PLAN.md` line 1194.

Three endpoints, each read-only:

* `GET /v1/audit/search` — filtered search over `audit_records`, newest first.
* `GET /v1/audit/custody/{task_id}` — the full principal→caveat chain for one task, in the
  order it happened, each entry carrying `decisions.explain()`'s narrative sentence rather
  than a bare outcome. This is `db/audit.py`'s `custody()` (T-023, already proven against real
  tampering, deletion and reordering) exposed live rather than rebuilt.
* `POST /v1/audit/verify` — runs `verify_chain()` (also T-023) against the live database and
  returns its verdict. A `GET` would be cached by an intermediary; a verification result must
  never be served stale.

Mounted unconditionally, like `tree_api`/`decisions_api`/`budgets_api`: every route answers
503 without a database, which is visibly "not wired" rather than a boot failure.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query

from agentiam_controlplane.db.audit import custody, verify_chain
from agentiam_controlplane.db.audit_search import (
    DEFAULT_LIMIT,
    AuditSearchFilter,
    search,
)
from agentiam_controlplane.db.decisions import explain

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["build_router"]


def build_router(
    *,
    session_factory: Callable[[], AsyncSession] | None,
) -> APIRouter:
    """Wire `/v1/audit`."""
    router = APIRouter(prefix="/v1/audit", tags=["Audit"])

    async def get_session() -> AsyncGenerator[AsyncSession, None]:
        if session_factory is None:
            raise HTTPException(status_code=503, detail="no database configured")
        async with session_factory() as session:
            yield session

    @router.get("/search")
    async def search_records(
        decision_id: uuid.UUID | None = Query(None),  # noqa: B008
        task_id: uuid.UUID | None = Query(None),  # noqa: B008
        agent_id: str | None = Query(None),
        principal_id: str | None = Query(None),
        scope: str | None = Query(None),
        outcome: str | None = Query(None),
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
        offset: int = Query(0, ge=0),
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        """A page of audit records matching the given filters, newest first."""
        result = await search(
            session,
            filters=AuditSearchFilter(
                decision_id=decision_id,
                task_id=task_id,
                agent_id=agent_id,
                principal_id=principal_id,
                scope=scope,
                outcome=outcome,
            ),
            limit=limit,
            offset=offset,
        )
        return {
            "results": [event.as_dict() for event in result.events],
            "total": result.total,
            "limit": limit,
            "offset": offset,
        }

    @router.get("/custody/{task_id}")
    async def get_custody(
        task_id: uuid.UUID,
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        """The full principal→caveat chain for one task, oldest first, as a narrative.

        Each entry carries `explanation` from the same `explain()` the live stream uses, so
        a refusal in the custody view names the exact caveat rather than a generic message —
        the same guarantee T-046 gave the live feed, here for the historical record.
        """
        entries = await custody(session, task_id=task_id)
        narrative: list[dict[str, Any]] = [
            {
                "seq": entry.seq,
                "decision_id": entry.record.get("decision_id"),
                "timestamp": entry.record.get("timestamp"),
                "agent_id": entry.agent_id,
                "depth": entry.depth,
                "scope": entry.scope,
                "tool_id": entry.record.get("tool_id"),
                "outcome": entry.outcome,
                "reason_code": entry.reason_code,
                "explanation": explain(entry.record),
            }
            for entry in entries
        ]
        return {"task_id": str(task_id), "entries": narrative}

    @router.post("/verify")
    async def verify(
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        """Walk the whole chain right now and report the verdict — never cached."""
        result = await verify_chain(session)
        return {
            "ok": result.ok,
            "checked": result.checked,
            "first_bad_seq": result.first_bad_seq,
            "detail": result.detail,
        }

    return router
