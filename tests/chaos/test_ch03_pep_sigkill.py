"""CH-3 — one PEP of three killed without warning (T-052, `PLAN.md` §13.2).

*Expected: its lease strands ≤ TTL then reclaims; others unaffected.*

Three **real child processes** take three real leases against one pool. One is killed with
no chance to clean up. `tests/integration/test_lease_pool_crash.py` (T-021, ADR-025) proved
the single-PEP version of this; CH-3 is the multi-tenant one, and the second half of the
expectation — *others unaffected* — is the half that needs three of them to mean anything.

**`signal.SIGKILL` is not used**, for the reason T-021 recorded: `hasattr(signal, "SIGKILL")`
is False on win32, so a test written against it imports fine in CI and fails on the
development host. `Popen.kill()` is the portable spelling — `SIGKILL` on POSIX,
`TerminateProcess` on Windows — and neither lets the child clean up, which is the point.

**The survivors are given a much longer TTL than the doomed holder.** Not cosmetic: with
three leases acquired within a second of each other and the same TTL, they expire together,
and a reap past that moment reclaims all three. The assertion *the other two were untouched*
would then be checking nothing. The doomed lease expires first by construction, so the reap
that reclaims it is the same reap that must leave the others alone.

The most interesting thing here is what does **not** happen. A stranded lease looks like
missing money, and it is tempting to expect the invariant checker to light up. It must not:
`leased` still equals the outstanding total of *active* leases, because the dead PEP's lease
is still active and still counted. Stranded is not the same as unaccounted, and the sidecar
sweeping throughout is what turns that from a claim into an observation.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.ledger import SKEW_ALLOWANCE, reap
from agentiam_controlplane.db.models import BudgetRow, LeaseRow
from agentiam_core.models import BudgetDimension, LeaseState
from tests.chaos.harness import chaos_run
from tests.chaos.pepstack import NOW, make_pool_budget

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.chaos

POOL_TOTAL = Decimal("1000.0000")
LEASE_SIZE = Decimal("200.0000")
DOOMED_TTL_S = 60
SURVIVOR_TTL_S = 3600

#: Run in a child process: take a lease, announce it, then heartbeat off a real query so
#: the parent can see that a *surviving* holder is still talking to the ledger after its
#: neighbour was killed. `PLAN.md`'s "others unaffected" is otherwise an assumption.
HOLDER = """
import asyncio, sys, uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import acquire


async def main() -> None:
    url, mandate_id, requested, pep_id, ttl_s, now_iso = (
        sys.argv[1], uuid.UUID(sys.argv[2]), Decimal(sys.argv[3]),
        sys.argv[4], int(sys.argv[5]), sys.argv[6],
    )
    now = datetime.fromisoformat(now_iso)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    async with factory() as session:
        lease = await acquire(
            session,
            mandate_id=mandate_id,
            dimension="spend_bdt",
            requested=requested,
            pep_id=pep_id,
            ttl=timedelta(seconds=ttl_s),
            now=now,
        )
    print(f"READY {lease.id}", flush=True)
    beat = 0
    while True:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        beat += 1
        print(f"BEAT {beat}", flush=True)
        await asyncio.sleep(0.25)


asyncio.run(main())
"""


class Holder:
    """A child process holding one real lease."""

    def __init__(self, process: subprocess.Popen[str], pep_id: str) -> None:
        """Wrap a started child."""
        self.process = process
        self.pep_id = pep_id
        self.lease_id: uuid.UUID | None = None

    async def _line(self, timeout: float) -> str:
        assert self.process.stdout is not None
        return await asyncio.wait_for(
            asyncio.to_thread(self.process.stdout.readline), timeout=timeout
        )

    async def wait_ready(self, timeout: float = 180.0) -> uuid.UUID:
        """Block until the child announces its lease id."""
        line = await self._line(timeout)
        if not line.startswith("READY "):
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise AssertionError(f"{self.pep_id} never acquired a lease: {line!r} {stderr}")
        self.lease_id = uuid.UUID(line.split()[1])
        return self.lease_id

    async def beats(self, count: int, timeout: float = 30.0) -> int:
        """Read `count` heartbeats, proving the child is alive and reaching the ledger."""
        seen = 0
        for _ in range(count):
            line = await self._line(timeout)
            if line.startswith("BEAT "):
                seen += 1
        return seen

    def kill(self) -> None:
        """No warning, no cleanup."""
        self.process.kill()

    def stop(self) -> None:
        """Tear the child down at the end of the scenario."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=30)


def _spawn(script: Path, url: str, mandate_id: uuid.UUID, pep_id: str, ttl_s: int) -> Holder:
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(script),
            url,
            str(mandate_id),
            str(LEASE_SIZE),
            pep_id,
            str(ttl_s),
            NOW.isoformat(),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return Holder(process, pep_id)


async def _leased(engine: AsyncEngine, mandate_id: uuid.UUID) -> Decimal:
    factory = make_session_factory(engine)
    async with factory() as session:
        row = (
            await session.execute(
                select(BudgetRow).where(
                    BudgetRow.mandate_id == mandate_id,
                    BudgetRow.dimension == BudgetDimension.SPEND_BDT.value,
                    BudgetRow.agent_id.is_(None),
                )
            )
        ).scalar_one()
        return row.leased


async def _lease(engine: AsyncEngine, lease_id: uuid.UUID) -> LeaseRow:
    factory = make_session_factory(engine)
    async with factory() as session:
        return (await session.execute(select(LeaseRow).where(LeaseRow.id == lease_id))).scalar_one()


