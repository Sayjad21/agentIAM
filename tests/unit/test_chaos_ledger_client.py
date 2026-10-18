"""The chaos harness's ledger client — an empty pool is spent, not down (APICTA-08).

`tests/chaos/pepstack.PoolLedgerClient` is the harness's copy of the deployed `SessionLedger`
(`scripts/pep_service.py`). It re-raised the ledger's `LeaseUnavailableError`, which the pool
reads as *unreachable*: a mandate whose budget was gone answered the retryable
`LEASE_UNAVAILABLE`, and every refused request scheduled another top-up. Measured while
building `make bench-compare`: 43,085 ledger round trips in a run the deployed client finishes
in 24. An unreachable ledger must still raise — CH-01 depends on that (CLAUDE.md, 2026-10-16).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentiam_controlplane.db import ledger as ledger_db
from agentiam_controlplane.db.base import make_engine
from agentiam_controlplane.errors import LeaseUnavailableError
from agentiam_core.models import BudgetDimension
from tests.chaos.pepstack import PoolLedgerClient


async def ask(client: PoolLedgerClient) -> object:
    return await client.acquire(
        mandate_id=uuid.uuid4(),
        dimension=BudgetDimension.SPEND_BDT,
        requested=Decimal(1),
        pep_id="pep-1",
        ttl=timedelta(seconds=30),
        now=datetime.now(UTC),
    )


@pytest.fixture
def client() -> PoolLedgerClient:
    # No I/O: the engine is never connected, `acquire` is replaced below.
    return PoolLedgerClient(make_engine("postgresql+asyncpg://a:b@localhost:5432/c"))


async def test_an_empty_pool_is_none(
    client: PoolLedgerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(*_args: object, **_kwargs: object) -> None:
        raise LeaseUnavailableError("no budget available in the pool")

    monkeypatch.setattr("tests.chaos.pepstack.acquire", empty)
    assert await ask(client) is None
    assert client.acquire_failures == 0


async def test_an_unreachable_ledger_still_raises(
    client: PoolLedgerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError("ledger down")

    monkeypatch.setattr("tests.chaos.pepstack.acquire", down)
    with pytest.raises(ConnectionRefusedError):
        await ask(client)
    assert client.acquire_failures == 1


def test_the_client_uses_the_ledger_function_it_is_patched_at() -> None:
    import tests.chaos.pepstack as pepstack

    assert vars(pepstack)["acquire"] is ledger_db.acquire
