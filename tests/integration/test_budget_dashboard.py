"""The budget and lease dashboard — T-047, `PLAN.md` §1192.

Driven through the real ledger operations (`acquire`, `ledger_commit`, `release`) rather
than hand-inserted rows, for the same reason T-046's fixture ended up going through
`append()`: a dashboard built on fabricated state proves the arithmetic and nothing about
whether the numbers it reads are the ones the ledger actually writes.

The four criteria, one class each. The invariant indicator gets two — that it is green on a
healthy ledger, and that it goes red on a corrupted one — because an indicator that has only
ever been observed green is indistinguishable from a light that is painted on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.budget_dashboard import Dashboard, build_dashboard
from agentiam_controlplane.db.ledger import acquire, ledger_commit, release
from agentiam_controlplane.db.models import BudgetRow

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)


async def seed_pool(
    engine: AsyncEngine, *, total: Decimal = Decimal("1000.0000")
) -> tuple[uuid.UUID, uuid.UUID]:
    """One pool budget. Returns `(mandate_id, budget_id)`."""
    mandate_id = uuid.uuid4()
    async with make_session_factory(engine)() as s, s.begin():
        row = BudgetRow(mandate_id=mandate_id, dimension="spend_bdt", total=total)
        s.add(row)
        await s.flush()
        return mandate_id, row.id


async def dashboard_of(engine: AsyncEngine, *, now: datetime = NOW) -> Dashboard:
    """One snapshot, on its own session."""
    async with make_session_factory(engine)() as s:
        return await build_dashboard(s, now=now)


class TestSpendGauge:
    async def test_an_untouched_pool_is_entirely_available(
        self, migrated_engine: AsyncEngine
    ) -> None:
        await seed_pool(migrated_engine, total=Decimal("1000.0000"))
        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.total == Decimal("1000.0000")
        assert gauge.available == Decimal("1000.0000")
        assert gauge.committed == Decimal("0.0000")

    async def test_acquiring_moves_money_from_available_to_leased(
        self, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id, _ = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s:
            await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("250.0000"),
                ttl=TTL,
                now=NOW,
            )

        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.leased == Decimal("250.0000")
        assert gauge.committed == Decimal("0.0000")
        # The point of separating the two: money held is not money spent.
        assert gauge.available == Decimal("750.0000")

    async def test_committing_moves_leased_into_committed(
        self, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id, _ = await seed_pool(migrated_engine)
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

        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.committed == Decimal("100.0000")
        assert gauge.as_dict()["spend_fraction"] == "0.1000"

    async def test_the_four_terms_always_sum_to_the_total(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The gauge is a picture of the pool invariant, so it must add up exactly."""
        mandate_id, _ = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("300.0000"),
                ttl=TTL,
                now=NOW,
            )
        async with make_session_factory(migrated_engine)() as s:
            await ledger_commit(
                s,
                lease_id=lease.id,
                reservation_id=uuid.uuid4(),
                amount=Decimal("120.0000"),
                now=NOW,
            )

        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.committed + gauge.leased + gauge.allocated + gauge.available == gauge.total

    async def test_a_zero_total_pool_reports_zero_rather_than_dividing_by_it(
        self, migrated_engine: AsyncEngine
    ) -> None:
        # A pool created but never funded is a real state, not an error.
        await seed_pool(migrated_engine, total=Decimal("0.0000"))
        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.as_dict()["spend_fraction"] == "0.0000"

    async def test_allocation_rows_are_not_counted_as_pools(
        self, migrated_engine: AsyncEngine
    ) -> None:
        # An allocation's total is a slice of its parent's; counting both would double-count
        # the same money and show a mandate as over-committed when it is not.
        mandate_id, budget_id = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s, s.begin():
            s.add(
                BudgetRow(
                    mandate_id=mandate_id,
                    dimension="spend_bdt",
                    total=Decimal("100.0000"),
                    parent_budget_id=budget_id,
                    agent_id="agt-child",
                )
            )

        gauges = (await dashboard_of(migrated_engine)).gauges
        assert len(gauges) == 1
        assert gauges[0].budget_id == budget_id


