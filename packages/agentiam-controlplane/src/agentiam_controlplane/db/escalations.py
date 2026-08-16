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
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentiam_controlplane.db.models import EscalationRow
from agentiam_controlplane.errors import EscalationNotFoundError
from agentiam_core.escalation import (
    Escalation,
    EscalationState,
    approve,
    deny,
    request_escalation,
)

if TYPE_CHECKING:
    from agentiam_core.escalation import ElevationGrant

__all__ = [
    "create",
    "get",
    "list_by_state",
    "resolve_approve",
    "resolve_deny",
]


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
) -> Escalation:
    """Open a pending escalation and persist it.

    Validation (non-empty scopes, non-negative amount) happens in `request_escalation`
    before anything is written, so a rejected request leaves no row behind.
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
    )
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
            )
        )
    return escalation


async def get(session: AsyncSession, escalation_id: uuid.UUID) -> Escalation | None:
    """Fetch one escalation, or `None` if no such id exists."""
    row = await session.get(EscalationRow, escalation_id)
    return _to_domain(row) if row is not None else None


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
) -> tuple[Escalation, ElevationGrant]:
    """Approve under a row lock, so two racing approvers cannot both win (EC-A10).

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
        row.state = resolved.state.value
        row.resolved_by = resolved.resolved_by
        row.resolved_at = resolved.resolved_at
    return resolved, grant


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
