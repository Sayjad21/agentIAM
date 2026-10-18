"""Measure AgentIAM against the designs it replaces — APICTA-08, `make bench-compare`.

Writes `docs/benchmarks/comparative.json` and renders `comparative.md` from it
(`generate_comparative_results.py`, which also explains what each comparison is). Starts its
own Postgres on an ephemeral port, as `run_load_test.py` does, so a run needs Docker and
nothing else — plus a configured LLM backend for the third comparison, which is reported as
not measured rather than assumed when there is none.

The three measurements are functions so the integration suite can run them small against a
real database (`tests/integration/test_comparative_benchmarks.py`), checking that each row
measures what it says: the exchange baseline reads the database per call, B1 makes one ledger
round trip per call, and neither budget design spends more than the pool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import statistics
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine
    from starlette.applications import Starlette

    from agentiam_controlplane.db.settlement_sink import PendingSettlement
    from agentiam_pep.pool import LeaseGrant

_BENCH: Final = _REPO_ROOT / "docs" / "benchmarks"
_RESULTS: Final = _BENCH / "comparative.json"
_PB2: Final = _BENCH / "pb2-breakdown.json"

#: How long a B3 call may wait for a lease top-up before it counts as refused. Far above any
#: top-up measured here; it exists so a wedged pool ends the run instead of hanging it.
_TOPUP_PATIENCE_S: Final = 5.0


def _stats(samples_us: list[float]) -> dict[str, Any]:
    """Median and p99, never a mean (`PLAN.md` §13.1)."""
    if not samples_us:
        return {"n": 0, "median_us": 0.0, "p99_us": 0.0}
    ordered = sorted(samples_us)
    p99 = ordered[min(len(ordered) - 1, round(0.99 * (len(ordered) - 1)))]
    return {
        "n": len(ordered),
        "median_us": round(statistics.median(ordered), 1),
        "p99_us": round(p99, 1),
    }


def _host() -> dict[str, Any]:
    from scripts.run_load_test import _host as host

    return host()


# --------------------------------------------------------------------------- 1. attenuation


class _ExchangeServer:
    """A token endpoint on loopback that does one primary-key lookup per call.

    Runs on its own thread and event loop, so the client's timing loop does not also pay for
    the server's work — a real authorization server is another process too.
    """

    def __init__(self, database_url: str, mandate_id: uuid.UUID) -> None:
        """Prepare the app; `start` binds it."""
        self.database_url = database_url
        self.mandate_id = mandate_id
        self.lookups = 0
        self.port = 0
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def _app(self) -> Starlette:
        from sqlalchemy import select
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        from agentiam_controlplane.db.base import make_engine, make_session_factory
        from agentiam_controlplane.db.models import MandateRow

        engine = make_engine(self.database_url)
        factory = make_session_factory(engine)

        async def token(request: Request) -> JSONResponse:
            form = await request.form()
            async with factory() as session:
                row = (
                    await session.execute(
                        select(MandateRow).where(MandateRow.mandate_id == self.mandate_id)
                    )
                ).scalar_one_or_none()
            self.lookups += 1
            if row is None or not form.get("subject_token"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(
                {
                    "access_token": secrets.token_urlsafe(32),
                    "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "token_type": "Bearer",
                    "scope": str(form.get("scope", "")),
                }
            )

        return Starlette(routes=[Route("/token", token, methods=["POST"])])

    def start(self) -> None:
        import socket

        import uvicorn

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        config = uvicorn.Config(
            self._app(), host="127.0.0.1", port=self.port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.perf_counter() + 30
        while not self._server.started:
            if time.perf_counter() > deadline:
                raise RuntimeError("the token-exchange endpoint never started")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=30)


async def _seed_mandate(database_url: str, mandate_id: uuid.UUID) -> None:
    from agentiam_controlplane.db.base import make_engine, make_session_factory
    from agentiam_controlplane.db.mandates import ensure_row

    engine = make_engine(database_url)
    async with make_session_factory(engine)() as session, session.begin():
        await ensure_row(session, mandate_id, now=datetime.now(UTC))
    await engine.dispose()


async def measure_attenuation(database_url: str, *, n: int) -> dict[str, Any]:
    """`attenuate()` in process, and a simulated token exchange, `n` timed calls each."""
    import httpx

    from agentiam_core.attenuation import attenuate
    from agentiam_core.hashing import hash_object
    from agentiam_core.models import Budget, Mandate, ScopeSubset
    from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root, verify

    now = datetime.now(UTC)
    mandate_id = uuid.uuid4()
    root = generate_keypair()
    key_set = RootKeySet([root.public_key])
    mandate = Mandate(
        mandate_id=mandate_id,
        task_id=uuid.uuid4(),
        principal_id="kc:bench",
        intent_hash=hash_object("comparative-benchmark"),
        scopes=frozenset({"invoice:read", "payment:initiate"}),
        budget=Budget(spend_bdt=Decimal(1000)),
        max_depth=4,
        not_before=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
    )
    parent_text = mint_root(mandate, root.private_key)
    parent = verify(parent_text, key_set, now=now)
    narrower = [ScopeSubset(scopes=frozenset({"invoice:read"}))]

    child = attenuate(parent, narrower, agent_id="agt-child", role="reader")
    child_verifies = verify(child, key_set, now=now).depth == parent.depth + 1
    in_process: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        attenuate(parent, narrower, agent_id="agt-child", role="reader")
        in_process.append((time.perf_counter() - started) * 1e6)

    await _seed_mandate(database_url, mandate_id)
    server = _ExchangeServer(database_url, mandate_id)
    server.start()
    exchange: list[float] = []
    try:
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": parent_text,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": "invoice:read",
        }
        url = f"http://127.0.0.1:{server.port}/token"
        async with httpx.AsyncClient(timeout=10.0) as client:
            (await client.post(url, data=form)).raise_for_status()  # warm-up, untimed
            for _ in range(n):
                started = time.perf_counter()
                response = await client.post(url, data=form)
                exchange.append((time.perf_counter() - started) * 1e6)
                response.raise_for_status()
    finally:
        server.stop()

    return {
        "in_process": {**_stats(in_process), "child_verifies": child_verifies},
        "token_exchange": {**_stats(exchange), "lookups": server.lookups},
    }


# --------------------------------------------------------------------------- 2. budget


async def _pool_row(engine: AsyncEngine, mandate_id: uuid.UUID, total: Decimal) -> None:
    from agentiam_controlplane.db.base import make_session_factory
    from agentiam_controlplane.db.mandates import ensure_row
    from agentiam_controlplane.db.models import BudgetRow
    from agentiam_core.models import BudgetDimension

    async with make_session_factory(engine)() as session, session.begin():
        await ensure_row(session, mandate_id, now=datetime.now(UTC))
        session.add(
            BudgetRow(mandate_id=mandate_id, dimension=BudgetDimension.SPEND_BDT.value, total=total)
        )


async def _books(engine: AsyncEngine, mandate_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """`(overspend, committed)`: how far committed + leased exceeds the total, and committed.

    Overspend alone cannot see the failure that matters most here. A settlement the ledger
    rejects leaves `committed` *below* what was spent, so the books stay inside the pool while
    the unrecorded money goes back to it as available — so `committed` is returned too, and
    each design reports `unrecorded = spent - committed`.
    """
    from sqlalchemy import select

    from agentiam_controlplane.db.base import make_session_factory
    from agentiam_controlplane.db.models import BudgetRow

    async with make_session_factory(engine)() as session:
        row = (
            await session.execute(select(BudgetRow).where(BudgetRow.mandate_id == mandate_id))
        ).scalar_one()
    return max(Decimal(0), row.committed + row.leased - row.total), row.committed


async def _b1(
    database_url: str, *, peps: int, calls: int, amount: Decimal, total: Decimal
) -> dict[str, Any]:
    """One ledger transaction per call: lock the pool row, check, debit, commit."""
    from sqlalchemy import select

    from agentiam_controlplane.db.base import make_engine, make_session_factory
    from agentiam_controlplane.db.models import BudgetRow

    mandate_id = uuid.uuid4()
    admin = make_engine(database_url)
    await _pool_row(admin, mandate_id, total)
    engines = [make_engine(database_url) for _ in range(peps)]
    latencies: list[float] = []
    refused = 0
    trips = 0

    async def pep(engine: AsyncEngine) -> None:
        nonlocal refused, trips
        factory = make_session_factory(engine)
        for _ in range(calls):
            started = time.perf_counter()
            async with factory() as session, session.begin():
                row = (
                    await session.execute(
                        select(BudgetRow)
                        .where(BudgetRow.mandate_id == mandate_id)
                        .with_for_update()
                    )
                ).scalar_one()
                allowed = row.total - row.committed - row.leased >= amount
                if allowed:
                    row.committed += amount
            trips += 1
            if allowed:
                latencies.append((time.perf_counter() - started) * 1e6)
            else:
                refused += 1

    wall = time.perf_counter()
    await asyncio.gather(*(pep(engine) for engine in engines))
    wall = time.perf_counter() - wall
    overspend, committed = await _books(admin, mandate_id)
    for engine in [*engines, admin]:
        await engine.dispose()
    return {
        **_stats(latencies),
        "calls_per_s": round(len(latencies) / wall, 1) if wall else 0.0,
        "ledger_round_trips": trips,
        "refused": refused,
        "overspend": f"{overspend:.4f}",
        "spent": f"{amount * len(latencies):.4f}",
        "committed": f"{committed:.4f}",
    }


async def _b3(
    database_url: str,
    *,
    peps: int,
    calls: int,
    amount: Decimal,
    total: Decimal,
    lease_size: Decimal,
) -> dict[str, Any]:
    """AgentIAM: each PEP reserves from its own lease in memory, composed as deployed.

    Every allowed call does what `Pipeline.settle` does — commit the reservation locally and
    enqueue it for `LEDGER_COMMIT` — and each pool drains its settlements before any release
    (ADR-049). The first version of this function reserved and never settled, and measured a
    ৳300 overspend on a ৳300 pool: a top-up released the old lease, and the ledger took back
    money that had been reserved but never committed. That is ADR-049's double spend, and it
    is why the comparison has to use the deployed composition rather than `reserve()` alone.
    """
    from agentiam_controlplane.db.base import make_engine, make_session_factory
    from agentiam_controlplane.db.settlement_sink import LedgerSettlementSink
    from agentiam_core.models import BudgetDimension
    from agentiam_pep.pool import LeasePool, PoolSettings
    from agentiam_pep.settlement import SettlementQueue, SettlementSettings
    from scripts.pep_service import SessionLedger

    trips = [0]

    class CountingLedger(SessionLedger):
        """The deployed ACQUIRE/RELEASE client, counting every round trip of this run.

        The deployed one rather than the chaos harness's `PoolLedgerClient`: on an empty pool
        the ledger raises `LeaseUnavailableError`, which `SessionLedger` turns into the `None`
        that marks the mandate spent, and `PoolLedgerClient` re-raised as an outage. Measured
        with it: 43,085 ledger round trips and every refused call waiting out the full 5 s,
        a top-up storm the deployed PEP does not have.
        """

        async def acquire(self, **kwargs: Any) -> LeaseGrant | None:  # noqa: ANN401
            trips[0] += 1
            return await super().acquire(**kwargs)

        async def release(self, *, lease_id: uuid.UUID) -> None:
            trips[0] += 1
            await super().release(lease_id=lease_id)

    class CountingSink(LedgerSettlementSink):
        """The deployed `LEDGER_COMMIT` sink; one batch is one round trip."""

        async def commit(self, batch: Sequence[PendingSettlement]) -> Sequence[bool]:
            trips[0] += 1
            return await super().commit(batch)

    mandate_id = uuid.uuid4()
    admin = make_engine(database_url)
    await _pool_row(admin, mandate_id, total)
    engines = [make_engine(database_url) for _ in range(peps)]
    settlements = [
        SettlementQueue(
            CountingSink(make_session_factory(engine)),
            SettlementSettings(),
            now=lambda: datetime.now(UTC),
        )
        for engine in engines
    ]
    pools = [
        LeasePool(
            CountingLedger(make_session_factory(engine)),
            PoolSettings(pep_id=f"pep-bench-{i}", lease_size=lease_size),
            mandate_id=mandate_id,
            now=lambda: datetime.now(UTC),
            before_release=settlement.drain,
        )
        for i, (engine, settlement) in enumerate(zip(engines, settlements, strict=True))
    ]
    for settlement in settlements:
        await settlement.start()
    for pool in pools:
        await pool.prime(BudgetDimension.SPEND_BDT)
    request = {BudgetDimension.SPEND_BDT: amount}
    latencies: list[float] = []
    refused = 0
    waits = 0

    async def pep(pool: LeasePool, settlement: SettlementQueue) -> None:
        nonlocal refused, waits
        for _ in range(calls):
            started = time.perf_counter()
            while True:
                verdict = pool.check(request)
                if verdict.ok:
                    reservation = pool.reserve(BudgetDimension.SPEND_BDT, amount)
                    outcome = pool.commit(BudgetDimension.SPEND_BDT, reservation, amount)
                    settlement.enqueue(outcome, now=datetime.now(UTC))
                    latencies.append((time.perf_counter() - started) * 1e6)
                    break
                if verdict.mandate_exhausted or (time.perf_counter() - started > _TOPUP_PATIENCE_S):
                    refused += 1
                    break
                waits += 1
                # `check()` has scheduled the top-up as it refused (T-052); let it land.
                await asyncio.sleep(0.001)
            # A closed loop that never yields would starve the other PEPs' top-ups — and B1's
            # loop yields at every `await`, so this is what keeps the two loops comparable.
            await asyncio.sleep(0)

    wall = time.perf_counter()
    await asyncio.gather(*(pep(p, s) for p, s in zip(pools, settlements, strict=True)))
    wall = time.perf_counter() - wall
    # The shutdown order `serve_pep.py` uses: settlements first, then the leases.
    for settlement in settlements:
        await settlement.aclose()
    for pool in pools:
        await pool.aclose()
    ledger_overspend, committed = await _books(admin, mandate_id)
    overspend = max(ledger_overspend, amount * len(latencies) - total)
    for engine in [*engines, admin]:
        await engine.dispose()
    return {
        **_stats(latencies),
        "calls_per_s": round(len(latencies) / wall, 1) if wall else 0.0,
        "ledger_round_trips": trips[0],
        "refused": refused,
        "waits": waits,
        "overspend": f"{max(Decimal(0), overspend):.4f}",
        "spent": f"{amount * len(latencies):.4f}",
        "committed": f"{committed:.4f}",
    }


async def measure_budget(
    database_url: str,
    *,
    peps: int,
    calls_per_pep: int,
    amount: Decimal,
    lease_size: Decimal,
    pool_total: Decimal | None = None,
) -> dict[str, Any]:
    """B1 and B3 against the same Postgres, each on a fresh pool of the same size."""
    total = pool_total if pool_total is not None else amount * peps * calls_per_pep * 2
    return {
        "peps": peps,
        "calls_per_pep": calls_per_pep,
        "amount": f"{amount:.4f}",
        "pool_total": f"{total:.4f}",
        "lease_size": f"{lease_size:.4f}",
        "b1": await _b1(database_url, peps=peps, calls=calls_per_pep, amount=amount, total=total),
        "b3": await _b3(
            database_url,
            peps=peps,
            calls=calls_per_pep,
            amount=amount,
            total=total,
            lease_size=lease_size,
        ),
    }


# --------------------------------------------------------------------------- 3. context


def in_process_us(pb2: Path = _PB2) -> float:
    """Verify + decide + extract medians — `performance.md`'s per-request in-process cost."""
    steps = json.loads(pb2.read_text(encoding="utf-8"))["steps"]
    total = sum(float(steps[name]["median_us"]) for name in ("verify", "decide_total", "extract"))
    return round(total, 1)


