"""Errors raised by the control plane itself, as opposed to by the correctness core.

Mirrors `agentiam_sdk.errors`: each subclasses `AgentIAMError` so a caller can catch one
exception type across packages, and each carries a `reason_code` where a reason code is
meaningful (P-18).
"""

from __future__ import annotations

import uuid

from agentiam_core.errors import AgentIAMError, ReasonCode


class ControlPlaneError(AgentIAMError):
    """Base for every error originating in the control plane rather than the core."""


class LeaseUnavailableError(ControlPlaneError):
    """`ACQUIRE` could not grant any amount — spec 04 §4.1's `Insufficient` outcome.

    Raised rather than returned so the pool-exhaustion path reads the same as every other
    deny path in the codebase (rule 5): one exception type, one reason code.
    """

    reason_code = ReasonCode.LEASE_UNAVAILABLE


class LeaseNotActiveError(ControlPlaneError):
    """`LEDGER_COMMIT` was called against a lease that already left `active` — spec 04 §4.4, §11.

    A distinct code from `LEASE_UNAVAILABLE`: that one means the pool had nothing left to
    grant at `ACQUIRE` time, this one means a settlement arrived for a lease the ledger has
    already released, expired, or revoked (ADR-009). The commit is rejected and recorded as
    a reconciliation anomaly rather than silently corrupting `leased` (spec 04 §11, TM-21).
    """

    reason_code = ReasonCode.LEASE_NOT_ACTIVE


class AllocationError(ControlPlaneError):
    """A proportional split would hand out more than the parent has — spec 04 §13, INV-5.

    The *static* half of INV-5. Where `LeaseUnavailableError` means the pool had nothing
    left to lease right now, this means the division itself does not add up: the parent
    cannot promise its children more than `total - committed - leased - allocated`.

    Checked under the parent's row lock and raised before any child row is created, so a
    refused split leaves nothing behind.
    """

    reason_code = ReasonCode.BUDGET_EXHAUSTED_MANDATE


class EscalationNotFoundError(ControlPlaneError):
    """No escalation exists with the given id — T-037. Maps to HTTP 404.

    Not part of the closed reason-code enum (rule 5): that set names why a *decision*
    denied a request, and a 404 on the admin API is neither a decision nor a denial.
    """


class DuplicateEscalationError(ControlPlaneError):
    """An escalation already exists for this decision — T-037. Maps to HTTP 409.

    `escalations.decision_id` is unique: one decision raises at most one escalation, so a
    second `POST /v1/escalations` for the same decision is a *state* conflict rather than a
    malformed request. Same reading as `app._refuse_activation`'s 409 (ADR-030, spec 05
    §5.5) and as EC-A10's losing approver — well-formed, permitted, and refused because the
    row is already in a state that excludes it.

    Deliberately **not** absorbed the way a repeat revoke is (spec 07 §9, EC-R05). A repeat
    revoke asks for a state the table already holds, so returning the existing row answers
    the caller's question truthfully. A second escalation may name different scopes, a
    different amount and a different reason; returning the first one as though it were this
    request would report a grant nobody asked for. So it errors, and names the escalation
    that is in the way.

    Not part of the closed reason-code enum (rule 5), for the same reason as
    `EscalationNotFoundError`: this is an admin-API conflict, not a decision.
    """

    def __init__(self, message: str, *, existing_id: uuid.UUID, decision_id: uuid.UUID) -> None:
        """Carry the conflicting escalation's id, so the caller can act on it."""
        super().__init__(message)
        self.existing_id = existing_id
        self.decision_id = decision_id


__all__ = [
    "AllocationError",
    "ControlPlaneError",
    "DuplicateEscalationError",
    "EscalationNotFoundError",
    "LeaseNotActiveError",
    "LeaseUnavailableError",
]
