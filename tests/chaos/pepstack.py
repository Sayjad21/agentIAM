"""A wired PEP a chaos scenario can start, fault, and restart — T-052.

Everything a request passes through on the way to a verdict is real: a root biscuit, the
T-018 gateway, the T-020 extractor, the T-019 pipeline, the T-024 Cedar engine, the T-021
lease pool against Postgres, the T-022 emitter writing the real audit chain, and the M4
stub tools behind it. The shape is `tests/e2e/test_thin_slice.py`'s `Slice`, rebuilt here
rather than imported because chaos needs three things that fixture cannot give:

* **Its own ledger URL per instance.** CH-4 hands one PEP a partitioned address while the
  invariant checker keeps a direct one — one shared engine would make the partition
  invisible to the thing being partitioned, or blind the checker along with it.
* **A restart.** CH-10 tears an instance down and builds another with the same identity,
  which means the whole object graph has to be constructible more than once.
* **Several at a time.** CH-3 and CH-10 both need three PEPs against one pool, each with
  its own lease, which is also what makes `leased` a sum worth checking.

**The clock is frozen**, as it is in the e2e slice. That is not laziness: it separates the
fault under test from lease expiry. CH-1 is about a ledger that cannot be reached, and if
wall-clock time were also running, a refusal thirty seconds in would have two candidate
causes and the scenario would prove neither. CH-3 does the opposite deliberately — it
advances an *injected* `now` into the reaper, which is how it observes reclamation without
waiting sixty seconds for it.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from agentiam_controlplane.db.audit_sink import LedgerAuditSink
from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import acquire, release
from agentiam_controlplane.db.models import BudgetRow
from agentiam_controlplane.db.settlement_sink import LedgerSettlementSink
from agentiam_core.hashing import hash_object
from agentiam_core.models import Budget, BudgetDimension, Mandate
from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root
from agentiam_demo.tools import create_tools_app
from agentiam_pep.app import create_app
from agentiam_pep.config import PepSettings
from agentiam_pep.drift import RuleBasedDriftOracle
from agentiam_pep.emitter import DecisionEmitter, EmitterSettings
from agentiam_pep.extractor import RouteTable
from agentiam_pep.pipeline import Pipeline, PipelineSettings
from agentiam_pep.policy import AgentPrincipal, CedarEngine, PolicyBundle, ToolFacts
from agentiam_pep.pool import LeaseGrant, LeasePool, PoolSettings
from agentiam_pep.revocation import InMemoryRevocationSet
from agentiam_pep.settlement import SettlementQueue, SettlementSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from agentiam_core.decision import DriftOracle
    from agentiam_core.tokens import VerifiedToken

__all__ = [
    "KEY_SET",
    "NOW",
    "PepStack",
    "PoolLedgerClient",
    "a_mandate",
    "available",
    "build_stack",
    "make_pool_budget",
]

#: The frozen clock every stack reads. See the module docstring for why.
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

_ROOT = generate_keypair()
KEY_SET = RootKeySet([_ROOT.public_key])

_ROUTES: dict[str, Any] = {
    "routes": [
        {
            "method": "GET",
            "path": "/invoices/{id}",
            "scope": "invoice:read",
            "tool": "invoice_api",
            "args": {"invoice.id": "path.id"},
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
        },
    ],
    "default": {"action": "deny"},
}

_POLICY = """
permit(principal, action == Action::"invoice:read", resource);
permit(principal, action == Action::"payment:initiate", resource)
when { context.amount.lessThanOrEqual(decimal("100000.0")) };
"""

_TOOLS = {
    "invoice_api": ToolFacts(tool_id="invoice_api", server="erp", sensitivity="low"),
    "payment_api": ToolFacts(
        tool_id="payment_api", server="bank", sensitivity="high", is_external=True
    ),
}


def a_mandate(
    mandate_id: uuid.UUID, *, total: Decimal, task_intent_text: str | None = None
) -> Mandate:
    """The mandate every chaos scenario authorizes against.

    `task_intent_text` binds the mandate to the intent a scenario will send in the
    `AgentIAM-Task-Intent` header. It matters for CH-8 and nowhere else: the pipeline
    derives `request_intent` from that header when present (spec 06 §1.1), so a mandate
    minted with an arbitrary `intent_hash` is refused with `INTENT_MISMATCH` at step 4 —
    before drift is consulted at step 6. CH-8's first run did exactly that and reported
    12 denials from an "unreachable advisory oracle" the oracle had never been asked.
    """
    return Mandate(
        mandate_id=mandate_id,
        task_id=uuid.uuid4(),
        principal_id="kc:alice",
        intent_hash="a" * 64 if task_intent_text is None else hash_object(task_intent_text),
        scopes=frozenset({"invoice:read", "payment:initiate"}),
        budget=Budget(spend_bdt=total, tool_calls=1000),
        max_depth=4,
        not_before=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=8),
    )


async def make_pool_budget(
    engine: AsyncEngine, *, mandate_id: uuid.UUID, total: Decimal
) -> uuid.UUID:
    """Create the mandate's spend pool row directly, as `tests/integration` does."""
    factory = make_session_factory(engine)
    async with factory() as session, session.begin():
        row = BudgetRow(
            mandate_id=mandate_id, dimension=BudgetDimension.SPEND_BDT.value, total=total
        )
        session.add(row)
        await session.flush()
        return row.id


