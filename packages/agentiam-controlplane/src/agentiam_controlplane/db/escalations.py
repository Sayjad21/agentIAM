"""Persistence for the escalation queue — T-037, `PLAN.md` §11.7.

`agentiam_core.escalation` holds every rule that carries security weight: the grant is a
subset of the request, an escalation resolves exactly once, expiry is a state rather than a
sweeper. This module is only the storage adapter — read a row, hand it to the pure functions,
write back what they decided.

**Exactly-once (EC-A10) is enforced here, not there.** The core module's own docstring says a
persisted implementation needs an `UPDATE ... WHERE state = 'pending'`; the equivalent used
here is a `SELECT ... FOR UPDATE` on the row before `approve()`/`deny()` runs, matching
`db/ledger.py`'s pattern for the same problem. The second of two racing callers blocks on the
lock, then sees the first caller's write and gets `EscalationNotPending` from the pure check —
the lock only has to make the read-then-decide atomic; the "already resolved" logic still
lives entirely in `agentiam_core.escalation`.

**The grant is written in the approving transaction** (APICTA-03). `resolve_approve` takes the
minter as a callable and runs it under the row lock, so a mint that fails — a token over the
size limit — rolls the approval back rather than committing an approval whose token the
waiting agent can never collect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentiam_controlplane.db.models import EscalationRow
from agentiam_controlplane.errors import DuplicateEscalationError, EscalationNotFoundError
from agentiam_core.escalation import (
    Escalation,
    EscalationState,
    approve,
    deny,
    request_escalation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentiam_core.escalation import ElevationGrant

__all__ = [
    "Delivery",
    "create",
    "get",
    "get_delivery",
    "list_by_state",
    "resolve_approve",
    "resolve_deny",
]


@dataclass(frozen=True, slots=True)
class Delivery:
    """The elevated token as stored with an approval, and what it grants."""

    elevated_token: str
    expires_at: datetime
    scopes: frozenset[str]
    amount: Decimal

    def to_json(self) -> dict[str, object]:
        """The `grant` column's shape, which is also the approve route's response body."""
        return {
            "elevated_token": self.elevated_token,
            "expires_at": self.expires_at.isoformat(),
            "scopes": sorted(self.scopes),
            "amount": str(self.amount),
        }

    @classmethod
    def from_json(cls, stored: dict[str, Any]) -> Delivery:
        """Read back what `to_json` wrote."""
        return cls(
            elevated_token=str(stored["elevated_token"]),
            expires_at=datetime.fromisoformat(str(stored["expires_at"])),
            scopes=frozenset(str(s) for s in stored["scopes"]),
            amount=Decimal(str(stored["amount"])),
        )


def _to_domain(row: EscalationRow) -> Escalation:
    return Escalation(
        id=row.id,
        decision_id=row.decision_id,
        task_id=row.task_id,
        agent_id=row.agent_id,
        principal_id=row.principal_id,
        intent_hash=row.intent_hash,
        requested_scopes=frozenset(row.requested_scopes),
        requested_amount=row.requested_amount,
        reason=row.reason,
        created_at=row.created_at,
        expires_at=row.expires_at,
        state=EscalationState(row.state),
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
        resolution_reason=row.resolution_reason,
        mandate_id=row.mandate_id,
        agent_path=tuple(row.agent_path) if row.agent_path is not None else None,
    )


async def create(
    session: AsyncSession,
    *,
    decision_id: uuid.UUID,
    task_id: uuid.UUID,
    agent_id: str,
    principal_id: str,
    intent_hash: str,
    requested_scopes: frozenset[str],
    requested_amount: Decimal,
    reason: str,
    now: datetime,
    ttl: timedelta,
    mandate_id: uuid.UUID | None = None,
    agent_path: tuple[str, ...] | None = None,
) -> Escalation:
    """Open a pending escalation and persist it.

    Validation (non-empty scopes, non-negative amount) happens in `request_escalation`
    before anything is written, so a rejected request leaves no row behind.

    Raises:
        ValueError: The request itself is invalid — see `request_escalation`.
        DuplicateEscalationError: An escalation already exists for `decision_id`.
    """
    escalation = request_escalation(
        decision_id=decision_id,
        task_id=task_id,
        agent_id=agent_id,
        principal_id=principal_id,
        intent_hash=intent_hash,
        requested_scopes=requested_scopes,
        requested_amount=requested_amount,
        reason=reason,
        now=now,
        ttl=ttl,
        mandate_id=mandate_id,
        agent_path=agent_path,
    )
    try:
        async with session.begin():
            session.add(
                EscalationRow(
                    id=escalation.id,
                    decision_id=escalation.decision_id,
                    task_id=escalation.task_id,
                    agent_id=escalation.agent_id,
                    principal_id=escalation.principal_id,
                    intent_hash=escalation.intent_hash,
                    requested_scopes=sorted(escalation.requested_scopes),
                    requested_amount=escalation.requested_amount,
                    reason=escalation.reason,
                    created_at=escalation.created_at,
                    expires_at=escalation.expires_at,
                    state=escalation.state.value,
                    mandate_id=escalation.mandate_id,
                    agent_path=(
                        list(escalation.agent_path) if escalation.agent_path is not None else None
                    ),
                )
            )
    except IntegrityError as exc:
        existing = await _by_decision(session, decision_id)
        if existing is None:
            raise
        raise DuplicateEscalationError(
            f"decision {decision_id} already raised escalation {existing.id}, "
            f"opened {existing.created_at.isoformat()} and now "
            f"{existing.state_at(now).value}",
            existing_id=existing.id,
            decision_id=decision_id,
        ) from exc
    return escalation


async def _by_decision(session: AsyncSession, decision_id: uuid.UUID) -> Escalation | None:
    """The escalation raised by `decision_id`, or `None`.

    Only reached after a failed insert, so the extra round trip costs nothing on the path
    that succeeds. Which constraint the database rejected is inferred from *what is there*
    rather than from parsing the driver's message: a row for this `decision_id` means
    `uq_escalations_decision_id`, and anything else re-raises unchanged rather than being
    reported as a duplicate it is not.

    `session.begin()` above has already rolled back by the time this runs, so the session
    is usable again for a fresh read.
    """
    row = (
        await session.execute(select(EscalationRow).where(EscalationRow.decision_id == decision_id))
    ).scalar_one_or_none()
    return _to_domain(row) if row is not None else None


async def get(session: AsyncSession, escalation_id: uuid.UUID) -> Escalation | None:
    """Fetch one escalation, or `None` if no such id exists."""
    row = await session.get(EscalationRow, escalation_id)
    return _to_domain(row) if row is not None else None


async def get_delivery(
    session: AsyncSession, escalation_id: uuid.UUID
) -> tuple[Escalation, Delivery | None] | None:
    """Fetch one escalation with its stored grant, or `None` if no such id exists.

    The grant is `None` until the escalation is approved, and stays `None` for one approved
    without a minter.
    """
    row = await session.get(EscalationRow, escalation_id)
    if row is None:
        return None
    return _to_domain(row), Delivery.from_json(row.grant) if row.grant is not None else None


async def list_by_state(
    session: AsyncSession, *, state: EscalationState, now: datetime
) -> list[Escalation]:
    """List escalations reporting `state` as of `now`.

    For `PENDING`, a row past its `expires_at` is excluded even though nothing has swept its
    stored `state` column yet — `state_at()` is what "TTL expiry auto-denies" means, and a
    queue that still showed an expired request as actionable would contradict it. Every other
    state is a plain column match: `APPROVED`/`DENIED` are terminal and never age out.
    """
    query = select(EscalationRow)
    if state is EscalationState.PENDING:
        query = query.where(EscalationRow.state == state.value, EscalationRow.expires_at > now)
    else:
        query = query.where(EscalationRow.state == state.value)
    query = query.order_by(EscalationRow.created_at)
    rows = (await session.execute(query)).scalars().all()
    return [_to_domain(row) for row in rows]


async def _load_for_update(session: AsyncSession, escalation_id: uuid.UUID) -> EscalationRow:
    row = (
        await session.execute(
            select(EscalationRow).where(EscalationRow.id == escalation_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise EscalationNotFoundError(f"no escalation {escalation_id}")
    return row


async def resolve_approve(
    session: AsyncSession,
    escalation_id: uuid.UUID,
    *,
    approver: str,
    authorized: frozenset[str],
    now: datetime,
    elevation_ttl: timedelta,
    narrowed_scopes: frozenset[str] | None = None,
    max_amount: Decimal | None = None,
    mint: Callable[[ElevationGrant], str] | None = None,
) -> tuple[Escalation, ElevationGrant, Delivery | None]:
    """Approve under a row lock, so two racing approvers cannot both win (EC-A10).

    `mint` turns the grant into the elevated token. It runs under the same lock and in the
    same transaction as the state change, and whatever it raises propagates and rolls the
    approval back — an approved row always carries its token.

    Raises:
        EscalationNotFoundError: No such escalation.
        ApproverNotAuthorized, EscalationExpired, EscalationNotPending,
            NarrowingWidensRequest, ValueError: See `agentiam_core.escalation.approve`.
    """
    async with session.begin():
        row = await _load_for_update(session, escalation_id)
        resolved, grant = approve(
            _to_domain(row),
            approver=approver,
            authorized=authorized,
            now=now,
            elevation_ttl=elevation_ttl,
            narrowed_scopes=narrowed_scopes,
            max_amount=max_amount,
        )
        delivery = (
            Delivery(
                elevated_token=mint(grant),
                expires_at=grant.expires_at,
                scopes=grant.scopes,
                amount=grant.amount,
            )
            if mint is not None
            else None
        )
        row.state = resolved.state.value
        row.resolved_by = resolved.resolved_by
        row.resolved_at = resolved.resolved_at
        row.grant = delivery.to_json() if delivery is not None else None
    return resolved, grant, delivery


async def resolve_deny(
    session: AsyncSession,
    escalation_id: uuid.UUID,
    *,
    approver: str,
    authorized: frozenset[str],
    now: datetime,
    reason: str,
) -> Escalation:
    """Deny under a row lock. Same locking rationale as `resolve_approve`.

    Raises:
        EscalationNotFoundError: No such escalation.
        ApproverNotAuthorized, EscalationExpired, EscalationNotPending, ValueError: See
            `agentiam_core.escalation.deny`.
    """
    async with session.begin():
        row = await _load_for_update(session, escalation_id)
        resolved = deny(
            _to_domain(row), approver=approver, authorized=authorized, now=now, reason=reason
        )
        row.state = resolved.state.value
        row.resolved_by = resolved.resolved_by
        row.resolved_at = resolved.resolved_at
        row.resolution_reason = resolved.resolution_reason
    return resolved
