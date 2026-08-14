"""Errors raised by the control plane itself, as opposed to by the correctness core.

Mirrors `agentiam_sdk.errors`: each subclasses `AgentIAMError` so a caller can catch one
exception type across packages, and each carries a `reason_code` where a reason code is
meaningful (P-18).
"""

from __future__ import annotations

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


__all__ = ["ControlPlaneError", "LeaseNotActiveError", "LeaseUnavailableError"]
