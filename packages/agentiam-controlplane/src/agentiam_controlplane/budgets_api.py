"""Budget and lease dashboard API — T-047, `PLAN.md` §1192.

One endpoint, `GET /v1/budgets/dashboard`, returning the whole snapshot.

**A snapshot rather than SSE, deliberately, and unlike T-046.** The decision stream carries
*events*, where missing one is a real loss and a cursor is the right shape. This page shows
*levels* — how much is committed, how much is leased, whether the invariant holds. Missing
an intermediate value costs nothing, because the next poll supersedes it. Streaming levels
would add a cursor, a heartbeat and a recycle path to deliver less useful information.

"Live" in the acceptance criterion is satisfied by the page refreshing itself, which is what
the invariant indicator needs: a colour that is current, not a log of colour changes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException

from agentiam_controlplane.db.budget_dashboard import build_dashboard

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = ["build_router"]


def build_router(
    *,
    session_factory: Callable[[], AsyncSession] | None,
    now: Callable[[], datetime] | None = None,
) -> APIRouter:
    """Wire `/v1/budgets`.

    A `None` `session_factory` yields 503, matching the tree and the decision stream: the
    console boots without a database and visibly cannot report, rather than failing to
    start.
    """
    router = APIRouter(prefix="/v1/budgets", tags=["Budgets"])

    def _now() -> datetime:
        return now() if now else datetime.now(UTC)

    async def get_session() -> AsyncGenerator[AsyncSession, None]:
        if session_factory is None:
            raise HTTPException(status_code=503, detail="no database configured")
        async with session_factory() as session:
            yield session

    @router.get("/dashboard")
    async def dashboard(
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Spend gauges, lease utilization, top-up rate, and the invariant verdict."""
        report = await build_dashboard(session, now=_now())
        return report.as_dict()

    return router
