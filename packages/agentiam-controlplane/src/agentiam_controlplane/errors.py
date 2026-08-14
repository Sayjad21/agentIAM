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


__all__ = ["ControlPlaneError", "LeaseUnavailableError"]
