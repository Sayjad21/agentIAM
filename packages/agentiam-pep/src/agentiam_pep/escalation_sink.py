"""The `EscalationSink` that connects an `ESCALATE` decision to the escalation queue — T-037.

Mirrors `emitter.py`'s `RecordSink`: the pipeline is deliberately ignorant of where a pending
escalation is persisted, so it depends on this `Protocol` rather than on a database session,
and `agentiam_controlplane` supplies the implementation that actually owns one.

A failed write must **raise** rather than swallow its own error. `Pipeline.authorize()` fails
the request closed when this happens (ADR-026's reasoning applied here: a system that cannot
record *that a human was asked* must not tell the agent one was asked).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime, timedelta
    from decimal import Decimal
    from uuid import UUID

    from agentiam_core.escalation import Escalation

__all__ = ["EscalationSink"]


class EscalationSink(Protocol):
    """Opens a pending escalation and hands back the record, id included."""

    async def create(
        self,
        *,
        decision_id: UUID,
        task_id: UUID,
        agent_id: str,
        principal_id: str,
        intent_hash: str,
        requested_scopes: frozenset[str],
        requested_amount: Decimal,
        reason: str,
        now: datetime,
        ttl: timedelta,
    ) -> Escalation:
        """Persist a new pending escalation.

        Raises:
            Exception: Whatever the underlying store raised. The caller treats this as a
                reason to deny, not to proceed without an escalation id.
        """
        ...
