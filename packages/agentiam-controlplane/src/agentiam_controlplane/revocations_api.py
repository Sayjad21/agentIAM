"""The `/v1/revocations` JSON API — T-038, `PLAN.md` §8, spec 07 §4.

`PLAN.md` §8 only sketches the pull side (`GET /v1/revocations?since=seq`). A write endpoint
has to exist too — nothing else in the plan proposes one — so this adds `POST /v1/revocations`
alongside it, gated the same way T-037's escalation approve/deny are (ADR-041): no OIDC login
exists yet (T-043), so `revoked_by` must name an id from the same `ControlPlaneSettings
.approvers` config list rather than coming from a session. Reusing that set rather than
inventing a second one keeps one stopgap instead of two.

`GET .../revocations` needs no such check: it hands back which block ids are revoked, which
every PEP has to be able to read routinely (spec 07 §5.2) and which carries no privilege of
its own to leak.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentiam_controlplane.db import revocations as store

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from agentiam_controlplane.db.models import RevocationRow
    from agentiam_controlplane.db.revocations import RevocationPublisher
    from agentiam_controlplane.settings import ControlPlaneSettings

__all__ = ["build_router"]

_VALID_SCOPES = ("token", "subtree", "mandate")


def _serialize(row: RevocationRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "seq": row.seq,
        "block_id": row.block_id,
        "scope": row.scope,
        "reason": row.reason,
        "revoked_by": row.revoked_by,
        "revoked_at": row.revoked_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
    }


class RevokeRequest(BaseModel):
    """`POST /v1/revocations` — spec 07 §4.1."""

    block_id: str = Field(min_length=1)
    scope: str
    reason: str
    revoked_by: str
    expires_at: datetime


def build_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: ControlPlaneSettings,
    publisher: RevocationPublisher | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Build the two revocation routes, bound to `session_factory`, `settings` and `publisher`.

    `publisher=None` is the pull-only deployment shape (spec 07 §5.2) — revoking still works,
    only the fast path is absent.
    """
    router = APIRouter(prefix="/v1/revocations", tags=["revocations"])

    @router.post("", status_code=201)
    async def revoke(body: RevokeRequest) -> JSONResponse:
        if body.scope not in _VALID_SCOPES:
            return JSONResponse(
                {"detail": f"scope must be one of: {', '.join(_VALID_SCOPES)}"},
                status_code=400,
            )
        if body.revoked_by not in settings.approvers:
            return JSONResponse(
                {"detail": f"{body.revoked_by!r} is not an authorized revoker"},
                status_code=403,
            )
        async with session_factory() as session:
            record = await store.revoke(
                session,
                block_id=body.block_id,
                scope=body.scope,
                reason=body.reason,
                revoked_by=body.revoked_by,
                expires_at=body.expires_at,
                now=now(),
                publisher=publisher,
            )
        return JSONResponse(_serialize(record), status_code=201)

    @router.get("")
    async def list_revocations(since: int = Query(default=0)) -> JSONResponse:
        async with session_factory() as session:
            entries = await store.pull(session, since_seq=since)
        next_seq = entries[-1].seq if entries else since
        return JSONResponse({"entries": [_serialize(r) for r in entries], "next_seq": next_seq})

    return router
