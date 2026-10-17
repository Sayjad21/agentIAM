"""`POST /v1/mandates` — issuing a mandate and its spend pool (APICTA-02 Part B).

Issuing a mandate allocates the organization's money to a task, so it takes the evidence
revocation takes: a signed-in session or an operator token, belonging to an approver
(`revocations_api._acting_principal`). The mandate's `principal_id` is that caller —
derived, never read from the body — because the principal is who the task's agents act for.

The id is minted here, not chosen by the caller, so two callers cannot collide on one and a
caller cannot claim an id a PEP already serves. The console mints its own tasks in-process
through the same `db.mandates.create` (Part C); this route is the same operation for a script.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentiam_controlplane.db import mandates as store
from agentiam_controlplane.revocations_api import _acting_principal

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from agentiam_controlplane.db.mandates import MandatePublisher
    from agentiam_controlplane.settings import ControlPlaneSettings

__all__ = ["DEFAULT_TTL", "IssueMandateRequest", "build_router"]

#: How long a mandate issued without an explicit expiry lasts — a working day, which is what
#: the demo's own mandate is minted for.
DEFAULT_TTL = timedelta(hours=8)


class IssueMandateRequest(BaseModel):
    """What the caller chooses: the ceiling, and optionally the task and the expiry."""

    spend_bdt: Decimal = Field(gt=0, max_digits=20, decimal_places=4)
    task_id: uuid.UUID | None = None
    expires_at: datetime | None = None


def build_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: ControlPlaneSettings,
    publisher: MandatePublisher | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Mount `POST /v1/mandates` against `session_factory`, announcing through `publisher`."""
    router = APIRouter(prefix="/v1/mandates", tags=["mandates"])

    @router.post("", status_code=201)
    async def issue(request: Request, body: IssueMandateRequest) -> JSONResponse:
        principal = _acting_principal(request, settings=settings)
        if principal is None:
            return JSONResponse(
                {"detail": "issuing a mandate requires a signed-in session or an operator token"},
                status_code=401,
            )
        if principal not in settings.approvers:
            return JSONResponse(
                {"detail": f"{principal!r} may not issue mandates"}, status_code=403
            )
        instant = now()
        expires_at = body.expires_at or instant + DEFAULT_TTL
        if expires_at <= instant:
            return JSONResponse({"detail": "expires_at must be in the future"}, status_code=422)
        async with session_factory() as session:
            row = await store.create(
                session,
                mandate_id=uuid.uuid4(),
                task_id=body.task_id or uuid.uuid4(),
                principal_id=principal,
                spend_total=body.spend_bdt,
                expires_at=expires_at,
                now=instant,
                publisher=publisher,
            )
        return JSONResponse(
            {
                "mandate_id": str(row.mandate_id),
                "task_id": str(row.task_id),
                "principal_id": row.principal_id,
                "spend_bdt": str(body.spend_bdt),
                "expires_at": expires_at.isoformat(),
            },
            status_code=201,
        )

    return router
