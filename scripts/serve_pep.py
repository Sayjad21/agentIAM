"""Run a real PEP on a real port — T-053, and the entrypoint the project did not have.

Every assembly of the PEP so far has lived in a test: `tests/e2e/test_thin_slice.py` wires
it for the thin slice, `tests/chaos/pepstack.py` wires it for the chaos scenarios, and
`create_app()` takes an already-built `Pipeline` because until now nothing outside a test
ever built one. That is fine for correctness — both of those are the real object graph —
but NFR-2 is *"end-to-end PEP proxy overhead p99 < 8 ms at 500 RPS"*, and a load generator
needs a socket.

So this is the composition root. It is also what the flame graph attaches to (a profiler
needs a process), and it is the shape T-056's deployment artifacts will want.

**It seeds as well as serves, and that is deliberate.** A load profile needs a mandate that
exists, a budget row with money in it, and a bearer token signed by a key the server
accepts. Generating those in one place, in the same process that will serve them, removes
the class of load-test failure where the harness is measuring 401s at four thousand
requests a second and reporting it as throughput. `--seed` writes the token where the
locustfile can find it and prints what it created.

The root keypair is generated per run unless `AGENTIAM_ROOT_PRIVATE_KEY` is set. That is
right for a load test — the token and the key are born together and die with the process —
and wrong for anything else, which is why there is no default that outlives the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from biscuit_auth import PrivateKey
    from fastapi import FastAPI

    from agentiam_core.tokens import RootKeySet, VerifiedToken

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]

#: Matches `docker-compose.yml`, so `make up` then this script just works.
DEFAULT_DATABASE_URL: Final = "postgresql+asyncpg://agentiam:agentiam@localhost:5432/agentiam"

#: Where `--seed` leaves the bearer token and mandate id for the load generator.
DEFAULT_PROFILE_PATH: Final = _REPO_ROOT / "docs" / "benchmarks" / ".perf-profile.json"

#: The route table the perf profile drives. Two routes: one that spends and one that does
#: not, so a profile can separate "the whole pipeline" from "the whole pipeline plus the
#: lease pool" without changing servers.
ROUTES: Final[dict[str, Any]] = {
    "routes": [
        {
            "method": "GET",
            "path": "/invoices/{id}",
            "scope": "invoice:read",
            "tool": "invoice_api",
            "args": {"invoice.id": "path.id"},
            "drift_mode": "off",
        },
        {
            "method": "POST",
            "path": "/payments",
            "scope": "payment:initiate",
            "tool": "payment_api",
            "args": {
                "payment.amount": "body.amount:number",
                "payment.to": "body.recipient.account_id",
            },
            "drift_mode": "off",
        },
    ],
    "default": {"action": "deny"},
}

POLICY: Final = """
permit(principal, action == Action::"invoice:read", resource);
permit(principal, action == Action::"payment:initiate", resource)
when { context.amount.lessThanOrEqual(decimal("100000.0")) };
"""

#: Large enough that a 500 RPS run cannot exhaust it and start measuring refusals.
POOL_TOTAL: Final = Decimal("100000000.0000")
LEASE_SIZE: Final = Decimal("1000000.0000")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without binding a port."""
    parser = argparse.ArgumentParser(
        prog="serve_pep", description="Run a real PEP for load testing and profiling."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--database-url",
        default=None,
        help="Ledger DSN. Defaults to $DATABASE_URL, then the compose defaults.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help=(
            "Create the mandate's budget row and mint a bearer token before serving, "
            "writing both to --profile for the load generator to read."
        ),
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help=(
            "Drop decision records instead of writing them. Measures the pipeline without "
            "the audit chain's write amplification — reported as a separate profile, never "
            "as the headline number."
        ),
    )
    parser.add_argument(
        "--no-enforce",
        action="store_true",
        help=(
            "Serve as the T-018 transport with no pipeline attached. Isolates what the "
            "proxy hop costs from what enforcement costs — NFR-2 minus this is AgentIAM's "
            "own overhead, and the rest is TCP."
        ),
    )
    parser.add_argument(
        "--upstream",
        default=None,
        help="Upstream base URL. Omitted means the in-process stub tools (T-004).",
    )
    return parser


