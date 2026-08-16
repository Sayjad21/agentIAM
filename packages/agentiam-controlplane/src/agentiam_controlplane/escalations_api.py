"""The `/v1/escalations` JSON API — T-037, `PLAN.md` §8.

`PLAN.md`'s sketch of these four routes lists only the fields a UI form would show
(`decision_id, requested_scope, requested_amount, reason` for the open call; `reason` alone
for a denial). Actually minting and persisting needs more than that — `task_id`, `agent_id`,
`principal_id` and `intent_hash` are what `Mandate` requires (spec 01 §4) and nothing upstream
of this router can supply them on the caller's behalf. `POST /v1/escalations`'s body is
extended to carry them; `POST .../approve` and `.../deny` both add `approver`, because no
session identity exists yet (T-043) and the request has to name who is acting. Recorded in
`docs/DECISIONS.md`.

No FastAPI app in this package has touched a database before this ticket (`app.py`'s
`DummyBundleStore` is deliberately in-memory). `build_router` takes the session factory and
settings explicitly rather than reading them from app state, so `create_app()` can keep
building the console without a database when neither is supplied — the same "wired means it
does the thing, unwired means it visibly doesn't" shape as the PEP's `enforcing` flag.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentiam_controlplane.db import escalations as store
from agentiam_controlplane.errors import EscalationNotFoundError
from agentiam_core.errors import TokenTooLargeError
from agentiam_core.escalation import (
    ApproverNotAuthorized,
    Escalation,
    EscalationExpired,
    EscalationNotPending,
    EscalationState,
    NarrowingWidensRequest,
)
from agentiam_core.models import Budget, Mandate
from agentiam_core.tokens import mint_root

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from agentiam_controlplane.settings import ControlPlaneSettings

__all__ = ["build_router"]


def _serialize(escalation: Escalation, *, now: datetime) -> dict[str, object]:
    return {
        "id": str(escalation.id),
        "decision_id": str(escalation.decision_id),
        "task_id": str(escalation.task_id),
        "agent_id": escalation.agent_id,
        "principal_id": escalation.principal_id,
        "requested_scopes": sorted(escalation.requested_scopes),
        "requested_amount": str(escalation.requested_amount),
        "reason": escalation.reason,
        "created_at": escalation.created_at.isoformat(),
        "expires_at": escalation.expires_at.isoformat(),
        "state": escalation.state_at(now).value,
        "resolved_by": escalation.resolved_by,
        "resolution_reason": escalation.resolution_reason,
    }


class OpenEscalationRequest(BaseModel):
    """`POST /v1/escalations` — opening one directly, rather than via a denied decision."""

    decision_id: uuid.UUID
    task_id: uuid.UUID
    agent_id: str
    principal_id: str
    intent_hash: str
    requested_scopes: list[str] = Field(min_length=1)
    requested_amount: Decimal = Field(ge=Decimal(0))
    reason: str
    ttl_s: float = 900.0


class ApproveRequest(BaseModel):
    """`POST /v1/escalations/{id}/approve`."""

    approver: str
    elevation_ttl_s: float = 300.0
    narrowed_scopes: list[str] | None = None
    max_amount: Decimal | None = Field(default=None, ge=Decimal(0))


class DenyRequest(BaseModel):
    """`POST /v1/escalations/{id}/deny`."""

    approver: str
    reason: str


@dataclass(frozen=True, slots=True)
class _ErrorMap:
    """Where a core/store error lands as an HTTP status — mirrors spec 09 §11's shape."""

    @staticmethod
    def status_for(exc: Exception) -> int:
        if isinstance(exc, EscalationNotFoundError):
            return 404
        if isinstance(exc, ApproverNotAuthorized):
            return 403
        if isinstance(exc, EscalationExpired):
            return 410
        if isinstance(exc, EscalationNotPending):
            return 409
        if isinstance(exc, (NarrowingWidensRequest, ValueError, TokenTooLargeError)):
            return 400
        return 500


def _error_response(exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=_ErrorMap.status_for(exc))


