"""Identity tree API endpoints — T-045.

Serves the agent delegation tree via JSON snapshots and Server-Sent Events (SSE) diffs.
Provides a block source lookup endpoint for the console's caveat panel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from agentiam_controlplane.db.models import AuditRecordRow
from agentiam_controlplane.db.tree import TreeNode, build_tree, build_tree_diff

logger = logging.getLogger(__name__)


class TreeSnapshot(BaseModel):
    """Full snapshot of the tree."""

    model_config = ConfigDict(frozen=True)
    task_id: uuid.UUID
    generated_at: datetime
    nodes: list[TreeNode]


class BlockSourceItem(BaseModel):
    """Structured caveat block for a node."""

    index: int
    source: str
    block_id: str


class AgentAuthorityView(BaseModel):
    """What the caveat panel shows about an agent, beyond its block ids.

    Block ids were all this endpoint could return, because the control plane holds decision
    records and never tokens — it cannot read a caveat back out of a hash. The PEP now folds
    the chain where the token *is* in hand and writes the result onto the record
    (`RecordedAuthority`); this is that fold, plus what the record already carried.

    `scopes` and `ceilings` are an **upper** bound: the fold covers the caveats the reader
    recognized, and an unread restriction makes the real token narrower, never wider. The
    panel says so rather than presenting this as an exhaustive list of an agent's limits.
    """

    #: The human this agent is acting for. It was on every decision record and on the tree
    #: node, and in neither the panel nor this model — so clicking a node answered "what may
    #: this agent do" and never "whose authority is it doing it under", which is the question
    #: an operator opens the panel with. Attested, not asserted: bound into the signed root
    #: token and read back off it by the PEP, so no request header can set it.
    principal_id: str | None = None
    role: str = ""
    depth: int = 0
    #: What this agent may ask for, after every block narrowed the grant. `None` for a record
    #: written before the PEP folded authority — absent, not "unconstrained".
    scopes: list[str] | None = None
    #: Tightest ceiling per dimension, keyed by dimension name.
    ceilings: dict[str, Decimal] | None = None
    expires_at: datetime | None = None
    max_depth: int | None = None
    #: Summed across this agent's own allow records: what it has actually spent.
    spent: Decimal = Decimal(0)
    #: How many decisions this agent has on record, so a reader can tell "spent nothing"
    #: from "has never been asked".
    decisions: int = 0


class BlockSourceResponse(BaseModel):
    """Response containing caveat block sources."""

    agent_id: str
    blocks: list[BlockSourceItem]
    #: `None` only when no record for this agent carried a folded authority.
    authority: AgentAuthorityView | None = None


def _decimal(value: object) -> Decimal:
    """Read a money field back out of stored JSON, tolerating absence."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _authority_view(rows: Sequence[AuditRecordRow]) -> AgentAuthorityView | None:
    """Fold one agent's records into what the caveat panel renders.

    `rows` is newest-first. Authority comes from the newest record that carried one — an
    older record's fold describes a token that may since have been narrowed further. Spend
    is the opposite: it sums every record, because that is what the agent has actually done.

    Returns `None` when no record carried an authority at all, which is what a chain
    recorded before the PEP folded them looks like. Rendering zeroes there would claim an
    agent has no budget rather than that nothing was measured.
    """
    newest_authority: dict[str, object] | None = None
    for row in rows:
        candidate = row.record.get("authority")
        if isinstance(candidate, dict):
            newest_authority = candidate
            break

    spent = Decimal(0)
    for row in rows:
        before = row.record.get("budget_before") or {}
        after = row.record.get("budget_after") or {}
        if isinstance(before, dict) and isinstance(after, dict):
            # The pool's remaining balance either side of this one call. A refusal reserves
            # nothing, so its delta is zero and it contributes nothing to the sum.
            spent += _decimal(before.get("spend_bdt")) - _decimal(after.get("spend_bdt"))

    newest = rows[0].record
    if newest_authority is None:
        return None

    raw_ceilings = newest_authority.get("ceilings")
    raw_scopes = newest_authority.get("scopes")
    expires_raw = newest_authority.get("not_after")
    return AgentAuthorityView(
        principal_id=str(newest["principal_id"]) if newest.get("principal_id") else None,
        role=str(newest.get("role") or ""),
        depth=int(str(newest.get("depth", 0) or 0)),
        scopes=[str(s) for s in raw_scopes] if isinstance(raw_scopes, list) else None,
        ceilings=(
            {str(k): _decimal(v) for k, v in raw_ceilings.items()}
            if isinstance(raw_ceilings, dict)
            else None
        ),
        expires_at=datetime.fromisoformat(expires_raw) if isinstance(expires_raw, str) else None,
        max_depth=(
            int(str(newest_authority["max_depth"]))
            if newest_authority.get("max_depth") is not None
            else None
        ),
        spent=spent,
        decisions=len(rows),
    )


