"""Reading decisions back out for the live stream — T-046.

The stream reads the **audit chain**, not a side channel. `audit_records` is already the
durable record of every decision (spec 08), its `seq` is gap-free and monotonic, and T-022
retries rather than drops — so a cursor over `seq` gives the console replay for free and
survives a PEP restart. An in-process pub/sub would have been fewer moving parts and would
have lost every decision the console was not watching for.

The one piece of real work here is `explain()`. T-046's third criterion is that *a denial
shows the exact failing caveat inline, not a generic message* — "denied by policy" is what
every IAM product already says and is precisely the thing this project exists not to do.
`DecisionRecord.failing_caveat` carries `block_index`, `kind` and `detail`, so the answer is
available; it just has to be assembled rather than thrown away.

`record` is a `DecisionRecord` serialised to JSONB and holds `arg_digest` only, never
argument values (NFR-5, TM-13). Nothing here reaches past it, so the stream cannot become a
PII leak by accident.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from agentiam_controlplane.db.models import AuditRecordRow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["DecisionEvent", "DecisionFilter", "explain", "read_since"]

#: A page of stream output. Large enough that a burst is not dribbled out over many polls,
#: small enough that a console opening onto a long-running demo does not receive megabytes
#: in its first frame.
DEFAULT_LIMIT = 100


@dataclass(frozen=True, slots=True)
class DecisionFilter:
    """What the console asked to see.

    All three are `None` by default, meaning *no filter* — distinct from a filter that
    matches nothing. Filtering happens in SQL rather than in Python because the chain grows
    without bound and the demo runs three agents concurrently.
    """

    outcome: str | None = None
    agent_id: str | None = None
    scope: str | None = None

    def matches_nothing(self) -> bool:
        """Whether an empty string was supplied, which no record can match.

        Worth distinguishing from `None`: `?agent_id=` in a URL is an empty filter value,
        and silently treating it as "no filter" would show an operator everything when they
        asked for one agent.
        """
        return any(value == "" for value in (self.outcome, self.agent_id, self.scope))


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    """One decision, flattened for the console.

    A projection rather than the whole `DecisionRecord`: the stream is a live feed, and the
    fields a reader scans for are the outcome, who did it, what they touched, and — when it
    was refused — why. `seq` is the cursor, so a reconnecting client resumes rather than
    replaying.
    """

    seq: int
    decision_id: uuid.UUID
    timestamp: datetime
    outcome: str
    reason_code: str
    agent_id: str
    principal_id: str
    scope: str
    tool_id: str
    depth: int
    latency_us: int
    #: The inline explanation. Never empty for a refusal — see `explain`.
    explanation: str
    #: Present only when the refusal came from a caveat rather than policy or budget.
    failing_caveat_kind: str | None
    failing_caveat_block: int | None
    drift_score: str | None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready, for the SSE frame."""
        return {
            "seq": self.seq,
            "decision_id": str(self.decision_id),
            "timestamp": self.timestamp.isoformat(),
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "agent_id": self.agent_id,
            "principal_id": self.principal_id,
            "scope": self.scope,
            "tool_id": self.tool_id,
            "depth": self.depth,
            "latency_us": self.latency_us,
            "explanation": self.explanation,
            "failing_caveat_kind": self.failing_caveat_kind,
            "failing_caveat_block": self.failing_caveat_block,
            "drift_score": self.drift_score,
        }


def explain(record: dict[str, Any]) -> str:
    """A human sentence for why this decision went the way it did.

    T-046: *a denial shows the exact failing caveat inline, not a generic message.* So when
    `failing_caveat` is present the sentence names its kind, which block of the token chain
    carried it, and the caveat's own detail — that is the difference between "denied by
    policy" and "block 2's `BudgetCeiling` refused it: spend_bdt 60000 exceeds 50000".

    Falls back to `reason_detail`, which the PEP already writes for every refusal, and only
    then to the bare reason code. The fallbacks exist because a denial with no explanation
    is the failure mode this whole function is here to prevent — an empty string is never
    returned.
    """
    outcome = str(record.get("outcome", "")).lower()
    reason_code = str(record.get("reason_code", "")) or "UNKNOWN"
    detail = str(record.get("reason_detail", "") or "").strip()

    if outcome == "allow":
        return detail or "allowed"

    caveat = record.get("failing_caveat")
    if isinstance(caveat, dict):
        kind = str(caveat.get("kind", "") or "caveat")
        block = caveat.get("block_index")
        where = f"block {block}" if isinstance(block, int) else "the token chain"
        caveat_detail = str(caveat.get("detail", "") or "").strip()
        sentence = f"{where}'s {kind} caveat refused it"
        if caveat_detail:
            sentence = f"{sentence}: {caveat_detail}"
        elif detail:
            sentence = f"{sentence}: {detail}"
        return sentence

    return detail or reason_code


def _project(row: AuditRecordRow) -> DecisionEvent:
    """One row to one event, tolerating a record that is missing fields.

    Deliberately forgiving: the console is a *view*. A malformed or older record should
    render as best it can rather than break the stream for every well-formed one after it.
    """
    record: dict[str, Any] = dict(row.record)
    caveat = record.get("failing_caveat")
    caveat_dict = caveat if isinstance(caveat, dict) else {}
    drift = record.get("drift_score")

    return DecisionEvent(
        seq=row.seq,
        decision_id=row.decision_id,
        timestamp=row.created_at,
        outcome=str(record.get("outcome", "unknown")),
        reason_code=str(record.get("reason_code", "UNKNOWN")),
        agent_id=str(record.get("agent_id", "")),
        principal_id=str(record.get("principal_id", "")),
        scope=str(record.get("scope", "")),
        tool_id=str(record.get("tool_id", "") or ""),
        depth=int(record.get("depth", 0) or 0),
        latency_us=int(record.get("latency_us", 0) or 0),
        explanation=explain(record),
        failing_caveat_kind=(
            str(caveat_dict["kind"]) if caveat_dict.get("kind") is not None else None
        ),
        failing_caveat_block=(
            int(caveat_dict["block_index"])
            if isinstance(caveat_dict.get("block_index"), int)
            else None
        ),
        drift_score=str(drift) if drift is not None else None,
    )


async def read_since(
    session: AsyncSession,
    *,
    after_seq: int = 0,
    filters: DecisionFilter | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Sequence[DecisionEvent]:
    """Decisions with `seq > after_seq`, oldest first, honouring `filters`.

    Oldest first so a reader can advance its cursor to the last element and lose nothing.
    Newest-first would look better in a table and would silently drop records whenever a
    burst exceeded `limit`.
    """
    active = filters or DecisionFilter()
    if active.matches_nothing():
        return []

    stmt = select(AuditRecordRow).where(AuditRecordRow.seq > after_seq)

    if active.outcome is not None:
        stmt = stmt.where(AuditRecordRow.record["outcome"].as_string() == active.outcome)
    if active.agent_id is not None:
        stmt = stmt.where(AuditRecordRow.record["agent_id"].as_string() == active.agent_id)
    if active.scope is not None:
        stmt = stmt.where(AuditRecordRow.record["scope"].as_string() == active.scope)

    stmt = stmt.order_by(AuditRecordRow.seq).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_project(row) for row in rows]
