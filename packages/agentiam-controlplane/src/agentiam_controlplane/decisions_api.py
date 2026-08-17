"""Live decision stream — T-046, `PLAN.md` §1189.

`GET /v1/decisions` is a page, `GET /v1/decisions/stream` is the same query as Server-Sent
Events. Both take the same three filters, so what an operator sees live is exactly what they
would see on reload — a stream whose filters disagreed with its snapshot would be worse than
having no snapshot.

SSE rather than WebSocket. `PLAN.md` allows either, and this direction is one-way: the
console never sends anything back. SSE is plain HTTP, reconnects on its own in every browser,
and passes through proxies that would need explicit configuration for an upgrade handshake.
A WebSocket would buy bidirectionality nothing here uses.

Follows T-045's polling shape deliberately (`tree_api.py`): poll, emit what is new, emit a
heartbeat when nothing is. Two streams in one console that behave differently would be two
things to debug.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from agentiam_controlplane.db.decisions import DecisionFilter, read_since

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = ["build_router"]

#: How often the stream looks for new decisions. Matches `tree_api`'s cadence rather than
#: inventing a second one. Fast enough that beat 3's denial appears while a judge is still
#: looking at the screen; slow enough that an idle console is not a load generator.
POLL_INTERVAL_S = 1.0

#: Polls of silence before a comment frame goes out. Proxies and load balancers close an
#: idle connection, and a stream that dies quietly during a demo looks like a broken product
#: rather than a quiet minute.
HEARTBEAT_AFTER_POLLS = 15

#: How long one connection lives before the server closes it and lets the client come back.
#:
#: Not arbitrary hygiene. `request.is_disconnected()` is the only other exit from the poll
#: loop, and it is **not** reliable — under an in-process ASGI transport it never reports a
#: disconnect at all, so the generator ran forever and hung its caller. In production the
#: same shape leaks a task per abandoned tab. `EventSource` reconnects on its own and the
#: `after_seq` cursor means a reconnect resumes rather than replays, so a bounded lifetime
#: costs a client nothing and gives the server a guaranteed exit.
MAX_STREAM_S = 900.0


def _sse(event: str, payload: object) -> str:
    """One SSE frame. `json.dumps` with no newlines, because a newline ends the frame."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def build_router(
    *,
    session_factory: Callable[[], AsyncSession] | None,
) -> APIRouter:
    """Wire `/v1/decisions`.

    A `None` `session_factory` yields 503 on both routes, matching `tree_api`: the console
    boots without a database and visibly cannot stream, rather than failing to start.
    """
    router = APIRouter(prefix="/v1/decisions", tags=["Decisions"])

    async def get_session() -> AsyncGenerator[AsyncSession, None]:
        if session_factory is None:
            raise HTTPException(status_code=503, detail="no database configured")
        async with session_factory() as session:
            yield session

    def _filters(outcome: str | None, agent_id: str | None, scope: str | None) -> DecisionFilter:
        return DecisionFilter(outcome=outcome, agent_id=agent_id, scope=scope)

    @router.get("")
    async def list_decisions(
        after_seq: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        outcome: str | None = Query(None),
        agent_id: str | None = Query(None),
        scope: str | None = Query(None),
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        """A page of decisions after `after_seq`, oldest first."""
        events = await read_since(
            session,
            after_seq=after_seq,
            filters=_filters(outcome, agent_id, scope),
            limit=limit,
        )
        return {
            "decisions": [event.as_dict() for event in events],
            # The cursor to resume from. Echoed back rather than left to the client to
            # compute, so a client that filtered everything out still advances.
            "next_seq": events[-1].seq if events else after_seq,
        }

    @router.get("/stream")
    async def stream_decisions(
        request: Request,
        after_seq: int = Query(0, ge=0),
        outcome: str | None = Query(None),
        agent_id: str | None = Query(None),
        scope: str | None = Query(None),
    ) -> StreamingResponse:
        """Stream decisions as they land, filtered the same way as the page."""
        if session_factory is None:
            raise HTTPException(status_code=503, detail="no database configured")

        filters = _filters(outcome, agent_id, scope)

        async def events() -> AsyncGenerator[str, None]:
            cursor = after_seq
            quiet = 0
            deadline = asyncio.get_running_loop().time() + MAX_STREAM_S

            # An immediate snapshot, so a console that connects mid-demo is not blank until
            # the next decision happens to arrive.
            async with session_factory() as session:
                initial = await read_since(session, after_seq=cursor, filters=filters)
            if initial:
                cursor = initial[-1].seq
            yield _sse("snapshot", [event.as_dict() for event in initial])

            while True:
                if await request.is_disconnected():
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    # Tell the client why the stream ended, so a reconnect looks like the
                    # design rather than a fault. `EventSource` retries on its own.
                    yield _sse("recycle", {"seq": cursor})
                    break
                await asyncio.sleep(POLL_INTERVAL_S)

                try:
                    async with session_factory() as session:
                        batch = await read_since(session, after_seq=cursor, filters=filters)
                except Exception:
                    # One failed poll must not end the stream: the console would go silent
                    # with no indication, and the operator would read that as "nothing is
                    # happening" rather than "the feed broke".
                    logger.warning("decision stream poll failed", exc_info=True)
                    continue

                if batch:
                    cursor = batch[-1].seq
                    quiet = 0
                    for event in batch:
                        yield _sse("decision", event.as_dict())
                    continue

                quiet += 1
                if quiet >= HEARTBEAT_AFTER_POLLS:
                    quiet = 0
                    yield _sse("heartbeat", {"seq": cursor})

        return StreamingResponse(events(), media_type="text/event-stream")

    return router