async def available(engine: AsyncEngine, mandate_id: uuid.UUID) -> tuple[Decimal, Decimal, Decimal]:
    """`(committed, leased, available)` for the mandate's spend pool."""
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
        return row.committed, row.leased, row.total - row.committed - row.leased


class PoolLedgerClient:
    """The real `ACQUIRE`/`RELEASE`, over whatever URL this instance was handed.

    Holds its own engine so a scenario can point one PEP through a fault proxy while
    everything else keeps a direct connection.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Bind to `engine`."""
        self._factory = make_session_factory(engine)
        self.acquire_failures = 0
        self.release_failures = 0

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
        """Take a lease, or report the ledger unreachable by returning None.

        A failed `ACQUIRE` is counted and swallowed rather than raised, because that is what
        the pool's contract already assumes: `_acquire` treats `None` as *the ledger has
        nothing left*, and a top-up runs in a background task whose exception nobody reads.
        Letting it propagate would make a partition look like a crashed task, which is a
        different failure with a different fix.
        """
        try:
            async with self._factory() as session:
                lease = await acquire(
                    session,
                    mandate_id=mandate_id,
                    dimension=dimension.value,
                    requested=requested,
                    pep_id=pep_id,
                    ttl=ttl,
                    now=now,
                )
        except Exception:
            self.acquire_failures += 1
            return None
        if lease is None or lease.granted <= 0:
            return None
        return LeaseGrant(id=lease.id, granted=lease.granted, expires_at=lease.expires_at)

    async def release(self, *, lease_id: uuid.UUID) -> None:
        """Return a lease, counting the failure if the ledger cannot be reached."""
        try:
            async with self._factory() as session:
                await release(session, lease_id=lease_id)
        except Exception:
            self.release_failures += 1


@dataclass
class PepStack:
    """One wired PEP instance, plus the handles a scenario needs to look inside it."""

    pep_id: str
    client: httpx.AsyncClient
    pool: LeasePool
    emitter: DecisionEmitter
    ledger: PoolLedgerClient
    engine: AsyncEngine
    mandate: Mandate
    tools_client: httpx.AsyncClient
    settlement: SettlementQueue
    #: How many shutdown steps hit their timeout. Non-zero is CH-4's finding, not noise.
    shutdown_timeouts: int = 0

    @property
    def auth(self) -> dict[str, str]:
        """The bearer header for this stack's root token."""
        return {"authorization": f"Bearer {self.token}"}

    token: str = ""

    async def pay(self, amount: Decimal, **headers: str) -> tuple[int, str | None]:
        """Post one payment. Returns `(status, reason_code)` — the shape `drive_load` wants."""
        response = await self.client.post(
            "/proxy/payments",
            headers={**self.auth, **headers},
            json={"amount": str(amount), "recipient": {"account_id": "acct_1001"}},
        )
        reason = None
        if response.status_code != 200:
            try:
                reason = str(response.json().get("reason_code"))
            except ValueError:
                reason = "unparseable"
        return response.status_code, reason

    async def read_invoice(self, invoice_id: str = "inv_001") -> tuple[int, str | None]:
        """Read one invoice — a zero-spend request, so it exercises everything but budget."""
        response = await self.client.get(f"/proxy/invoices/{invoice_id}", headers=self.auth)
        reason = None
        if response.status_code != 200:
            try:
                reason = str(response.json().get("reason_code"))
            except ValueError:
                reason = "unparseable"
        return response.status_code, reason

    def remaining(self) -> Decimal:
        """This PEP's local view of what it can still spend."""
        return self.pool.remaining(BudgetDimension.SPEND_BDT)

    async def aclose(self, *, graceful: bool = True, timeout: float = 20.0) -> None:
        """Shut the instance down.

        `graceful=False` skips the lease `RELEASE` and the emitter flush — the in-process
        stand-in for a process that was killed. It is *not* as strong as CH-3's real
        `Popen.kill()`, and no scenario uses it in place of that.

        **Every step is bounded, the bound does not cancel, and the engine is disposed
        without closing.** All three are scars from CH-4.

        `AsyncEngine.dispose()` asks asyncpg to close each pooled connection *gracefully*,
        which means waiting for the server to answer — and a connection that spent the
        scenario behind a black hole has no server to answer it. `dispose(close=False)`
        drops the pool and lets the sockets be collected, which is the right trade for a
        process that is about to end anyway.

        The bound is `asyncio.wait` on a task rather than `asyncio.wait_for`, and that is
        not a stylistic preference. **`wait_for` does not bound anything here**: it cancels
        the coroutine, the cancellation lands inside SQLAlchemy's greenlet bridge while
        asyncpg is blocked on a partitioned socket, and the driver's own cleanup — rollback,
        then close — needs the same dead socket to finish. Measured: a five-second
        `wait_for` around `LeasePool.aclose()` was still stuck five minutes later. So the
        step is left pending instead of cancelled; healing the partition is what finally
        releases it, and the count of steps that did not finish is data, not an error.
        """
        if graceful:
            # **Settlement first, and the order is load-bearing.** `RELEASE` returns
            # `granted - settled` to the pool, so a lease released while settlements are
            # still queued hands back budget that was already spent — the exact double-spend
            # T-052 found. Flushing before the pool closes is what makes the release exact.
            for coro in (self.settlement.aclose(), self.pool.aclose(), self.emitter.aclose()):
                task = asyncio.ensure_future(coro)
                done, _pending = await asyncio.wait({task}, timeout=timeout)
                if not done:
                    # A partitioned pool genuinely cannot drain (CH-4). Recording that and
                    # moving on beats hanging, and cancelling would not help — see above.
                    self.shutdown_timeouts += 1
        await self.client.aclose()
        await self.tools_client.aclose()
        await self.engine.dispose(close=False)


