"""Errors raised by the SDK itself, as opposed to by the correctness core.

Each subclasses `AgentIAMError`, so an agent can catch one exception type across both
packages, and each carries a `reason_code` where a reason code is meaningful — so an
error surfacing at the PEP boundary later becomes a decision record without a lookup
table (P-18).

`NoIdentityError` and `IdentityContextError` deliberately carry no reason code: they are
programming errors in the agent, not authorization outcomes. A decision record for
"the developer forgot to activate a token" would be a lie about what happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentiam_core.errors import AgentIAMError, ReasonCode

if TYPE_CHECKING:
    from uuid import UUID


class SDKError(AgentIAMError):
    """Base for every error originating in the SDK rather than the core."""


class NoIdentityError(SDKError):
    """An operation needed the current agent identity and none was active.

    Raised by `current_identity()` and by `@requires_scope`. The fix is to wrap the call
    site in `client.activate()` or `use_identity(...)`.
    """


class IdentityContextError(SDKError):
    """An identity scope was exited from a different context than it was entered in.

    `ContextVar.reset()` refuses a token minted in another context, which is what happens
    if a scope is entered in one asyncio task and exited in another. Measured: CPython
    raises `ValueError: <Token ...> was created in a different Context`. That message
    names neither the SDK nor the mistake, so it is translated here.
    """


class ScopeDeniedError(SDKError):
    """The active identity does not carry a scope the call requires.

    Client-side, advisory, and deliberately *not* the security boundary — see
    `decorators.requires_scope`. It fails fast and locally, before a request leaves the
    process; the PEP is what actually enforces the grant.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: ReasonCode,
        missing_scopes: frozenset[str],
    ) -> None:
        """Record which scopes were missing alongside the reason code."""
        super().__init__(message, reason_code=reason_code)
        self.missing_scopes = missing_scopes


class TokenSizeWarning(UserWarning):
    """A minted chain crossed the 4 KB advisory size limit.

    From T-010's acceptance criteria. T-010 itself is deferred (ADR-006) because the
    *hard* 8 KB limit is unreachable at `max_depth = 8`, but the advisory limit is
    reachable at depth 6 and stays warned about (EC-T11).
    """


class EscalationError(SDKError):
    """An escalation the agent waited on did not end in a grant it could use.

    Raised directly when the control plane answers the status poll with something other
    than a state — a 401 or 404 means this token cannot collect this escalation, and no
    amount of polling will change that.
    """

    def __init__(self, message: str, *, escalation_id: UUID) -> None:
        """Record which escalation this was, so the caller can follow it up elsewhere."""
        super().__init__(message, reason_code=ReasonCode.APPROVAL_REQUIRED)
        self.escalation_id = escalation_id


class EscalationDenied(EscalationError):
    """An approver refused the escalation. The message carries their reason."""


class EscalationExpired(EscalationError):
    """The escalation's own window closed before anyone resolved it."""


class EscalationWaitTimeout(EscalationError):
    """The agent stopped waiting; the escalation itself may still be pending.

    Deliberately not an `EscalationExpired`: nothing about the escalation changed, only this
    caller's patience ran out, and an approval that lands a minute later is still
    collectable with the same token.
    """


__all__ = [
    "EscalationDenied",
    "EscalationError",
    "EscalationExpired",
    "EscalationWaitTimeout",
    "IdentityContextError",
    "NoIdentityError",
    "SDKError",
    "ScopeDeniedError",
    "TokenSizeWarning",
]
