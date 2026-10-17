"""The `/v1/escalations` JSON API — T-037, `PLAN.md` §8.

`PLAN.md`'s sketch of these four routes lists only the fields a UI form would show
(`decision_id, requested_scope, requested_amount, reason` for the open call; `reason` alone
for a denial). Actually minting and persisting needs more than that — `task_id`, `agent_id`,
`principal_id` and `intent_hash` are what `Mandate` requires (spec 01 §4) and nothing upstream
of this router can supply them on the caller's behalf. `POST /v1/escalations`'s body is
extended to carry them. Recorded in `docs/DECISIONS.md`.

`POST .../approve` and `.../deny` do **not** take `approver` in the body (ADR-041 point 2's
stopgap, superseded by ADR-046/T-043): the acting identity is `agentiam_controlplane.auth
.require_session_principal` — a real OIDC session's `kc:<sub>`, or a 401. A body-supplied
approver name was never authenticated; nothing about that stopgap was worth keeping once a
real session exists.

No FastAPI app in this package has touched a database before T-037 (`app.py`'s
`DummyBundleStore` is deliberately in-memory). `build_router` takes the session factory and
settings explicitly rather than reading them from app state, so `create_app()` can keep
building the console without a database when neither is supplied — the same "wired means it
does the thing, unwired means it visibly doesn't" shape as the PEP's `enforcing` flag.

**Delivery** (APICTA-03). The approval used to hand the elevated token to the *approver*, minted
under a fresh mandate, and nothing reached the agent. Now the token is minted under the mandate
the PEP recorded, re-attenuated along the requesting agent's own delegation path so the PEP
resolves the same principal, and stored with the approval. The agent collects it from
`GET /v1/escalations/{id}/status`, authenticating with the token it was refused under; any
other caller — another agent of the same task included — gets a 404, so ids cannot be probed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentiam_controlplane.auth import require_session_principal
from agentiam_controlplane.db import escalations as store
from agentiam_controlplane.db.revocations import any_revoked
from agentiam_controlplane.errors import DuplicateEscalationError, EscalationNotFoundError
from agentiam_core.attenuation import attenuate
from agentiam_core.datalog import chain_identity
from agentiam_core.errors import TokenError, TokenTooLargeError
from agentiam_core.escalation import (
    ApproverNotAuthorized,
    ElevationGrant,
    Escalation,
    EscalationExpired,
    EscalationNotPending,
    EscalationState,
    NarrowingWidensRequest,
    belongs_to,
)
from agentiam_core.models import Budget, Mandate
from agentiam_core.tokens import RootKeySet, mint_root, verify

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
    """`POST /v1/escalations/{id}/approve`.

    No `approver` field (ADR-046/T-043) — the acting identity comes from the caller's
    session, not from anything the request body claims.
    """

    elevation_ttl_s: float = 300.0
    narrowed_scopes: list[str] | None = None
    max_amount: Decimal | None = Field(default=None, ge=Decimal(0))


class DenyRequest(BaseModel):
    """`POST /v1/escalations/{id}/deny`. No `approver` field — see `ApproveRequest`."""

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
        if isinstance(exc, (EscalationNotPending, DuplicateEscalationError)):
            return 409
        if isinstance(exc, (NarrowingWidensRequest, ValueError, TokenTooLargeError)):
            return 400
        return 500


#: The declared role on blocks the control plane re-mints along a requester's path. Display
#: only (spec 01 §6.1): it tells the console and the audit trail that these blocks were
#: written on approval, not by the agents they name. Cedar's role comes from configuration.
ELEVATED_ROLE = "elevated"


def mint_elevated(grant: ElevationGrant, settings: ControlPlaneSettings) -> str:
    """The elevated token for `grant`, positioned where the requesting agent stands.

    Minted under the grant's own mandate when the escalation recorded one, so it spends from
    the pool the task already holds, and attenuated once per segment of the recorded path so
    the PEP reads back the same delegation path — the key its role assignment uses (ADR-057).
    Without the path the token is a bare root, a different principal, and the corpus policy
    refuses it at any amount on a critical tool (measured on the demo stack).

    `max_depth` is the path's own length: the token sits at the requester's depth and cannot
    be delegated further. A root-level requester's token keeps T-037's floor of
    `elevation_max_depth`, because a mandate cannot have a depth limit of zero.

    Raises:
        TokenTooLargeError, AttenuationError: The token could not be built within limits.
    """
    path = grant.agent_path or ()
    mandate = Mandate(
        mandate_id=grant.mandate_id or uuid.uuid4(),
        task_id=grant.task_id,
        principal_id=grant.principal_id,
        intent_hash=grant.intent_hash,
        scopes=grant.scopes,
        budget=Budget(spend_bdt=grant.amount),
        max_depth=max(len(path), settings.elevation_max_depth),
        not_before=grant.not_before,
        expires_at=grant.expires_at,
    )
    token = mint_root(mandate, settings.root_private_key)
    if not path:
        return token
    if settings.root_public_key is None:
        raise ValueError(
            "re-minting along a delegation path needs the root public key "
            "(AGENTIAM_CONTROLPLANE_ROOT_PUBLIC_KEY)"
        )
    key_set = RootKeySet([settings.root_public_key])
    for segment in path:
        parent = verify(token, key_set, now=grant.not_before)
        token = attenuate(parent, [], agent_id=segment, role=ELEVATED_ROLE)
    return token


def _error_response(exc: Exception) -> JSONResponse:
    body: dict[str, object] = {"detail": str(exc)}
    if isinstance(exc, DuplicateEscalationError):
        # The conflicting row is the caller's next action — `GET /v1/escalations` to read
        # it, or approve/deny it — so it travels as a field rather than only inside prose
        # the client would have to parse.
        body["existing_escalation_id"] = str(exc.existing_id)
        body["decision_id"] = str(exc.decision_id)
    return JSONResponse(body, status_code=_ErrorMap.status_for(exc))


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
            except (DuplicateEscalationError, ValueError) as exc:
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
    async def approve_escalation(
        escalation_id: uuid.UUID,
        body: ApproveRequest,
        approver: str = Depends(require_session_principal),
    ) -> JSONResponse:
        instant = now()
        async with session_factory() as session:
            try:
                _resolved, _grant, delivery = await store.resolve_approve(
                    session,
                    escalation_id,
                    approver=approver,
                    authorized=settings.approvers,
                    now=instant,
                    elevation_ttl=_seconds(body.elevation_ttl_s),
                    narrowed_scopes=(
                        frozenset(body.narrowed_scopes)
                        if body.narrowed_scopes is not None
                        else None
                    ),
                    max_amount=body.max_amount,
                    mint=lambda grant: mint_elevated(grant, settings),
                )
            except (
                EscalationNotFoundError,
                ApproverNotAuthorized,
                EscalationExpired,
                EscalationNotPending,
                NarrowingWidensRequest,
                ValueError,
                TokenTooLargeError,
            ) as exc:
                return _error_response(exc)
        assert delivery is not None  # noqa: S101 — `mint` was passed, so one was built
        return JSONResponse(delivery.to_json())

    @router.get("/{escalation_id}/status")
    async def escalation_status(
        escalation_id: uuid.UUID,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """The requesting agent's view of its escalation, and its elevated token once approved.

        Authenticated by the agent's own token, not a session: the agent is the caller. A
        missing, unverifiable or revoked token is a 401. An escalation that does not exist and
        one that belongs to someone else are the same 404, so a caller holding a valid token
        for one task learns nothing about another's escalation ids. The revocation check is
        what stops a killed agent collecting authority it can then use: the elevated token is
        its own root and is not revoked with the chain it was requested from (TM-28).
        """
        if settings.root_public_key is None:
            return JSONResponse(
                {"detail": "the control plane has no root public key to verify agents with"},
                status_code=503,
            )
        if authorization is None or not authorization.lower().startswith("bearer "):
            return JSONResponse({"detail": "a bearer token is required"}, status_code=401)
        instant = now()
        try:
            token = verify(
                authorization[7:].strip(), RootKeySet([settings.root_public_key]), now=instant
            )
        except TokenError as exc:
            return JSONResponse({"detail": f"token refused: {exc}"}, status_code=401)
        async with session_factory() as session:
            if await any_revoked(session, token.revocation_ids):
                return JSONResponse({"detail": "token revoked"}, status_code=401)
            found = await store.get_delivery(session, escalation_id)
        not_found = JSONResponse({"detail": f"no escalation {escalation_id}"}, status_code=404)
        if found is None:
            return not_found
        escalation, delivery = found
        identity = chain_identity(token)
        if not belongs_to(
            escalation,
            task_id=token.task_id,
            mandate_id=token.mandate_id,
            agent_path=identity.path,
            agent_id=identity.terminal.agent_id or f"agt-depth-{token.depth}",
        ):
            return not_found
        state = escalation.state_at(instant)
        body: dict[str, object] = {"id": str(escalation.id), "state": state.value}
        if delivery is not None:
            body |= delivery.to_json()
        else:
            body |= {
                "scopes": sorted(escalation.requested_scopes),
                "amount": str(escalation.requested_amount),
                "expires_at": escalation.expires_at.isoformat(),
            }
        if state is EscalationState.DENIED:
            body["reason"] = escalation.resolution_reason
        return JSONResponse(body)

    @router.post("/{escalation_id}/deny")
    async def deny_escalation(
        escalation_id: uuid.UUID,
        body: DenyRequest,
        approver: str = Depends(require_session_principal),
    ) -> JSONResponse:
        instant = now()
        async with session_factory() as session:
            try:
                resolved = await store.resolve_deny(
                    session,
                    escalation_id,
                    approver=approver,
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
