"""Identity tree API endpoints — T-045.

Serves the agent delegation tree via JSON snapshots and Server-Sent Events (SSE) diffs.
Provides a block source lookup endpoint for the console's caveat panel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Request
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


class BlockSourceResponse(BaseModel):
    """Response containing caveat block sources."""

    agent_id: str
    blocks: list[BlockSourceItem]


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
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> TreeSnapshot:
        """Fetch the full agent delegation tree for a task."""
        current_time = _now()
        nodes = await build_tree(session, task_id=task_id, now=current_time)
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

        stmt = (
            select(AuditRecordRow)
            .where(
                AuditRecordRow.record["task_id"].as_string() == task_id_str,
                AuditRecordRow.record["agent_id"].as_string() == agent_id,
            )
            .order_by(desc(AuditRecordRow.seq))
            .limit(1)
        )

        result = await session.execute(stmt)
        record_row = result.scalars().first()

        if not record_row:
            raise HTTPException(status_code=404, detail="agent not found for task")

        rec = record_row.record
        chain_list_obj = rec.get("token_chain_ids", [])
        block_ids = [str(x) for x in chain_list_obj] if isinstance(chain_list_obj, list) else []

        # We output Option C: structured caveat refs if they were stored, or just the block IDs.
        # Since DecisionRecord currently has `failing_caveat` but not all structured caveats,
        # we will render what is available. For now we just return the block IDs as the
        # "source" to satisfy the UI requirement of showing block IDs.

        blocks = []
        for i, b_id in enumerate(block_ids):
            # In Option C, we don't have raw Datalog source. The source field is set to a
            # structured representation or empty if no known_caveats are available.
            source = f"Block ID: {b_id}"
            blocks.append(BlockSourceItem(index=i, source=source, block_id=b_id))

        return BlockSourceResponse(agent_id=agent_id, blocks=blocks)

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
