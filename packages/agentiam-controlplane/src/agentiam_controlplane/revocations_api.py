"""The `/v1/revocations` JSON API — T-038, `PLAN.md` §8, spec 07 §4.

`PLAN.md` §8 only sketches the pull side (`GET /v1/revocations?since=seq`). A write endpoint
has to exist too — nothing else in the plan proposes one — so this adds `POST /v1/revocations`
alongside it.

**Who may revoke, and how that is established.** This route originally carried ADR-041's
stopgap: the caller put its own id in a `revoked_by` body field and the route checked that
string against `ControlPlaneSettings.approvers`. ADR-046 retired that pattern for escalation
approve/deny once T-043 landed OIDC — "a body field saying `approver: alice` is a claim; a
session is evidence" — but this route was not migrated with it, and the claim was never
checked against anything. Measured on the demo stack: an unauthenticated `POST` naming a
real approver returned 201 and wrote a revocation, so anyone able to reach the control plane
could revoke the root authority block and cascade every agent to `ANCESTOR_REVOKED`.

The acting principal is now *derived*, never read off the request, from either:

- the OIDC session (`require_session_principal`), the same evidence approve/deny takes; or
- `Authorization: Bearer <secret>`, matched against `ControlPlaneSettings.operator_tokens`.

The second exists because revocation is incident response: it has to work from a script, and
from a deployment that leaves OIDC unwired (the demo stack does, deliberately). It is still
authentication — the caller proves possession of a secret — rather than assertion. With
neither configured nor presented the route refuses, so the failure direction is closed.

`revoked_by` is gone from the request body entirely, rather than left and ignored, so a
caller cannot believe it set something that no longer has any effect.

`GET .../revocations` needs no such check: it hands back which block ids are revoked, which
every PEP has to be able to read routinely (spec 07 §5.2) and which carries no privilege of
its own to leak.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hmac import compare_digest
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request
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


def _acting_principal(request: Request, *, settings: ControlPlaneSettings) -> str | None:
    """Who is making this request, or `None` if nothing authenticates them.

    The session is tried first so a signed-in operator using the console needs no second
    credential. `SessionMiddleware` is guaranteed here: `create_app` mounts this router only
    in the branch that installs it, the same guarantee `require_session_principal` relies on.
    """
    principal = request.session.get("principal_id")
    if principal:
        return str(principal)

    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    # RFC 6750 §2.1 makes the scheme case-insensitive; the PEP's extractor reads it the same
    # way, and disagreeing between the two services would be its own bug.
    if scheme.lower() != "bearer" or not presented.strip():
        return None
    offered = presented.strip().encode()
    matched: str | None = None
    for secret, operator in settings.operator_tokens.items():
        # Every entry is compared, and the result is not returned from inside the loop, so
        # neither the time taken nor the exit point depends on which secret matched.
        if compare_digest(offered, secret.encode()):
            matched = operator
    return matched


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
    async def revoke(request: Request, body: RevokeRequest) -> JSONResponse:
        if body.scope not in _VALID_SCOPES:
            return JSONResponse(
                {"detail": f"scope must be one of: {', '.join(_VALID_SCOPES)}"},
                status_code=400,
            )
        revoked_by = _acting_principal(request, settings=settings)
        if revoked_by is None:
            return JSONResponse(
                {"detail": "revoking requires a signed-in session or an operator token"},
                status_code=401,
            )
        # A session can exist for anyone Keycloak will authenticate, so being signed in is
        # not yet being allowed to revoke. An operator token has already been matched to an
        # approver at load, but is re-checked here so one rule states the answer.
        if revoked_by not in settings.approvers:
            return JSONResponse(
                {"detail": f"{revoked_by!r} is not an authorized revoker"},
                status_code=403,
            )
        async with session_factory() as session:
            record = await store.revoke(
                session,
                block_id=body.block_id,
                scope=body.scope,
                reason=body.reason,
                revoked_by=revoked_by,
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
