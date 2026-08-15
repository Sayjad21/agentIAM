"""The end-to-end thin slice — T-023, M4's exit gate.

`PLAN.md` T-023: *an agent with a root token calls a stub tool through the PEP, spends budget,
is denied when exhausted, and the decision appears in the audit ledger.*

Every layer is real. A root biscuit, the T-018 gateway, the T-020 extractor, the T-019
pipeline, the T-024 Cedar engine, the T-021 lease pool against Postgres, the T-022 emitter
writing to the real audit chain, and the M4 stub tools behind it. Nothing is a fake except the
network, which is an in-process ASGI transport so the test does not need free ports.

This is the first point at which the project's one sentence is demonstrable rather than
asserted, and `ROADMAP.md` says to record a screencast here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import select

from agentiam_controlplane.db.audit import custody, verify_chain
from agentiam_controlplane.db.audit_sink import LedgerAuditSink
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.ledger import acquire
from agentiam_controlplane.db.models import AuditRecordRow, BudgetRow
from agentiam_core.errors import ReasonCode
from agentiam_core.models import Budget, BudgetDimension, Mandate
from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root
from agentiam_demo.tools import create_tools_app
from agentiam_pep.app import create_app
from agentiam_pep.config import PepSettings
from agentiam_pep.emitter import DecisionEmitter, EmitterSettings
from agentiam_pep.extractor import RouteTable
from agentiam_pep.pipeline import Pipeline, PipelineSettings
from agentiam_pep.policy import AgentPrincipal, CedarEngine, PolicyBundle, ToolFacts
from agentiam_pep.pool import LeaseGrant, LeasePool, PoolSettings
from agentiam_pep.revocation import InMemoryRevocationSet
from tests.integration.conftest import make_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from agentiam_core.tokens import VerifiedToken

pytestmark = pytest.mark.e2e

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
ROOT = generate_keypair()
KEY_SET = RootKeySet([ROOT.public_key])

#: The mandate's ceiling, and the pool the PEP leases from.
POOL_TOTAL = Decimal("1000.0000")

ROUTES: dict[str, object] = {
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

POLICY = """
permit(principal, action == Action::"invoice:read", resource);
permit(principal, action == Action::"payment:initiate", resource)
when { context.amount.lessThanOrEqual(decimal("100000.0")) };
"""

TOOLS = {
    "invoice_api": ToolFacts(tool_id="invoice_api", server="erp", sensitivity="low"),
    "payment_api": ToolFacts(
        tool_id="payment_api", server="bank", sensitivity="high", is_external=True
    ),
}


class LedgerBackedClient:
    """The `LedgerClient` the pool acquires from — the real `ACQUIRE`, over real Postgres."""

    def __init__(self, engine: AsyncEngine, mandate_id: uuid.UUID) -> None:
        """Bind a session factory to the mandate this PEP leases against."""
        self._factory = make_session_factory(engine)
        self._mandate_id = mandate_id

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
        if lease is None or lease.granted <= 0:
            return None
        return LeaseGrant(id=lease.id, granted=lease.granted, expires_at=lease.expires_at)

    async def release(self, *, lease_id: uuid.UUID) -> None:
        from agentiam_controlplane.db.ledger import release

        async with self._factory() as session:
            await release(session, lease_id=lease_id)


def a_mandate(mandate_id: uuid.UUID, **over: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": mandate_id,
        "task_id": uuid.uuid4(),
        "principal_id": "kc:alice",
        "intent_hash": "a" * 64,
        "scopes": frozenset({"invoice:read", "payment:initiate"}),
        "budget": Budget(spend_bdt=POOL_TOTAL, tool_calls=100),
        "max_depth": 4,
        "not_before": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=1),
    }
    return Mandate(**(base | over))  # type: ignore[arg-type]


class Slice:
    """Everything wired together, plus the handles a test needs to look inside."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        emitter: DecisionEmitter,
        pool: LeasePool,
        mandate: Mandate,
        engine: AsyncEngine,
        revocation: InMemoryRevocationSet,
    ) -> None:
        """Hold every handle a test needs to look inside the wired slice."""
        self.client = client
        self.emitter = emitter
        self.pool = pool
        self.mandate = mandate
        self.engine = engine
        self.revocation = revocation
        self.token = mint_root(mandate, ROOT.private_key)

    @property
    def auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    async def settled(self) -> None:
        """Let the emitter drain, so the audit assertions see the records."""
        await self.emitter.flush()

    async def available(self) -> Decimal:
        factory = make_session_factory(self.engine)
        async with factory() as session:
            row = (
                await session.execute(
                    select(BudgetRow).where(
                        BudgetRow.mandate_id == self.mandate.mandate_id,
                        BudgetRow.dimension == BudgetDimension.SPEND_BDT.value,
                        BudgetRow.agent_id.is_(None),
                    )
                )
            ).scalar_one()
            return row.total - row.committed - row.leased


