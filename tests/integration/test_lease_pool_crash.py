"""A killed PEP strands budget for at most the TTL — spec 04 §7, §14 limitation 1, T-021.

The fourth of T-021's acceptance criteria, and the only one that cannot be faked. Graceful
shutdown is tested against a fake ledger in `tests/unit/test_pep_pool.py`; this is what
happens when there *is* no shutdown.

A real child process acquires a real lease against a real Postgres and is then killed
without warning. Nothing runs `RELEASE`. The claim under test is that the budget comes back
anyway, because `REAP` reclaims on the lease's ledger-issued `expires_at` rather than on
anything the PEP promises to do.

**`signal.SIGKILL` is not used**, though `PLAN.md` words the criterion that way. Measured:
`hasattr(signal, "SIGKILL")` is False on win32, so a test written against it would import
fine in CI and fail on the development host. `Popen.kill()` is the portable spelling of the
same thing — `SIGKILL` on POSIX, `TerminateProcess` on Windows — and neither gives the child
a chance to clean up, which is the whole point.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.ledger import SKEW_ALLOWANCE, reap
from agentiam_controlplane.db.models import BudgetRow, LeaseRow
from agentiam_core.models import LeaseState
from tests.integration.conftest import make_budget

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

TTL = timedelta(seconds=60)
LEASE_SIZE = Decimal("60.0000")
POOL_TOTAL = Decimal("100.0000")

#: Run in a child process: acquire a lease, announce it, then block forever. The parent
#: kills it mid-sleep, so nothing here ever gets to release anything.
HOLDER = """
import asyncio, sys, uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import acquire


async def main() -> None:
    url, mandate_id, requested = sys.argv[1], uuid.UUID(sys.argv[2]), Decimal(sys.argv[3])
    engine = make_engine(url)
    factory = make_session_factory(engine)
    async with factory() as session:
        lease = await acquire(
            session,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=requested,
            pep_id="pep-doomed",
            ttl=timedelta(seconds=60),
            now=datetime.now(UTC),
        )
    print(lease.id, flush=True)
    await asyncio.sleep(3600)


asyncio.run(main())
"""


async def _pool_state(engine: AsyncEngine, mandate_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """Return `(leased, available)` for the mandate's spend pool."""
    factory = make_session_factory(engine)
    async with factory() as session:
        row = (
            await session.execute(
                select(BudgetRow).where(
                    BudgetRow.mandate_id == mandate_id,
                    BudgetRow.dimension == "spend_bdt",
                    BudgetRow.agent_id.is_(None),
                )
            )
        ).scalar_one()
        return row.leased, row.total - row.committed - row.leased


async def _lease_state(engine: AsyncEngine, lease_id: uuid.UUID) -> tuple[str, datetime]:
    factory = make_session_factory(engine)
    async with factory() as session:
        row = (await session.execute(select(LeaseRow).where(LeaseRow.id == lease_id))).scalar_one()
        return row.state, row.expires_at


class TestCrashedPepStrandsBoundedBudget:
    async def test_a_killed_holder_leaves_budget_stranded_then_reclaimed(
        self, migrated_engine: AsyncEngine, postgres_url: str, tmp_path: Path
    ) -> None:
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)

        script = tmp_path / "holder.py"
        script.write_text(HOLDER, encoding="utf-8")

        child = subprocess.Popen(  # noqa: S603
            [sys.executable, str(script), postgres_url, str(mandate_id), str(LEASE_SIZE)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), timeout=120)
            if not line.strip():
                stderr = child.stderr.read() if child.stderr else ""
                pytest.fail(f"the holder never acquired a lease: {stderr}")
            lease_id = uuid.UUID(line.strip())

            leased, available = await _pool_state(migrated_engine, mandate_id)
            assert leased == LEASE_SIZE
            assert available == POOL_TOTAL - LEASE_SIZE, "the lease must reduce availability"

            # No RELEASE, no graceful anything.
            child.kill()
            child.wait(timeout=30)
            assert child.returncode != 0, "kill() must not look like a clean exit"
        finally:
            if child.poll() is None:  # pragma: no cover - only on an unexpected early exit
                child.kill()
            child.wait(timeout=30)

        # The budget is genuinely stranded: nothing has returned it.
        leased, available = await _pool_state(migrated_engine, mandate_id)
        assert leased == LEASE_SIZE
        assert available == POOL_TOTAL - LEASE_SIZE

        state, expires_at = await _lease_state(migrated_engine, lease_id)
        assert state == LeaseState.ACTIVE.value, "a killed PEP cannot have released anything"

        # A reap before `expires_at + S` must not reclaim — the skew margin is load-bearing,
        # and reclaiming early re-issues budget a lagging PEP still believes it holds (TM-22).
        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            early = await reap(session, now=expires_at + SKEW_ALLOWANCE - timedelta(seconds=1))
        assert lease_id not in early

        leased, _ = await _pool_state(migrated_engine, mandate_id)
        assert leased == LEASE_SIZE, "an early reap must leave the pool untouched"

        # Past the margin, it comes back without anyone having asked.
        async with factory() as session:
            reclaimed = await reap(session, now=expires_at + SKEW_ALLOWANCE + timedelta(seconds=1))
        assert lease_id in reclaimed

        leased, available = await _pool_state(migrated_engine, mandate_id)
        assert leased == Decimal(0)
        assert available == POOL_TOTAL, "the full pool must be spendable again"

        state, _ = await _lease_state(migrated_engine, lease_id)
        assert state == LeaseState.EXPIRED.value

    async def test_the_stranded_window_is_the_documented_bound(self) -> None:
        """Spec 04 §7: worst case `ttl + S + ttl/4`, which is 80 s with the defaults.

        Not a behavioural test — an arithmetic one, so the number quoted in the spec, the
        threat model and the pitch cannot drift from the parameters that produce it.
        """
        reap_interval = TTL / 4
        worst_case = TTL + SKEW_ALLOWANCE + reap_interval
        assert worst_case == timedelta(seconds=80)
