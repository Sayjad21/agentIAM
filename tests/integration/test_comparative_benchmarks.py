"""`scripts/run_comparative_benchmarks.py` against a real Postgres, at small sizes — APICTA-08.

The committed figures come from `make bench-compare`. These check that each measurement
measures what its row says: that the token-exchange baseline really reads the database per
call, that B1 really makes one ledger round trip per call and B3 far fewer, and — the one that
would make the table meaningless if it failed — that neither design spends more than the pool.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from scripts import run_comparative_benchmarks as bench

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


class TestAttenuation:
    async def test_both_sides_are_measured(
        self, migrated_engine: AsyncEngine, postgres_url: str
    ) -> None:
        result = await bench.measure_attenuation(postgres_url, n=20)
        for side in ("in_process", "token_exchange"):
            assert result[side]["n"] == 20
            assert 0 < result[side]["median_us"] <= result[side]["p99_us"]

    async def test_the_exchange_reads_the_database_on_every_call(
        self, migrated_engine: AsyncEngine, postgres_url: str
    ) -> None:
        result = await bench.measure_attenuation(postgres_url, n=15)
        # One warm-up call before the timed ones.
        assert result["token_exchange"]["lookups"] == 16

    async def test_the_in_process_child_is_a_real_narrower_token(
        self, migrated_engine: AsyncEngine, postgres_url: str
    ) -> None:
        result = await bench.measure_attenuation(postgres_url, n=3)
        assert result["in_process"]["child_verifies"] is True


class TestBudget:
    @pytest.fixture
    async def result(self, migrated_engine: AsyncEngine, postgres_url: str) -> dict[str, object]:
        return await bench.measure_budget(
            postgres_url,
            peps=3,
            calls_per_pep=40,
            amount=Decimal("10.0000"),
            lease_size=Decimal("100.0000"),
        )

    async def test_b1_makes_one_ledger_round_trip_per_call(self, result: dict[str, object]) -> None:
        b1 = result["b1"]
        assert isinstance(b1, dict)
        assert b1["ledger_round_trips"] == 3 * 40

    async def test_b3_makes_fewer_with_its_settlements_counted(
        self, result: dict[str, object]
    ) -> None:
        """Acquires, releases *and* the batched commits — B3 is not allowed to hide its writes.

        How many fewer depends on the lease size: at this test's 10-calls-per-lease it was
        36 against 120, measured. The committed run's ratio is in `comparative.md`.
        """
        b1, b3 = result["b1"], result["b3"]
        assert isinstance(b1, dict)
        assert isinstance(b3, dict)
        # At least: one acquire, one commit batch and one release per PEP.
        assert 3 * 3 <= b3["ledger_round_trips"] < b1["ledger_round_trips"]

    async def test_neither_design_spends_more_than_the_pool(
        self, result: dict[str, object]
    ) -> None:
        for design in ("b1", "b3"):
            stats = result[design]
            assert isinstance(stats, dict)
            assert Decimal(stats["overspend"]) == 0, design

    async def test_b1_records_every_taka_it_allows(self, result: dict[str, object]) -> None:
        b1 = result["b1"]
        assert isinstance(b1, dict)
        assert Decimal(b1["committed"]) == Decimal(b1["spent"])

    async def test_every_call_is_accounted_for(self, result: dict[str, object]) -> None:
        for design in ("b1", "b3"):
            stats = result[design]
            assert isinstance(stats, dict)
            assert stats["n"] + stats["refused"] == 3 * 40, design

    async def test_a_pool_too_small_refuses_rather_than_overspends(
        self, migrated_engine: AsyncEngine, postgres_url: str
    ) -> None:
        result = await bench.measure_budget(
            postgres_url,
            peps=3,
            calls_per_pep=20,
            amount=Decimal("10.0000"),
            lease_size=Decimal("50.0000"),
            pool_total=Decimal("300.0000"),
        )
        for design in ("b1", "b3"):
            stats = result[design]
            assert isinstance(stats, dict)
            assert stats["refused"] > 0, design
            assert Decimal(stats["overspend"]) == 0, design