class TestOnePepOfThreeKilled:
    async def test_the_dead_peps_lease_strands_then_reclaims_and_the_others_do_not(
        self, migrated_engine: AsyncEngine, postgres_url: str, tmp_path: Path
    ) -> None:
        mandate_id = uuid.uuid4()
        await make_pool_budget(migrated_engine, mandate_id=mandate_id, total=POOL_TOTAL)

        script = tmp_path / "holder.py"
        script.write_text(HOLDER, encoding="utf-8")

        holders = [
            _spawn(script, postgres_url, mandate_id, "pep-alpha", SURVIVOR_TTL_S),
            _spawn(script, postgres_url, mandate_id, "pep-doomed", DOOMED_TTL_S),
            _spawn(script, postgres_url, mandate_id, "pep-gamma", SURVIVOR_TTL_S),
        ]
        alpha, doomed, gamma = holders

        try:
            async with chaos_run(
                "CH-03",
                title="SIGKILL one PEP of three",
                expected="its lease strands <= TTL then reclaims; others unaffected",
                engine=migrated_engine,
            ) as run:
                for holder in holders:
                    await holder.wait_ready()
                run.event("three leases held")

                assert doomed.lease_id is not None
                doomed_lease = await _lease(migrated_engine, doomed.lease_id)
                run.measure("lease_size", LEASE_SIZE)
                run.measure("leased_with_three_holders", await _leased(migrated_engine, mandate_id))
                assert await _leased(migrated_engine, mandate_id) == LEASE_SIZE * 3

                # --- the kill ----------------------------------------------------------
                run.event("killing pep-doomed")
                doomed.kill()
                doomed.process.wait(timeout=30)
                assert doomed.process.returncode != 0, "kill() must not look like a clean exit"
                run.event(f"pep-doomed exited {doomed.process.returncode}")

                # --- others unaffected -------------------------------------------------
                alpha_beats = await alpha.beats(4)
                gamma_beats = await gamma.beats(4)
                run.measure("survivor_heartbeats_after_kill", alpha_beats + gamma_beats)
                assert alpha_beats == 4, "pep-alpha stopped reaching the ledger"
                assert gamma_beats == 4, "pep-gamma stopped reaching the ledger"

                # --- stranded, and *correctly* stranded ---------------------------------
                stranded = await _leased(migrated_engine, mandate_id)
                run.measure("leased_while_stranded", stranded)
                assert stranded == LEASE_SIZE * 3, (
                    "the dead PEP's budget must still be counted against the pool — nothing "
                    "released it, and pretending otherwise would double-issue it"
                )
                assert (await _lease(migrated_engine, doomed.lease_id)).state == (
                    LeaseState.ACTIVE.value
                ), "a killed PEP cannot have released anything"

                held = await run.sidecar.sweep()
                run.measure("invariant_held_while_stranded", held)
                assert held, (
                    "a stranded lease is not a broken book: `leased` still equals the "
                    "outstanding total of active leases, and the checker must agree"
                )

                # --- the reap ----------------------------------------------------------
                factory = make_session_factory(migrated_engine)
                early = doomed_lease.expires_at + SKEW_ALLOWANCE - timedelta(seconds=1)
                async with factory() as session:
                    reclaimed_early = await reap(session, now=early)
                run.event("reaped one second inside the skew margin")
                assert doomed.lease_id not in reclaimed_early, (
                    "reclaiming inside the skew margin re-issues budget a lagging PEP still "
                    "believes it holds (TM-22)"
                )
                assert await _leased(migrated_engine, mandate_id) == LEASE_SIZE * 3

                late = doomed_lease.expires_at + SKEW_ALLOWANCE + timedelta(seconds=1)
                async with factory() as session:
                    reclaimed = await reap(session, now=late)
                run.event("reaped past the skew margin")
                assert doomed.lease_id in reclaimed, "the stranded lease was never reclaimed"

                after = await _leased(migrated_engine, mandate_id)
                run.measure("leased_after_reap", after)
                assert after == LEASE_SIZE * 2, (
                    f"expected only the dead PEP's {LEASE_SIZE} back, got {stranded - after}"
                )

                for survivor in (alpha, gamma):
                    assert survivor.lease_id is not None
                    row = await _lease(migrated_engine, survivor.lease_id)
                    assert row.state == LeaseState.ACTIVE.value, (
                        f"{survivor.pep_id}'s lease was reclaimed by a reap aimed at "
                        f"someone else — over-reclamation is as much a bug as stranding"
                    )
                assert (await _lease(migrated_engine, doomed.lease_id)).state == (
                    LeaseState.EXPIRED.value
                )

                # Both survivors are still serving after a reap ran against the same pool.
                assert await alpha.beats(2) == 2
                assert await gamma.beats(2) == 2

                run.measure(
                    "documented_worst_case_strand_s",
                    (
                        timedelta(seconds=DOOMED_TTL_S)
                        + SKEW_ALLOWANCE
                        + timedelta(seconds=DOOMED_TTL_S) / 4
                    ).total_seconds(),
                )
                run.note(
                    "Reclamation is observed by advancing an injected `now` into `reap()`, "
                    "not by sleeping for the TTL. The bound under test is the ledger's "
                    "`expires_at + S`, which is a value, not a wall-clock wait."
                )
        finally:
            for holder in holders:
                holder.stop()

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
        assert run.sidecar.samples_held > 0, "no sweep ever completed"