@pytest.fixture
async def slice_(migrated_engine: AsyncEngine) -> AsyncIterator[Slice]:
    mandate_id = uuid.uuid4()
    await make_budget(
        migrated_engine,
        mandate_id=mandate_id,
        dimension=BudgetDimension.SPEND_BDT.value,
        total=POOL_TOTAL,
    )
    mandate = a_mandate(mandate_id)

    pool = LeasePool(
        LedgerBackedClient(migrated_engine, mandate_id),
        PoolSettings(pep_id="pep-e2e", lease_size=Decimal("400.0000")),
        mandate_id=mandate_id,
        now=lambda: NOW,
    )
    await pool.prime(BudgetDimension.SPEND_BDT)

    emitter = DecisionEmitter(
        LedgerAuditSink(make_session_factory(migrated_engine)),
        EmitterSettings(capacity=256, flush_interval_s=0.01),
    )
    await emitter.start()

    revocation = InMemoryRevocationSet()

    def principal_for(token: VerifiedToken) -> AgentPrincipal:
        return AgentPrincipal(
            agent_id="agt-procurement",
            role="worker",
            principal_id=token.principal_id,
            task_id=token.task_id,
        )

    from agentiam_pep.drift import RuleBasedDriftOracle

    pipeline = Pipeline(
        routes=RouteTable.from_config(ROUTES),
        key_set=KEY_SET,
        policy=CedarEngine(PolicyBundle(version="bundle-e2e", cedar_source=POLICY), tools=TOOLS),
        principal_for=principal_for,
        pool=pool,
        emitter=emitter,
        revocation=revocation,
        settings=PipelineSettings(pep_id="pep-e2e"),
        now=lambda: NOW,
        drift_oracle=RuleBasedDriftOracle(),
    )

    tools_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_tools_app()), base_url="http://tools"
    )
    app = create_app(
        settings=PepSettings(upstream_base_url="http://tools"),
        upstream_client=tools_client,
        pipeline=pipeline,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://pep"
    ) as client:
        yield Slice(client, emitter, pool, mandate, migrated_engine, revocation)

    await emitter.aclose()
    await tools_client.aclose()


