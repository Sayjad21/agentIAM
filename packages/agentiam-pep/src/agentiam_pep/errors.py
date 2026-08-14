"""Errors raised by the PEP itself, as opposed to by the correctness core.

Mirrors `agentiam_sdk.errors` and `agentiam_controlplane.errors`: subclasses `AgentIAMError`
so a caller can catch one exception type across packages, and carries a `reason_code`.
"""

from __future__ import annotations

from agentiam_core.errors import AgentIAMError, ReasonCode


class PepError(AgentIAMError):
    """Base for every error originating in the PEP rather than the core."""


class ReservationInsufficientError(PepError):
    """`RESERVE` could not hold `amount` locally — spec 04 §4.2's `Insufficient` outcome.

    One reason code covers all three causes named in the spec — the lease left `active`,
    the caller is inside the clock-skew margin (`now >= expires_at - S`, spec 04 §9), or
    `remaining_local` is short — because the spec itself returns the same `Insufficient`
    for all three; the caller decides what to do next (trigger an async top-up, escalate,
    or simply deny), not this exception.
    """

    reason_code = ReasonCode.LEASE_UNAVAILABLE


__all__ = ["PepError", "ReservationInsufficientError"]