def build_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: ControlPlaneSettings,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Build the four escalation routes, bound to `session_factory` and `settings`."""
    router = APIRouter(prefix="/v1/escalations", tags=["escalations"])

    @router.post("", status_code=201)
    async def open_escalation(body: OpenEscalationRequest) -> JSONResponse:
        async with session_factory() as session:
            try:
                escalation = await store.create(
                    session,
                    decision_id=body.decision_id,
                    task_id=body.task_id,
                    agent_id=body.agent_id,
                    principal_id=body.principal_id,
                    intent_hash=body.intent_hash,
                    requested_scopes=frozenset(body.requested_scopes),
                    requested_amount=body.requested_amount,
                    reason=body.reason,
                    now=now(),
                    ttl=_seconds(body.ttl_s),
                )
            except ValueError as exc:
                return _error_response(exc)
        return JSONResponse(_serialize(escalation, now=now()), status_code=201)

    @router.get("")
    async def list_escalations(
        state: str = Query(default="pending"),
    ) -> JSONResponse:
        try:
            parsed_state = EscalationState(state)
        except ValueError:
            valid = ", ".join(s.value for s in EscalationState)
            return JSONResponse({"detail": f"state must be one of: {valid}"}, status_code=400)
        instant = now()
        async with session_factory() as session:
            found = await store.list_by_state(session, state=parsed_state, now=instant)
        return JSONResponse([_serialize(e, now=instant) for e in found])

    @router.post("/{escalation_id}/approve")
    async def approve_escalation(escalation_id: uuid.UUID, body: ApproveRequest) -> JSONResponse:
        instant = now()
        async with session_factory() as session:
            try:
                _resolved, grant = await store.resolve_approve(
                    session,
                    escalation_id,
                    approver=body.approver,
                    authorized=settings.approvers,
                    now=instant,
                    elevation_ttl=_seconds(body.elevation_ttl_s),
                    narrowed_scopes=(
                        frozenset(body.narrowed_scopes)
                        if body.narrowed_scopes is not None
                        else None
                    ),
                    max_amount=body.max_amount,
                )
            except (
                EscalationNotFoundError,
                ApproverNotAuthorized,
                EscalationExpired,
                EscalationNotPending,
                NarrowingWidensRequest,
                ValueError,
            ) as exc:
                return _error_response(exc)

        mandate = Mandate(
            mandate_id=uuid.uuid4(),
            task_id=grant.task_id,
            principal_id=grant.principal_id,
            intent_hash=grant.intent_hash,
            scopes=grant.scopes,
            budget=Budget(spend_bdt=grant.amount),
            max_depth=settings.elevation_max_depth,
            not_before=grant.not_before,
            expires_at=grant.expires_at,
        )
        try:
            elevated_token = mint_root(mandate, settings.root_private_key)
        except TokenTooLargeError as exc:
            return _error_response(exc)

        return JSONResponse(
            {
                "elevated_token": elevated_token,
                "expires_at": grant.expires_at.isoformat(),
                "scopes": sorted(grant.scopes),
                "amount": str(grant.amount),
            }
        )

    @router.post("/{escalation_id}/deny")
    async def deny_escalation(escalation_id: uuid.UUID, body: DenyRequest) -> JSONResponse:
        instant = now()
        async with session_factory() as session:
            try:
                resolved = await store.resolve_deny(
                    session,
                    escalation_id,
                    approver=body.approver,
                    authorized=settings.approvers,
                    now=instant,
                    reason=body.reason,
                )
            except (
                EscalationNotFoundError,
                ApproverNotAuthorized,
                EscalationExpired,
                EscalationNotPending,
                ValueError,
            ) as exc:
                return _error_response(exc)
        return JSONResponse(_serialize(resolved, now=instant))

    return router


def _seconds(value: float) -> timedelta:
    """`float` seconds to `timedelta`, rejecting non-finite input with a plain `ValueError`."""
    try:
        return timedelta(seconds=value)
    except OverflowError as exc:
        raise ValueError(f"ttl_s must be a finite number of seconds, got {value!r}") from exc