def resolve_database_url(explicit: str | None) -> str:
    """Pick the DSN: the flag, then the environment, then the compose default."""
    return explicit or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


async def _seed(database_url: str, mandate_id: uuid.UUID) -> None:
    """Create the mandate's spend pool. Idempotent enough for repeated runs."""
    from sqlalchemy import select

    from agentiam_controlplane.db.base import make_engine, make_session_factory
    from agentiam_controlplane.db.models import BudgetRow
    from agentiam_core.models import BudgetDimension

    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    async with factory() as session, session.begin():
        existing = (
            await session.execute(
                select(BudgetRow).where(
                    BudgetRow.mandate_id == mandate_id,
                    BudgetRow.dimension == BudgetDimension.SPEND_BDT.value,
                    BudgetRow.agent_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                BudgetRow(
                    mandate_id=mandate_id,
                    dimension=BudgetDimension.SPEND_BDT.value,
                    total=POOL_TOTAL,
                )
            )
    await engine.dispose()


def build_app(
    *,
    database_url: str,
    mandate_id: uuid.UUID,
    private_key: PrivateKey,
    key_set: RootKeySet,
    upstream: str | None,
    drop_audit: bool,
    enforce: bool = True,
) -> tuple[FastAPI, str]:
    """Wire the whole PEP and return `(app, bearer_token)`.

    The same object graph the e2e slice and the chaos stacks build, assembled from
    configuration rather than from a fixture. Imports are local so `--help` does not pay
    for the whole dependency tree.
    """
    import httpx

    from agentiam_controlplane.db.audit_sink import LedgerAuditSink
    from agentiam_controlplane.db.base import make_engine, make_session_factory
    from agentiam_controlplane.db.ledger import acquire, release
    from agentiam_controlplane.db.settlement_sink import LedgerSettlementSink
    from agentiam_core.hashing import hash_object
    from agentiam_core.models import Budget, BudgetDimension, Mandate
    from agentiam_core.tokens import mint_root
    from agentiam_demo.tools import create_tools_app
    from agentiam_pep.app import create_app
    from agentiam_pep.config import PepSettings
    from agentiam_pep.emitter import DecisionEmitter, EmitterSettings
    from agentiam_pep.extractor import RouteTable
    from agentiam_pep.pipeline import Pipeline, PipelineSettings
    from agentiam_pep.policy import AgentPrincipal, CedarEngine, PolicyBundle, ToolFacts
    from agentiam_pep.pool import LeaseGrant, LeasePool, PoolSettings
    from agentiam_pep.revocation import InMemoryRevocationSet
    from agentiam_pep.settlement import SettlementQueue, SettlementSettings

    now = datetime.now(UTC)
    mandate = Mandate(
        mandate_id=mandate_id,
        task_id=uuid.uuid4(),
        principal_id="kc:perf",
        # A fixed, valid SHA-256: the perf profile sends no intent header, so
        # `request_intent` falls back to the token's own hash and matches (spec 09 §4).
        intent_hash=hash_object("agentiam-load-profile"),
        scopes=frozenset({"invoice:read", "payment:initiate"}),
        budget=Budget(spend_bdt=POOL_TOTAL, tool_calls=10_000_000),
        max_depth=4,
        not_before=now - timedelta(minutes=5),
        expires_at=now + timedelta(days=1),
    )

    engine = make_engine(database_url)
    factory = make_session_factory(engine)

    class Ledger:
        """The real `ACQUIRE`/`RELEASE`, over the configured DSN."""

        async def acquire(
            self,
            *,
            mandate_id: uuid.UUID,
            dimension: BudgetDimension,
            requested: Decimal,
            pep_id: str,
            ttl: timedelta,
            now: datetime,
        ) -> LeaseGrant | None:
            async with factory() as session:
                lease = await acquire(
                    session,
                    mandate_id=mandate_id,
                    dimension=dimension.value,
                    requested=requested,
                    pep_id=pep_id,
                    ttl=ttl,
                    now=now,
                )
            if lease is None or lease.granted <= 0:
                return None
            return LeaseGrant(id=lease.id, granted=lease.granted, expires_at=lease.expires_at)

        async def release(self, *, lease_id: uuid.UUID) -> None:
            async with factory() as session:
                await release(session, lease_id=lease_id)

    class DroppingSink:
        """Counts records and writes nothing — `--no-audit`."""

        def __init__(self) -> None:
            self.count = 0

        async def write(self, batch: Sequence[object]) -> None:
            self.count += len(batch)

    settlement = SettlementQueue(
        LedgerSettlementSink(factory), SettlementSettings(), now=lambda: datetime.now(UTC)
    )
    pool = LeasePool(
        Ledger(),
        PoolSettings(pep_id="pep-perf", lease_size=LEASE_SIZE),
        mandate_id=mandate_id,
        now=lambda: datetime.now(UTC),
        before_release=settlement.drain,
    )
    emitter = DecisionEmitter(
        DroppingSink() if drop_audit else LedgerAuditSink(factory),
        EmitterSettings(capacity=16384),
    )

    def principal_for(token: VerifiedToken) -> AgentPrincipal:
        return AgentPrincipal(
            agent_id="agt-perf",
            role="worker",
            principal_id=token.principal_id,
            task_id=token.task_id,
        )

    pipeline = Pipeline(
        routes=RouteTable.from_config(ROUTES),
        key_set=key_set,
        policy=CedarEngine(
            PolicyBundle(version="bundle-perf", cedar_source=POLICY),
            tools={
                "invoice_api": ToolFacts(tool_id="invoice_api", server="erp", sensitivity="low"),
                "payment_api": ToolFacts(
                    tool_id="payment_api", server="bank", sensitivity="high", is_external=True
                ),
            },
        ),
        principal_for=principal_for,
        pool=pool,
        emitter=emitter,
        revocation=InMemoryRevocationSet(),
        settings=PipelineSettings(pep_id="pep-perf"),
        now=lambda: datetime.now(UTC),
        settlement=settlement,
    )

    if upstream:
        upstream_client = None
        base_url = upstream
    else:
        # The stub tools in-process: a load test of the *PEP* must not be bounded by
        # whatever is behind it, and NFR-2 is explicitly the proxy's own overhead.
        upstream_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_tools_app()), base_url="http://tools"
        )
        base_url = "http://tools"

    app = create_app(
        settings=PepSettings(upstream_base_url=base_url),
        upstream_client=upstream_client,
        # `None` is T-018 transport mode: the same proxy, the same streaming, the same
        # header hygiene, with no decision in the path. The difference between the two is
        # exactly what enforcement costs.
        pipeline=pipeline if enforce else None,
    )

    @app.on_event("startup")
    async def _start() -> None:
        if not enforce:
            return
        await emitter.start()
        await settlement.start()
        await pool.prime(BudgetDimension.SPEND_BDT)

    @app.on_event("shutdown")
    async def _stop() -> None:
        if not enforce:
            return
        await settlement.aclose()
        await pool.aclose()
        await emitter.aclose()

    return app, mint_root(mandate, private_key)


def main(argv: Sequence[str] | None = None) -> int:
    """Seed if asked, then serve. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    import uvicorn

    from agentiam_core.tokens import RootKeySet, generate_keypair

    database_url = resolve_database_url(args.database_url)
    mandate_id = uuid.uuid4()
    root = generate_keypair()
    key_set = RootKeySet([root.public_key])

    if args.seed:
        asyncio.run(_seed(database_url, mandate_id))

    app, token = build_app(
        database_url=database_url,
        mandate_id=mandate_id,
        private_key=root.private_key,
        key_set=key_set,
        upstream=args.upstream,
        drop_audit=args.no_audit,
        enforce=not args.no_enforce,
    )

    args.profile.parent.mkdir(parents=True, exist_ok=True)
    args.profile.write_text(
        json.dumps(
            {
                "base_url": f"http://{args.host}:{args.port}",
                "mandate_id": str(mandate_id),
                "token": token,
                "audit": not args.no_audit,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"PEP on http://{args.host}:{args.port} — profile written to {args.profile}")
    print(
        f"  mandate {mandate_id}, audit {'off' if args.no_audit else 'on'}, "
        f"enforcing {'off' if args.no_enforce else 'on'}"
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