def build_router(
    *,
    session_factory: Callable[[], AsyncSession] | None,
    now: Callable[[], datetime] | None = None,
) -> APIRouter:
    """Wire the `/v1/tree` endpoints.

    If `session_factory` is `None`, the endpoints return 503 Service Unavailable — this allows
    the control plane to boot in memory-only mode without crashing, but tree rendering is disabled.
    """
    router = APIRouter(prefix="/v1/tree", tags=["Tree"])

    def _now() -> datetime:
        return now() if now else datetime.now(UTC)

    async def get_session() -> AsyncGenerator[AsyncSession, None]:
        if session_factory is None:
            raise HTTPException(status_code=503, detail="no database configured")
        async with session_factory() as session:
            yield session

    @router.get("/{task_id}", response_model=TreeSnapshot)
    async def get_tree(
        task_id: uuid.UUID = Path(...),  # noqa: B008
        generations: str = Query("current", pattern="^(current|all)$"),
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> TreeSnapshot:
        """Fetch the agent delegation tree for a task.

        `generations=all` also returns chains superseded by a later minting of the same
        mandate, which the append-only audit chain still remembers.
        """
        current_time = _now()
        nodes = await build_tree(
            session,
            task_id=task_id,
            now=current_time,
            include_superseded=generations == "all",
        )
        return TreeSnapshot(task_id=task_id, generated_at=current_time, nodes=nodes)

    @router.get("/{task_id}/blocks/{agent_id}", response_model=BlockSourceResponse)
    async def get_block_source(
        task_id: uuid.UUID = Path(...),  # noqa: B008
        agent_id: str = Path(...),
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> BlockSourceResponse:
        """Fetch structured caveat data for a given agent's token chain.

        Uses Option C (from implementation plan): returning block IDs and structured caveats
        from the audit record since the raw token isn't stored in the control plane.
        """
        task_id_str = str(task_id)

        # Every record for this agent, not just the newest: the newest gives the current
        # authority, but spend has to be summed across all of them.
        stmt = (
            select(AuditRecordRow)
            .where(
                AuditRecordRow.record["task_id"].as_string() == task_id_str,
                AuditRecordRow.record["agent_id"].as_string() == agent_id,
            )
            .order_by(desc(AuditRecordRow.seq))
        )

        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        record_row = rows[0] if rows else None

        if not record_row:
            raise HTTPException(status_code=404, detail="agent not found for task")

        rec = record_row.record
        chain_list_obj = rec.get("token_chain_ids", [])
        block_ids = [str(x) for x in chain_list_obj] if isinstance(chain_list_obj, list) else []

        # The block ids stay: they are what a revocation names, so an operator copying one
        # out of this panel is doing something real. They are no longer *all* it shows.
        blocks = [
            BlockSourceItem(index=i, source=f"Block ID: {b_id}", block_id=b_id)
            for i, b_id in enumerate(block_ids)
        ]

        return BlockSourceResponse(
            agent_id=agent_id,
            blocks=blocks,
            authority=_authority_view(rows),
        )

    @router.get("/{task_id}/stream")
    async def stream_tree(
        request: Request,
        task_id: uuid.UUID = Path(...),  # noqa: B008
    ) -> StreamingResponse:
        """Stream real-time tree changes using Server-Sent Events."""
        if session_factory is None:
            raise HTTPException(status_code=503, detail="no database configured")

        async def event_generator() -> AsyncGenerator[str, None]:
            last_nodes: list[TreeNode] = []

            # 1. Send initial snapshot
            async with session_factory() as session:
                last_nodes = await build_tree(session, task_id=task_id, now=_now())

            snapshot_data = json.dumps({"nodes": [n.model_dump(mode="json") for n in last_nodes]})
            yield f"event: snapshot\ndata: {snapshot_data}\n\n"

            heartbeat_counter = 0

            # 2. Poll every 3 seconds for diffs
            while True:
                if await request.is_disconnected():
                    break

                await asyncio.sleep(3.0)
                heartbeat_counter += 1

                async with session_factory() as session:
                    new_nodes = await build_tree(session, task_id=task_id, now=_now())

                diff = build_tree_diff(old=last_nodes, new=new_nodes)

                if diff.added or diff.changed or diff.removed:
                    diff_data = json.dumps(diff.model_dump(mode="json"))
                    yield f"event: diff\ndata: {diff_data}\n\n"
                    last_nodes = new_nodes
                    heartbeat_counter = 0
                elif heartbeat_counter >= 5:  # 15 seconds
                    yield "event: heartbeat\ndata: {}\n\n"
                    heartbeat_counter = 0

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return router
