"""Audit explorer + custody view against real Postgres — T-048.

`db/audit.py`'s `custody()` and `verify_chain()` are T-023's, already proven against real
tampering, deletion, reordering and head truncation in `test_audit_chain.py` — these tests do
not repeat that. What is new here is `db/audit_search.py` (JSONB filtering, same class of risk
as `read_since`'s, so it needs the real database) and the three HTTP routes wiring it, `custody`
and `verify_chain` together.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.audit import append
from agentiam_controlplane.db.audit_search import AuditSearchFilter, search
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.models import AuditRecordRow
from agentiam_core.errors import ReasonCode
from agentiam_core.models import Budget, CaveatKind, CaveatRef, DecisionRecord, Outcome

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def a_record(**over: Any) -> DecisionRecord:
    base: dict[str, Any] = {
        "decision_id": uuid.uuid4(),
        "trace_id": "trace-1",
        "timestamp": NOW,
        "pep_id": "pep-1",
        "token_chain_ids": ["b1"],
        "principal_id": "kc:alice",
        "task_id": uuid.uuid4(),
        "agent_id": "agt-1",
        "depth": 1,
        "scope": "invoice:read",
        "tool_id": "invoice_api",
        "arg_digest": "0" * 64,
        "outcome": Outcome.ALLOW,
        "reason_code": ReasonCode.OK,
        "reason_detail": "",
        "policy_version": "bundle-1",
        "budget_before": Budget(),
        "budget_after": Budget(),
        "latency_us": 4200,
    }
    merged = base | over
    if str(merged["outcome"]) != Outcome.ALLOW and merged["reason_code"] is ReasonCode.OK:
        merged["reason_code"] = ReasonCode.POLICY_DENIED
    return DecisionRecord(**merged)


async def seed(engine: AsyncEngine, records: list[DecisionRecord]) -> None:
    async with make_session_factory(engine)() as session:
        await append(session, records)


class TestSearchFilters:
    async def test_search_with_no_filter_returns_everything_newest_first(
        self, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record(), a_record(), a_record()])
        async with make_session_factory(migrated_engine)() as session:
            result = await search(session)
        assert [e.seq for e in result.events] == [3, 2, 1]
        assert result.total == 3

    async def test_search_by_decision_id(self, migrated_engine: AsyncEngine) -> None:
        target = a_record()
        await seed(migrated_engine, [a_record(), target, a_record()])
        async with make_session_factory(migrated_engine)() as session:
            result = await search(
                session, filters=AuditSearchFilter(decision_id=target.decision_id)
            )
        assert [e.decision_id for e in result.events] == [target.decision_id]
        assert result.total == 1

    async def test_search_by_task_id(self, migrated_engine: AsyncEngine) -> None:
        task = uuid.uuid4()
        await seed(
            migrated_engine,
            [a_record(task_id=task), a_record(), a_record(task_id=task)],
        )
        async with make_session_factory(migrated_engine)() as session:
            result = await search(session, filters=AuditSearchFilter(task_id=task))
        assert result.total == 2
        assert all(e.task_id == task for e in result.events)

    async def test_search_by_agent_and_principal(self, migrated_engine: AsyncEngine) -> None:
        await seed(
            migrated_engine,
            [
                a_record(agent_id="agt-2", principal_id="kc:bob"),
                a_record(agent_id="agt-1", principal_id="kc:alice"),
            ],
        )
        async with make_session_factory(migrated_engine)() as session:
            result = await search(
                session, filters=AuditSearchFilter(agent_id="agt-2", principal_id="kc:bob")
            )
        assert result.total == 1
        assert result.events[0].agent_id == "agt-2"

    async def test_search_by_scope_and_outcome(self, migrated_engine: AsyncEngine) -> None:
        await seed(
            migrated_engine,
            [
                a_record(scope="payment:initiate", outcome="deny"),
                a_record(scope="payment:initiate", outcome="allow"),
            ],
        )
        async with make_session_factory(migrated_engine)() as session:
            result = await search(
                session,
                filters=AuditSearchFilter(scope="payment:initiate", outcome="deny"),
            )
        assert result.total == 1
        assert result.events[0].outcome == "deny"

    async def test_pagination_reports_the_true_total(self, migrated_engine: AsyncEngine) -> None:
        await seed(migrated_engine, [a_record() for _ in range(5)])
        async with make_session_factory(migrated_engine)() as session:
            first_page = await search(session, limit=2, offset=0)
            second_page = await search(session, limit=2, offset=2)
        assert first_page.total == second_page.total == 5
        assert len(first_page.events) == 2
        assert [e.seq for e in first_page.events] == [5, 4]
        assert [e.seq for e in second_page.events] == [3, 2]


class TestTheEndpoints:
    @pytest.fixture
    async def client(self, migrated_engine: AsyncEngine) -> AsyncClient:
        app = create_app(session_factory=make_session_factory(migrated_engine))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_search_endpoint_honours_filters(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record(agent_id="agt-1"), a_record(agent_id="agt-2")])
        async with client:
            resp = await client.get("/v1/audit/search", params={"agent_id": "agt-2"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"][0]["agent_id"] == "agt-2"

    async def test_custody_endpoint_renders_the_narrative(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        task = uuid.uuid4()
        await seed(
            migrated_engine,
            [
                a_record(task_id=task, scope="invoice:read"),
                a_record(
                    task_id=task,
                    scope="payment:initiate",
                    outcome="deny",
                    reason_code=ReasonCode.BUDGET_EXHAUSTED_CAVEAT,
                    failing_caveat=CaveatRef(
                        kind=CaveatKind.BUDGET_CEILING,
                        block_index=2,
                        detail="spend_bdt 60000 exceeds 50000",
                    ),
                ),
            ],
        )
        async with client:
            resp = await client.get(f"/v1/audit/custody/{task}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == str(task)
        assert [e["scope"] for e in body["entries"]] == ["invoice:read", "payment:initiate"]
        # T-048's narrative: a refusal names its caveat, same guarantee as T-046's live feed.
        denied = body["entries"][1]
        assert "budget_ceiling" in denied["explanation"]
        assert "block 2" in denied["explanation"]
        assert "60000" in denied["explanation"]

    async def test_custody_of_an_unknown_task_is_an_empty_narrative(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record()])
        async with client:
            resp = await client.get(f"/v1/audit/custody/{uuid.uuid4()}")
        assert resp.status_code == 200
        assert resp.json()["entries"] == []

    async def test_verify_reports_an_intact_chain(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record(), a_record()])
        async with client:
            resp = await client.post("/v1/audit/verify")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["checked"] == 2

    async def test_verify_reports_a_broken_chain(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed(migrated_engine, [a_record() for _ in range(3)])
        async with make_session_factory(migrated_engine)() as session, session.begin():
            await session.execute(delete(AuditRecordRow).where(AuditRecordRow.seq == 2))

        async with client:
            resp = await client.post("/v1/audit/verify")
        body = resp.json()
        assert body["ok"] is False
        assert body["first_bad_seq"] == 3

    async def test_the_console_page_renders(self, client: AsyncClient) -> None:
        async with client:
            resp = await client.get("/audit")
        assert resp.status_code == 200
        assert "Audit explorer" in resp.text

    async def test_without_a_database_every_route_reports_503(self) -> None:
        app = create_app(session_factory=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/audit/search")).status_code == 503
            assert (await client.get(f"/v1/audit/custody/{uuid.uuid4()}")).status_code == 503
            assert (await client.post("/v1/audit/verify")).status_code == 503
            # The console page itself still renders — same shape as decisions/budgets — it
            # just tells the user there is nothing to search.
            assert (await client.get("/audit")).status_code == 200
