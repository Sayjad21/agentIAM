"""`GET /metrics` against real Postgres — T-049, `PLAN.md` §1197.

Driven through the same real operations `test_audit_explorer.py` (decisions, via `append()`)
and `test_budget_dashboard.py` (budgets, via a seeded `BudgetRow` + `acquire`) already use,
for the same reason: a metrics endpoint proven against hand-built numbers proves the
rendering and nothing about whether it reads what the ledger actually wrote.

`db/decision_metrics.py`'s JSONB grouped query is the one piece of new logic — the same class
of risk `db/audit_search.py`'s filtering was for T-048, so it needs the real database, not a
fake session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.audit import append
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.decision_metrics import count_by_outcome_reason
from agentiam_controlplane.db.ledger import acquire, ledger_commit
from agentiam_controlplane.db.models import BudgetRow
from agentiam_core.errors import ReasonCode
from agentiam_core.models import Budget, DecisionRecord, Outcome

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)


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


async def seed_records(engine: AsyncEngine, records: list[DecisionRecord]) -> None:
    async with make_session_factory(engine)() as session:
        await append(session, records)


async def seed_pool(
    engine: AsyncEngine, *, total: Decimal = Decimal("1000.0000")
) -> tuple[uuid.UUID, uuid.UUID]:
    """One pool budget. Returns `(mandate_id, budget_id)` — same shape as T-047's own."""
    mandate_id = uuid.uuid4()
    async with make_session_factory(engine)() as s, s.begin():
        row = BudgetRow(mandate_id=mandate_id, dimension="spend_bdt", total=total)
        s.add(row)
        await s.flush()
        return mandate_id, row.id


class TestDecisionMetricsQuery:
    async def test_counts_group_by_outcome_and_reason_code(
        self, migrated_engine: AsyncEngine
    ) -> None:
        await seed_records(
            migrated_engine,
            [
                a_record(outcome="allow"),
                a_record(outcome="allow"),
                a_record(outcome="deny", reason_code=ReasonCode.POLICY_DENIED),
            ],
        )
        async with make_session_factory(migrated_engine)() as session:
            counts = await count_by_outcome_reason(session)

        by_key = {(c.outcome, c.reason_code): c.count for c in counts}
        assert by_key[("allow", ReasonCode.OK.value)] == 2
        assert by_key[("deny", ReasonCode.POLICY_DENIED.value)] == 1

    async def test_an_empty_chain_reports_nothing(self, migrated_engine: AsyncEngine) -> None:
        async with make_session_factory(migrated_engine)() as session:
            counts = await count_by_outcome_reason(session)
        assert counts == []


class TestMetricsEndpoint:
    @pytest.fixture
    async def client(self, migrated_engine: AsyncEngine) -> AsyncClient:
        app = create_app(session_factory=make_session_factory(migrated_engine))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_decision_counts_are_exposed(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed_records(migrated_engine, [a_record(), a_record()])
        async with client:
            resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        expected = (
            "agentiam_controlplane_decisions_total"
            f'{{outcome="allow",reason_code="{ReasonCode.OK.value}"}} 2.0'
        )
        assert expected in resp.text

    async def test_budget_gauges_reflect_the_pool(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id, _ = await seed_pool(migrated_engine, total=Decimal("1000.0000"))
        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("250.0000"),
                ttl=TTL,
                now=NOW,
            )
        async with make_session_factory(migrated_engine)() as s:
            await ledger_commit(
                s,
                lease_id=lease.id,
                reservation_id=uuid.uuid4(),
                amount=Decimal("100.0000"),
                now=NOW,
            )

        async with client:
            resp = await client.get("/metrics")
        body = resp.text
        assert f'mandate_id="{mandate_id}"' in body
        assert "agentiam_controlplane_budget_committed_bdt{" in body
        assert "agentiam_controlplane_budget_available_bdt{" in body
        assert "agentiam_controlplane_lease_utilization_ratio{" in body
        assert "agentiam_controlplane_invariant_ok 1.0" in body

    async def test_without_a_database_metrics_reports_503(self) -> None:
        app = create_app(session_factory=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/metrics")).status_code == 503
