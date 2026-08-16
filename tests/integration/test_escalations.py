"""Integration tests for the escalation queue's persistence — T-037.

Real Postgres via testcontainers, because EC-A10 (an escalation resolves exactly once) is a
row-locking guarantee that a mock session cannot prove. Ten approvers race the same pending
escalation the way `test_ledger_commit.py` races `LEDGER_COMMIT`.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.escalations import (
    create,
    get,
    list_by_state,
    resolve_approve,
    resolve_deny,
)
from agentiam_controlplane.errors import EscalationNotFoundError
from agentiam_core.escalation import (
    ApproverNotAuthorized,
    Escalation,
    EscalationExpired,
    EscalationNotPending,
    EscalationState,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
_INTENT_HASH = hashlib.sha256(b"invoice INV-2291 exceeds the standing ceiling").hexdigest()
_APPROVERS = frozenset({"kc:manager", "kc:cfo"})


async def _an_escalation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ttl: timedelta = timedelta(minutes=15),
    **over: object,
) -> Escalation:
    base: dict[str, object] = {
        "decision_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "agent_id": "agt-1",
        "principal_id": "kc:alice",
        "intent_hash": _INTENT_HASH,
        "requested_scopes": frozenset({"payment:initiate"}),
        "requested_amount": Decimal("50000"),
        "reason": "invoice INV-2291 exceeds the standing ceiling",
        "now": _NOW,
        "ttl": ttl,
    }
    async with session_factory() as s:
        return await create(s, **(base | over))  # type: ignore[arg-type]


async def test_create_persists_a_pending_row(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    escalation = await _an_escalation(factory)

    async with factory() as s:
        fetched = await get(s, escalation.id)

    assert fetched is not None
    assert fetched.state is EscalationState.PENDING
    assert fetched.requested_scopes == frozenset({"payment:initiate"})
    assert fetched.requested_amount == Decimal("50000.0000")
    assert fetched.intent_hash == _INTENT_HASH


async def test_get_returns_none_for_an_unknown_id(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        assert await get(s, uuid.uuid4()) is None


async def test_list_by_state_pending_excludes_an_unswept_expired_row(
    migrated_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(migrated_engine)
    live = await _an_escalation(factory, ttl=timedelta(minutes=15))
    await _an_escalation(factory, ttl=timedelta(seconds=1))

    async with factory() as s:
        pending = await list_by_state(
            s, state=EscalationState.PENDING, now=_NOW + timedelta(minutes=1)
        )

    assert [e.id for e in pending] == [live.id]


async def test_resolve_approve_persists_the_resolution_and_returns_a_grant(
    migrated_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(migrated_engine)
    escalation = await _an_escalation(factory)

    async with factory() as s:
        resolved, grant = await resolve_approve(
            s,
            escalation.id,
            approver="kc:manager",
            authorized=_APPROVERS,
            now=_NOW,
            elevation_ttl=timedelta(minutes=5),
        )
    assert resolved.state is EscalationState.APPROVED
    assert grant.scopes == escalation.requested_scopes
    assert grant.intent_hash == _INTENT_HASH

    async with factory() as s:
        fetched = await get(s, escalation.id)
    assert fetched is not None
    assert fetched.state is EscalationState.APPROVED
    assert fetched.resolved_by == "kc:manager"


async def test_resolve_deny_persists_the_reason(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    escalation = await _an_escalation(factory)

    async with factory() as s:
        await resolve_deny(
            s,
            escalation.id,
            approver="kc:cfo",
            authorized=_APPROVERS,
            now=_NOW,
            reason="vendor is not on the approved list",
        )

    async with factory() as s:
        fetched = await get(s, escalation.id)
    assert fetched is not None
    assert fetched.state is EscalationState.DENIED
    assert fetched.resolution_reason == "vendor is not on the approved list"


async def test_resolve_approve_raises_not_found_for_an_unknown_id(
    migrated_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        with pytest.raises(EscalationNotFoundError):
            await resolve_approve(
                s,
                uuid.uuid4(),
                approver="kc:manager",
                authorized=_APPROVERS,
                now=_NOW,
                elevation_ttl=timedelta(minutes=5),
            )


async def test_approving_twice_is_refused(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    escalation = await _an_escalation(factory)
    async with factory() as s:
        await resolve_approve(
            s,
            escalation.id,
            approver="kc:manager",
            authorized=_APPROVERS,
            now=_NOW,
            elevation_ttl=timedelta(minutes=5),
        )
    async with factory() as s:
        with pytest.raises(EscalationNotPending):
            await resolve_approve(
                s,
                escalation.id,
                approver="kc:cfo",
                authorized=_APPROVERS,
                now=_NOW,
                elevation_ttl=timedelta(minutes=5),
            )


async def test_an_unauthorized_approver_is_refused(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    escalation = await _an_escalation(factory)
    async with factory() as s:
        with pytest.raises(ApproverNotAuthorized):
            await resolve_approve(
                s,
                escalation.id,
                approver="kc:intruder",
                authorized=_APPROVERS,
                now=_NOW,
                elevation_ttl=timedelta(minutes=5),
            )


async def test_approving_after_ttl_is_refused(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    escalation = await _an_escalation(factory, ttl=timedelta(minutes=15))
    async with factory() as s:
        with pytest.raises(EscalationExpired):
            await resolve_approve(
                s,
                escalation.id,
                approver="kc:manager",
                authorized=_APPROVERS,
                now=_NOW + timedelta(minutes=16),
                elevation_ttl=timedelta(minutes=5),
            )


async def test_ten_racing_approvers_resolve_exactly_once(
    postgres_url: str, migrated_engine: AsyncEngine
) -> None:
    """EC-A10 under real concurrency.

    The row lock, not the pure check alone, is what proves only one of many simultaneous
    approvers can win.
    """
    factory = make_session_factory(migrated_engine)
    escalation = await _an_escalation(factory)

    concurrent_engine = make_engine(postgres_url, poolclass=NullPool)
    concurrent_factory = make_session_factory(concurrent_engine)

    async def one_approval(approver: str) -> bool:
        async with concurrent_factory() as s:
            try:
                await resolve_approve(
                    s,
                    escalation.id,
                    approver=approver,
                    authorized=_APPROVERS,
                    now=_NOW,
                    elevation_ttl=timedelta(minutes=5),
                )
                return True
            except EscalationNotPending:
                return False

    approvers = ["kc:manager", "kc:cfo"] * 5
    results = await asyncio.gather(*(one_approval(a) for a in approvers))
    await concurrent_engine.dispose()

    assert sum(results) == 1

    async with factory() as s:
        fetched = await get(s, escalation.id)
    assert fetched is not None
    assert fetched.state is EscalationState.APPROVED