async def build_stack(
    *,
    ledger_url: str,
    mandate: Mandate,
    pep_id: str,
    lease_size: Decimal,
    drift_oracle: DriftOracle | None = None,
    emitter_capacity: int = 4096,
    flush_interval_s: float = 0.05,
    prime: bool = True,
    engine_kwargs: dict[str, Any] | None = None,
) -> PepStack:
    """Wire one PEP against `ledger_url` and take its first lease.

    Args:
        ledger_url: The DSN this instance's ledger client uses. Point it at a fault proxy
            to partition this PEP alone.
        mandate: The mandate to authorize against; its `mandate_id` names the pool.
        pep_id: This instance's identity, which is what `leases.pep_id` records.
        lease_size: How much this PEP asks for per `ACQUIRE`.
        drift_oracle: Wired into step 6. `None` leaves drift unconsulted; CH-8 supplies a
            real `RuleBasedDriftOracle` pointed at a dead Ollama.
        emitter_capacity: Audit buffer size. CH-1 sizes this deliberately — a buffer that
            fills during an outage starts denying (ADR-026), which is a *different*
            fail-closed path from the lease running out.
        flush_interval_s: Drain cadence.
        prime: Whether to take the first lease now.
        engine_kwargs: Passed to `make_engine`.

    Returns:
        The started stack. The caller closes it.
    """
    engine = make_engine(ledger_url, **(engine_kwargs or {}))
    ledger = PoolLedgerClient(engine)

    # Spec 04 §4.4. Without this the PEP settles only in its own memory and `RELEASE` hands
    # spent budget back to the pool — the gap CH-10 measured before it was wired. Built
    # before the pool because the pool takes `before_release` from it.
    settlement = SettlementQueue(
        LedgerSettlementSink(make_session_factory(engine)),
        SettlementSettings(flush_interval_s=flush_interval_s),
        now=lambda: NOW,
    )
    await settlement.start()

    pool = LeasePool(
        ledger,
        PoolSettings(pep_id=pep_id, lease_size=lease_size),
        mandate_id=mandate.mandate_id,
        now=lambda: NOW,
        # Settle before releasing, on top-ups as well as on shutdown: a released lease
        # rejects every settlement still owed against it (spec 04 §11) and hands the
        # already-spent budget back to the pool. See `LeasePool._release`.
        before_release=settlement.drain,
    )
    if prime:
        await pool.prime(BudgetDimension.SPEND_BDT)

    emitter = DecisionEmitter(
        LedgerAuditSink(make_session_factory(engine)),
        EmitterSettings(capacity=emitter_capacity, flush_interval_s=flush_interval_s),
    )
    await emitter.start()

    def principal_for(token: VerifiedToken) -> AgentPrincipal:
        return AgentPrincipal(
            agent_id="agt-procurement",
            role="worker",
            principal_id=token.principal_id,
            task_id=token.task_id,
        )

    pipeline = Pipeline(
        routes=RouteTable.from_config(_ROUTES),
        key_set=KEY_SET,
        policy=CedarEngine(
            PolicyBundle(version="bundle-chaos", cedar_source=_POLICY), tools=_TOOLS
        ),
        principal_for=principal_for,
        pool=pool,
        emitter=emitter,
        revocation=InMemoryRevocationSet(),
        settings=PipelineSettings(pep_id=pep_id),
        now=lambda: NOW,
        drift_oracle=drift_oracle,
        settlement=settlement,
    )

    tools_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_tools_app()), base_url="http://tools"
    )
    app = create_app(
        settings=PepSettings(upstream_base_url="http://tools"),
        upstream_client=tools_client,
        pipeline=pipeline,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{pep_id}")
    return PepStack(
        pep_id=pep_id,
        client=client,
        pool=pool,
        emitter=emitter,
        ledger=ledger,
        engine=engine,
        mandate=mandate,
        tools_client=tools_client,
        settlement=settlement,
        token=mint_root(mandate, _ROOT.private_key),
    )


def drift_oracle_against(base_url: str, *, timeout: float = 2.0) -> RuleBasedDriftOracle:
    """A real drift oracle pointed at `base_url` — CH-8 points it at nothing."""
    return RuleBasedDriftOracle(base_url=base_url, timeout=timeout)