class TestTheSlice:
    """`PLAN.md` T-023's acceptance criterion, sentence by sentence."""

    async def test_readyz_reports_enforcing(self, slice_: Slice) -> None:
        """`STATUS.md` gap 11 closes here, and the flag is derived from the wiring."""
        assert (await slice_.client.get("/readyz")).json()["enforcing"] is True

    async def test_an_agent_with_a_root_token_calls_a_stub_tool(self, slice_: Slice) -> None:
        response = await slice_.client.get("/proxy/invoices/inv_001", headers=slice_.auth)
        assert response.status_code == 200
        assert response.json()["id"] == "inv_001"

    async def test_a_call_without_a_token_never_reaches_the_tool(self, slice_: Slice) -> None:
        response = await slice_.client.get("/proxy/invoices/inv_001")
        assert response.status_code == 401
        assert response.json()["reason_code"] == ReasonCode.MALFORMED_REQUEST.value

    async def test_spending_budget_moves_the_ledger(self, slice_: Slice) -> None:
        """The lease was drawn at prime; spending draws down the PEP's local remainder."""
        before = slice_.pool.remaining(BudgetDimension.SPEND_BDT)
        response = await slice_.client.post(
            "/proxy/payments",
            headers=slice_.auth,
            json={"amount": "250.0000", "recipient": {"account_id": "acct_1001"}},
        )
        assert response.status_code == 200
        assert slice_.pool.remaining(BudgetDimension.SPEND_BDT) == before - Decimal(250)

    async def test_it_is_denied_when_the_budget_is_exhausted(self, slice_: Slice) -> None:
        """The headline beat: spend until it stops, and the refusal names why."""
        spent = Decimal(0)
        last: httpx.Response | None = None
        for _ in range(10):
            last = await slice_.client.post(
                "/proxy/payments",
                headers=slice_.auth,
                json={"amount": "90.0000", "recipient": {"account_id": "acct_1001"}},
            )
            if last.status_code != 200:
                break
            spent += Decimal(90)

        assert last is not None
        assert last.status_code == 429, f"expected exhaustion, got {last.status_code}"
        assert last.json()["reason_code"] in {
            ReasonCode.LEASE_UNAVAILABLE.value,
            ReasonCode.BUDGET_EXHAUSTED_MANDATE.value,
        }
        assert spent > 0, "nothing was spent before the refusal"

    async def test_the_denial_never_reached_the_tool(self, slice_: Slice) -> None:
        """A policy denial that still calls the tool is not a policy."""
        response = await slice_.client.post(
            "/proxy/payments",
            headers=slice_.auth,
            json={"amount": "500000.0000", "recipient": {"account_id": "acct_1001"}},
        )
        assert response.status_code == 403
        assert "payment_id" not in response.text

    async def test_the_decision_appears_in_the_audit_ledger(self, slice_: Slice) -> None:
        await slice_.client.get("/proxy/invoices/inv_001", headers=slice_.auth)
        await slice_.settled()

        factory = make_session_factory(slice_.engine)
        async with factory() as session:
            rows = (await session.execute(select(AuditRecordRow))).scalars().all()
            result = await verify_chain(session)

        assert len(rows) >= 1
        assert result.ok, f"the chain is broken: {result}"

    async def test_the_chain_of_custody_answers_who_authorized_this(self, slice_: Slice) -> None:
        """The demo's closing question, asked of the real ledger."""
        await slice_.client.get("/proxy/invoices/inv_001", headers=slice_.auth)
        await slice_.client.post(
            "/proxy/payments",
            headers=slice_.auth,
            json={"amount": "10.0000", "recipient": {"account_id": "acct_1001"}},
        )
        await slice_.settled()

        factory = make_session_factory(slice_.engine)
        async with factory() as session:
            entries = await custody(session, task_id=slice_.mandate.task_id)

        assert [e.scope for e in entries] == ["invoice:read", "payment:initiate"]
        assert all(e.agent_id == "agt-procurement" for e in entries)

    async def test_denials_are_recorded_too(self, slice_: Slice) -> None:
        """An unaudited denial is how *we never saw that request* happens."""
        await slice_.client.post(
            "/proxy/payments",
            headers=slice_.auth,
            json={"amount": "500000.0000", "recipient": {"account_id": "acct_1001"}},
        )
        await slice_.settled()

        factory = make_session_factory(slice_.engine)
        async with factory() as session:
            entries = await custody(session, task_id=slice_.mandate.task_id)

        assert len(entries) == 1
        assert entries[0].outcome == "deny"

    async def test_a_revoked_token_stops_working(self, slice_: Slice) -> None:
        """Demo Beat 7's mechanism, with the set updated in place."""
        from agentiam_core.tokens import verify

        verified = verify(slice_.token, KEY_SET, now=NOW)
        assert (
            await slice_.client.get("/proxy/invoices/inv_001", headers=slice_.auth)
        ).status_code == 200

        for revocation_id in verified.revocation_ids:
            slice_.revocation.revoke(revocation_id)

        response = await slice_.client.get("/proxy/invoices/inv_001", headers=slice_.auth)
        assert response.status_code == 401
        assert response.json()["reason_code"] == ReasonCode.TOKEN_REVOKED.value

    async def test_an_unmapped_route_is_refused(self, slice_: Slice) -> None:
        response = await slice_.client.get("/proxy/admin/keys", headers=slice_.auth)
        assert response.status_code == 401
        assert response.json()["reason_code"] == ReasonCode.MALFORMED_REQUEST.value

    async def test_the_audit_record_carries_no_argument_values(self, slice_: Slice) -> None:
        """NFR-5 all the way through: the account id must not reach the ledger."""
        await slice_.client.post(
            "/proxy/payments",
            headers=slice_.auth,
            json={"amount": "10.0000", "recipient": {"account_id": "acct_secret_1001"}},
        )
        await slice_.settled()

        factory = make_session_factory(slice_.engine)
        async with factory() as session:
            rows = (await session.execute(select(AuditRecordRow))).scalars().all()

        assert rows
        assert all("acct_secret_1001" not in str(row.record) for row in rows)