#: A demo-agent-shaped request: read what the tool returned, choose the next action.
_PROMPT: Final = (
    "You are an accounts-payable agent. Invoice inv_001 from Padma Supplies is for BDT "
    "12,500 and is open. Choose the next action."
)
_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["pay", "read_vendor", "stop"]},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
}


async def measure_llm(*, n: int) -> dict[str, Any]:
    """Median wall time of `n` calls to the configured backend, or why it could not be had."""
    from agentiam_controlplane.nl_compiler.llm import LLMError, client_from_env

    try:
        client = client_from_env()
    except LLMError as exc:
        return {"status": "unavailable", "reason": str(exc)}
    backend = type(client).__name__.removesuffix("Client").lower()
    timings: list[float] = []
    try:
        await client.warm()
        for _ in range(n):
            started = time.perf_counter()
            await client.generate_structured(_PROMPT, _SCHEMA)
            timings.append((time.perf_counter() - started) * 1000)
    except LLMError as exc:
        return {"status": "unavailable", "reason": f"{backend}: {type(exc).__name__}: {exc}"}
    return {
        "status": "measured",
        "backend": backend,
        "model": str(getattr(client, "model", "unknown")),
        "n": n,
        "median_ms": round(statistics.median(timings), 1),
        "max_ms": round(max(timings), 1),
    }


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without Docker."""
    parser = argparse.ArgumentParser(
        prog="run_comparative_benchmarks", description="AgentIAM against B1 and token exchange."
    )
    parser.add_argument("--attenuations", type=int, default=500, help="Timed calls per side.")
    parser.add_argument("--peps", type=int, default=3)
    parser.add_argument("--calls-per-pep", type=int, default=300)
    parser.add_argument("--amount", type=Decimal, default=Decimal("10.0000"))
    parser.add_argument("--lease-size", type=Decimal, default=Decimal("1000.0000"))
    parser.add_argument("--llm-calls", type=int, default=5)
    parser.add_argument("--skip-llm", action="store_true", help="Record the LLM as not measured.")
    parser.add_argument("--results", type=Path, default=_RESULTS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Measure, write the JSON, render the Markdown. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    from alembic import command
    from alembic.config import Config
    from testcontainers.community.postgres import PostgresContainer

    from scripts import generate_comparative_results

    package_root = _REPO_ROOT / "packages" / "agentiam-controlplane"
    with PostgresContainer(
        image="postgres:16-alpine",
        driver="asyncpg",
        username="agentiam",
        password="agentiam",  # noqa: S106 - throwaway, lives for one run
        dbname="agentiam",
    ) as container:
        database_url = container.get_connection_url()
        cfg = Config(str(package_root / "alembic.ini"))
        cfg.set_main_option(
            "script_location",
            str(package_root / "src" / "agentiam_controlplane" / "db" / "migrations"),
        )
        cfg.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(cfg, "head")

        print("1. attenuation vs simulated token exchange")
        attenuation = asyncio.run(measure_attenuation(database_url, n=args.attenuations))
        print("2. B1 (FOR UPDATE per call) vs B3 (leases)")
        budget = asyncio.run(
            measure_budget(
                database_url,
                peps=args.peps,
                calls_per_pep=args.calls_per_pep,
                amount=args.amount,
                lease_size=args.lease_size,
            )
        )

    print("3. in-process cost vs a measured LLM call")
    llm = (
        {"status": "unavailable", "reason": "skipped with --skip-llm"}
        if args.skip_llm
        else asyncio.run(measure_llm(n=args.llm_calls))
    )
    results = {
        "measured_at": datetime.now(UTC).isoformat(),
        "host": _host(),
        "attenuation": attenuation,
        "budget": budget,
        "context": {"in_process_us": in_process_us(), "llm": llm},
    }
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.results}")
    output = args.results.with_suffix(".md")
    return generate_comparative_results.main(
        ["--source", str(args.results), "--output", str(output)]
    )


if __name__ == "__main__":
    sys.exit(main())
