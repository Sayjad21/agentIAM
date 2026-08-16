"""The escalation workflow — T-037, `PLAN.md` §11.7.

When `decide()` returns `Outcome.ESCALATE`, the request stops and a human is asked. This
module is that conversation: the request, the approver's answer, and the grant an approval
produces.

**Elevation mints a fresh mandate; it never touches the existing token.** That is not a
style choice. Elevation *widens* authority, and `attenuate()` narrows by construction — it
is incapable of granting a scope the parent lacks (INV-1). So the only honest mechanism is
a new, short-lived grant signed by the root key, exactly as `PLAN.md` §207 requires. The
happy consequence is that "the original token is unchanged" needs no discipline to hold:
nothing here can reach it.

Three properties carry the security weight:

1. **The grant is always ⊆ the request.** An approver may narrow and may not widen. If
   approval could grant more than was asked for, the request text an operator reads on
   screen would stop being what they are authorising (EC-A09).
2. **An escalation resolves exactly once.** Two approvers racing must not produce two
   elevated tokens (EC-A10). Enforced here on the value; a persisted implementation needs
   the same check in its `UPDATE ... WHERE state = 'pending'`.
3. **Expiry is a state, not a sweeper.** `state_at()` reports `EXPIRED` once the TTL has
   passed, so "TTL expiry auto-denies" holds for anything reading the queue even if no
   background job has run (EC-A07).

Everything is pure and immutable — no clock, no I/O, no mutation. `now` is passed in.

Rule 4: money is `Decimal`, never `float`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "ApproverNotAuthorized",
    "ElevationGrant",
    "Escalation",
    "EscalationError",
    "EscalationExpired",
    "EscalationNotPending",
    "EscalationState",
    "NarrowingWidensRequest",
    "approve",
    "deny",
    "request_escalation",
]


class EscalationState(StrEnum):
    """Where an escalation stands.

    `EXPIRED` is distinct from `DENIED` because the two mean different things to the agent
    and to a later audit: one is a human's decision, the other is a human never arriving.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class EscalationError(Exception):
    """Base for every refusal in this module."""


class EscalationExpired(EscalationError):
    """The request's TTL has passed (EC-A07). Maps to HTTP 410."""


class EscalationNotPending(EscalationError):
    """Already resolved. Maps to HTTP 409 — the second of two racing approvers."""


class ApproverNotAuthorized(EscalationError):
    """The principal may not resolve this escalation (EC-A08).

    Carries the attempted identity, because EC-A08 requires the attempt to be *audited*
    and a generic 403 would throw away the only fact worth recording.
    """

    def __init__(self, approver: str, detail: str) -> None:
        """Record who tried, and why it was refused."""
        super().__init__(detail)
        self.approver = approver


class NarrowingWidensRequest(EscalationError):
    """An approver tried to grant more than was requested (EC-A09). Maps to HTTP 400."""


@dataclass(frozen=True, slots=True)
class ElevationGrant:
    """What an approval authorises — the input to a fresh short-lived mandate.

    Deliberately *not* a token. Minting needs the root signing key, which belongs to the
    issuer, while the rules about what may be granted belong here where they can be tested
    without one.
    """

    escalation_id: uuid.UUID
    task_id: uuid.UUID
    principal_id: str
    agent_id: str
    intent_hash: str
    scopes: frozenset[str]
    amount: Decimal
    not_before: datetime
    expires_at: datetime
    approved_by: str


@dataclass(frozen=True, slots=True)
class Escalation:
    """One request for human authority, and its resolution."""

    id: uuid.UUID
    decision_id: uuid.UUID
    task_id: uuid.UUID
    agent_id: str
    principal_id: str
    intent_hash: str
    requested_scopes: frozenset[str]
    requested_amount: Decimal
    reason: str
    created_at: datetime
    expires_at: datetime
    state: EscalationState = EscalationState.PENDING
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_reason: str | None = None

    def is_expired(self, now: datetime) -> bool:
        """Whether the request's window has closed.

        Inclusive at the boundary: at exactly `expires_at` the window is over. "Expires at
        12:15" is ambiguous in English and must not be in code.
        """
        return now >= self.expires_at

    def state_at(self, now: datetime) -> EscalationState:
        """The state as of `now`, accounting for expiry.

        A still-`PENDING` request past its TTL reports `EXPIRED`. This is what makes "TTL
        expiry auto-denies" true for any reader without a sweeper having run — and it
        means a sweeper, when one exists, only has to persist what is already true.
        """
        if self.state is EscalationState.PENDING and self.is_expired(now):
            return EscalationState.EXPIRED
        return self.state