class TestLeaseUtilization:
    async def test_utilization_is_settled_over_granted(self, migrated_engine: AsyncEngine) -> None:
        mandate_id, _ = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("200.0000"),
                ttl=TTL,
                now=NOW,
            )
        async with make_session_factory(migrated_engine)() as s:
            await ledger_commit(
                s,
                lease_id=lease.id,
                reservation_id=uuid.uuid4(),
                amount=Decimal("50.0000"),
                now=NOW,
            )

        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.active_leases == 1
        assert gauge.as_dict()["lease_utilization"] == "0.2500"

    async def test_a_released_lease_stops_counting(self, migrated_engine: AsyncEngine) -> None:
        # Only active leases are "held". A released one is no longer stranding anything, so
        # counting it would make utilization look permanently bad.
        mandate_id, _ = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("200.0000"),
                ttl=TTL,
                now=NOW,
            )
        async with make_session_factory(migrated_engine)() as s:
            await release(s, lease_id=lease.id)

        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.active_leases == 0
        assert gauge.leased == Decimal("0.0000")

    async def test_no_leases_reports_zero_not_an_error(self, migrated_engine: AsyncEngine) -> None:
        await seed_pool(migrated_engine)
        (gauge,) = (await dashboard_of(migrated_engine)).gauges
        assert gauge.as_dict()["lease_utilization"] == "0.0000"


class TestTopUpRate:
    async def test_acquires_inside_the_window_are_counted(
        self, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id, _ = await seed_pool(migrated_engine, total=Decimal("10000.0000"))
        for _ in range(3):
            async with make_session_factory(migrated_engine)() as s:
                await acquire(
                    s,
                    mandate_id=mandate_id,
                    dimension="spend_bdt",
                    pep_id="pep-1",
                    requested=Decimal("100.0000"),
                    ttl=TTL,
                    now=NOW,
                )

        report = await dashboard_of(migrated_engine)
        assert report.topups_in_window == 3
        # A one-minute window means the count *is* the per-minute rate.
        assert report.as_dict()["topups_per_minute"] == 3.0

    async def test_acquires_before_the_window_are_excluded(
        self, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id, _ = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s:
            await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("100.0000"),
                ttl=TTL,
                now=NOW,
            )

        # Ask an hour later: the lease is old news, so the *rate* is zero even though the
        # lease still exists.
        report = await dashboard_of(migrated_engine, now=NOW + timedelta(hours=1))
        assert report.topups_in_window == 0


class TestInvariantIndicator:
    async def test_a_healthy_ledger_reports_green(self, migrated_engine: AsyncEngine) -> None:
        mandate_id, _ = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s:
            await acquire(
                s,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                pep_id="pep-1",
                requested=Decimal("100.0000"),
                ttl=TTL,
                now=NOW,
            )

        report = await dashboard_of(migrated_engine)
        assert report.invariants_ok is True
        assert report.violations == ()
        assert report.budgets_checked >= 1

    async def test_a_corrupted_ledger_reports_red_and_names_the_violation(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """An indicator only ever seen green is indistinguishable from a painted-on light.

        `leased` is moved behind the ledger's back, exactly as T-016's own acceptance tests
        do — the schema's `CHECK` guards the sum, so the injection has to break a
        cross-table relationship the constraint cannot see.
        """
        _, budget_id = await seed_pool(migrated_engine)
        async with make_session_factory(migrated_engine)() as s, s.begin():
            await s.execute(
                text("UPDATE budgets SET leased = :leased WHERE id = :id"),
                {"leased": Decimal("500.0000"), "id": budget_id},
            )

        report = await dashboard_of(migrated_engine)
        assert report.invariants_ok is False
        assert report.violations
        detail = report.violations[0]["detail"]
        assert str(budget_id) in detail or report.violations[0]["budget_id"] == str(budget_id)


class TestTheEndpoint:
    @pytest.fixture
    async def client(self, migrated_engine: AsyncEngine) -> AsyncClient:
        app = create_app(session_factory=make_session_factory(migrated_engine))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_the_dashboard_endpoint_returns_the_snapshot(
        self, client: AsyncClient, migrated_engine: AsyncEngine
    ) -> None:
        await seed_pool(migrated_engine)
        async with client:
            resp = await client.get("/v1/budgets/dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["invariants_ok"] is True
        assert len(body["gauges"]) == 1
        # Rule 4 reaches the wire: amounts are strings, not JSON numbers.
        assert isinstance(body["gauges"][0]["total"], str)

    async def test_the_console_page_renders(self, client: AsyncClient) -> None:
        async with client:
            resp = await client.get("/budgets")
        assert resp.status_code == 200
        assert "Budgets" in resp.text

    async def test_without_a_database_the_endpoint_reports_503(self) -> None:
        app = create_app(session_factory=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v1/budgets/dashboard")).status_code == 503