def request_escalation(
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
    """Open a pending escalation.

    Raises:
        ValueError: No scopes were requested, or the amount is negative. An escalation
            that asks for nothing cannot be approved into anything, and would sit in the
            queue looking like work.
    """
    if not requested_scopes:
        raise ValueError("an escalation must request at least one scope")
    if requested_amount < 0:
        raise ValueError("requested_amount cannot be negative")

    return Escalation(
        id=uuid.uuid4(),
        decision_id=decision_id,
        task_id=task_id,
        agent_id=agent_id,
        principal_id=principal_id,
        intent_hash=intent_hash,
        requested_scopes=requested_scopes,
        requested_amount=requested_amount,
        reason=reason,
        created_at=now,
        expires_at=now + ttl,
    )


def approve(
    escalation: Escalation,
    *,
    approver: str,
    authorized: frozenset[str],
    now: datetime,
    elevation_ttl: timedelta,
    narrowed_scopes: frozenset[str] | None = None,
    max_amount: Decimal | None = None,
) -> tuple[Escalation, ElevationGrant]:
    """Approve an escalation, optionally narrowing it.

    Returns:
        The resolved escalation and the grant to mint from. The input is untouched.

    Raises:
        ApproverNotAuthorized: Not an approver, or the agent's own principal (EC-A08).
        EscalationExpired: The TTL has passed (EC-A07).
        EscalationNotPending: Already resolved (EC-A10).
        NarrowingWidensRequest: The narrowing grants more than was requested (EC-A09).
        ValueError: The narrowing leaves no scopes at all.
    """
    _check_resolvable(escalation, approver=approver, authorized=authorized, now=now)

    scopes = escalation.requested_scopes if narrowed_scopes is None else narrowed_scopes
    if not scopes:
        raise ValueError(
            "an approval must grant at least one scope; deny it instead so the agent "
            "receives a reason rather than a token that permits nothing"
        )

    # ⊆ the request, in both dimensions. This is the property that makes the approval
    # screen mean what it says.
    widened = scopes - escalation.requested_scopes
    if widened:
        raise NarrowingWidensRequest(
            f"approval would grant {sorted(widened)}, which was not requested"
        )

    amount = escalation.requested_amount if max_amount is None else max_amount
    if amount > escalation.requested_amount:
        raise NarrowingWidensRequest(
            f"approval would grant amount {amount}, above the requested "
            f"{escalation.requested_amount}"
        )

    resolved = replace(
        escalation,
        state=EscalationState.APPROVED,
        resolved_by=approver,
        resolved_at=now,
    )
    grant = ElevationGrant(
        escalation_id=escalation.id,
        task_id=escalation.task_id,
        principal_id=escalation.principal_id,
        agent_id=escalation.agent_id,
        intent_hash=escalation.intent_hash,
        scopes=scopes,
        amount=amount,
        not_before=now,
        expires_at=now + elevation_ttl,
        approved_by=approver,
    )
    return resolved, grant


def deny(
    escalation: Escalation,
    *,
    approver: str,
    authorized: frozenset[str],
    now: datetime,
    reason: str,
) -> Escalation:
    """Deny an escalation, with a reason the agent receives.

    Raises:
        ApproverNotAuthorized: Not an approver, or the agent's own principal.
        EscalationExpired: The TTL has passed.
        EscalationNotPending: Already resolved.
        ValueError: No reason was given.
    """
    _check_resolvable(escalation, approver=approver, authorized=authorized, now=now)
    if not reason.strip():
        raise ValueError(
            "a denial needs a reason: without one the agent has nothing to act on and "
            "the operator has nothing to defend later"
        )

    return replace(
        escalation,
        state=EscalationState.DENIED,
        resolved_by=approver,
        resolved_at=now,
        resolution_reason=reason,
    )


def _check_resolvable(
    escalation: Escalation, *, approver: str, authorized: frozenset[str], now: datetime
) -> None:
    """The three gates every resolution passes, in the order that leaks least.

    Authorization is checked *first*, so an unauthorized caller learns nothing about
    whether the escalation exists, has expired, or was already decided.
    """
    if approver not in authorized:
        raise ApproverNotAuthorized(approver, f"{approver!r} is not an authorized approver")
    if approver == escalation.principal_id:
        # Self-approval would make the human-in-the-loop decorative — checked even when
        # the principal is otherwise a legitimate approver.
        raise ApproverNotAuthorized(
            approver, f"{approver!r} cannot approve their own agent's escalation"
        )
    if escalation.state is not EscalationState.PENDING:
        raise EscalationNotPending(
            f"escalation {escalation.id} is already {escalation.state.value}"
        )
    if escalation.is_expired(now):
        raise EscalationExpired(
            f"escalation {escalation.id} expired at {escalation.expires_at.isoformat()}"
        )
