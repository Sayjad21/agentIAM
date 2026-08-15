"""The ten steps, wired — T-023, spec 09.

`decide()` covers steps 3-7 and has its own 54 scenarios (T-019). This is the wiring: pulling
a request apart, verifying the token, reserving, recording, and turning the outcome into a
status code.

Two classes carry the weight:

* `TestEmitPrecedesForward` — ADR-026 says a full audit buffer denies, which is only true if
  the record is emitted before the side effect. Emitting afterwards would make the policy
  theatre, and nothing else in the suite would notice.
* `TestStatusMapping` — spec 09 §11, including that an unmapped reason code raises rather than
  defaulting to 500.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from agentiam_core.errors import ReasonCode
from agentiam_core.models import (
    Budget,
    BudgetDimension,
    DecisionRecord,
    Mandate,
    Outcome,
    ScopeSubset,
)
from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root
from agentiam_pep.app import create_app
from agentiam_pep.config import PepSettings
from agentiam_pep.emitter import BackPressure, DecisionEmitter, EmitterSettings
from agentiam_pep.errors import ReservationInsufficientError
from agentiam_pep.extractor import RouteTable
from agentiam_pep.pipeline import (
    Authorized,
    Pipeline,
    PipelineSettings,
    Refused,
    status_for,
)
from agentiam_pep.policy import AgentPrincipal, CedarEngine, PolicyBundle, ToolFacts
from agentiam_pep.pool import LeaseGrant, LeasePool, PoolSettings
from agentiam_pep.revocation import InMemoryRevocationSet

if TYPE_CHECKING:
    from agentiam_core.tokens import VerifiedToken

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
ROOT = generate_keypair()
KEY_SET = RootKeySet([ROOT.public_key])

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
    "invoice_api": ToolFacts(tool_id="invoice_api", sensitivity="low"),
    "payment_api": ToolFacts(tool_id="payment_api", sensitivity="high", is_external=True),
}


class CollectingSink:
    def __init__(self) -> None:
        """Start empty."""
        self.records: list[object] = []

    async def write(self, batch: object) -> None:
        self.records.extend(batch)  # type: ignore[arg-type]


class FakeLedger:
    def __init__(self, granted: Decimal = Decimal(1000)) -> None:
        """Grant a fixed amount on every acquire."""
        self.granted = granted

    async def acquire(self, **kw: object) -> LeaseGrant:
        return LeaseGrant(
            id=uuid.uuid4(), granted=self.granted, expires_at=NOW + timedelta(seconds=60)
        )

    async def release(self, **kw: object) -> None:
        return None


def a_mandate(**over: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "principal_id": "kc:alice",
        "intent_hash": "a" * 64,
        "scopes": frozenset({"invoice:read", "payment:initiate"}),
        "budget": Budget(spend_bdt=Decimal(500000), tool_calls=100),
        "max_depth": 4,
        "not_before": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=30),
    }
    return Mandate(**(base | over))  # type: ignore[arg-type]


def principal_for(token: VerifiedToken) -> AgentPrincipal:
    return AgentPrincipal(
        agent_id="agt-1",
        role="worker",
        principal_id=token.principal_id,
        task_id=token.task_id,
    )


async def a_pipeline(
    *,
    emitter: DecisionEmitter | None = None,
    revoked: list[str] | None = None,
    policy_source: str = POLICY,
    granted: Decimal = Decimal(1000),
) -> tuple[Pipeline, CollectingSink, LeasePool]:
    sink = CollectingSink()
    pool = LeasePool(
        FakeLedger(granted),
        PoolSettings(pep_id="pep-1", lease_size=granted),
        mandate_id=uuid.uuid4(),
        now=lambda: NOW,
    )
    await pool.prime(BudgetDimension.SPEND_BDT)
    pipeline = Pipeline(
        routes=RouteTable.from_config(ROUTES),
        key_set=KEY_SET,
        policy=CedarEngine(
            PolicyBundle(version="bundle-1", cedar_source=policy_source), tools=TOOLS
        ),
        principal_for=principal_for,
        pool=pool,
        emitter=emitter or DecisionEmitter(sink, EmitterSettings(capacity=64)),
        revocation=InMemoryRevocationSet(revoked or []),
        settings=PipelineSettings(pep_id="pep-1"),
        now=lambda: NOW,
    )
    return pipeline, sink, pool


def bearer(mandate: Mandate) -> list[tuple[str, str]]:
    return [("authorization", f"Bearer {mint_root(mandate, ROOT.private_key)}")]


class TestHappyPath:
    async def test_a_valid_read_is_authorized(self) -> None:
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        assert isinstance(result, Authorized)
        assert result.extraction.scope == "invoice:read"

    async def test_a_payment_reserves_budget(self) -> None:
        pipeline, _, pool = await a_pipeline()
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "250.0000", "recipient": {"account_id": "acct_1"}}',
        )
        assert isinstance(result, Authorized)
        assert result.reservation is not None
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before - Decimal(250)

    async def test_settling_returns_an_overestimate(self) -> None:
        pipeline, _, pool = await a_pipeline()
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "250.0000", "recipient": {"account_id": "acct_1"}}',
        )
        assert isinstance(result, Authorized)
        pipeline.settle(result, actual=Decimal(100))
        assert pool.remaining(BudgetDimension.SPEND_BDT) == Decimal(900)

    async def test_a_read_reserves_nothing(self) -> None:
        pipeline, _, pool = await a_pipeline()
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        assert isinstance(result, Authorized)
        assert result.reservation is None
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before


class TestRefusals:
    async def test_no_token_is_401(self) -> None:
        """EC-T01."""
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(method="GET", path="/invoices/inv_001")
        assert isinstance(result, Refused)
        assert result.status == 401

    async def test_an_unmapped_route_is_401_malformed(self) -> None:
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(
            method="GET", path="/admin/keys", headers=bearer(a_mandate())
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.MALFORMED_REQUEST

    async def test_an_expired_token_is_401(self) -> None:
        pipeline, _, _ = await a_pipeline()
        expired = a_mandate(
            not_before=NOW - timedelta(hours=2), expires_at=NOW - timedelta(hours=1)
        )
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(expired)
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.TOKEN_EXPIRED
        assert result.status == 401

    async def test_a_scope_the_mandate_lacks_is_403(self) -> None:
        pipeline, _, _ = await a_pipeline()
        narrow = a_mandate(scopes=frozenset({"vendor:read"}))
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(narrow)
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.SCOPE_NOT_GRANTED
        assert result.status == 403

    async def test_a_policy_denial_is_403(self) -> None:
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "999999.0000", "recipient": {"account_id": "acct_1"}}',
        )
        assert isinstance(result, Refused)
        assert result.status == 403

    async def test_a_revoked_token_is_401(self) -> None:
        mandate = a_mandate()
        token_text = mint_root(mandate, ROOT.private_key)
        from agentiam_core.tokens import verify

        verified = verify(token_text, KEY_SET, now=NOW)
        pipeline, _, _ = await a_pipeline(revoked=list(verified.revocation_ids))

        result = await pipeline.authorize(
            method="GET",
            path="/invoices/inv_001",
            headers=[("authorization", f"Bearer {token_text}")],
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.TOKEN_REVOKED
        assert result.status == 401

    async def test_exhausted_budget_is_429(self) -> None:
        pipeline, _, _ = await a_pipeline(granted=Decimal(10))
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "500.0000", "recipient": {"account_id": "acct_1"}}',
        )
        assert isinstance(result, Refused)
        assert result.status == 429

    async def test_every_refusal_carries_a_decision_id(self) -> None:
        """Spec 09 §11.3 — *why was I denied?* must be answerable from the client's side."""
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(method="GET", path="/invoices/inv_001")
        assert isinstance(result, Refused)
        body = result.body()
        assert body["decision_id"]
        assert body["reason_code"] == result.reason_code.value


class TestRecording:
    async def test_an_allow_is_recorded(self) -> None:
        pipeline, sink, _ = await a_pipeline()
        emitter = pipeline._emitter
        await emitter.start()
        await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        await emitter.flush()
        assert len(sink.records) == 1

    async def test_a_denial_is_recorded_too(self) -> None:
        """An unaudited denial is how *we never saw that request* happens."""
        pipeline, sink, _ = await a_pipeline()
        emitter = pipeline._emitter
        await emitter.start()
        narrow = a_mandate(scopes=frozenset({"vendor:read"}))
        await pipeline.authorize(method="GET", path="/invoices/inv_001", headers=bearer(narrow))
        await emitter.flush()
        assert len(sink.records) == 1

    async def test_the_record_carries_the_digest_not_the_arguments(self) -> None:
        pipeline, sink, _ = await a_pipeline()
        emitter = pipeline._emitter
        await emitter.start()
        await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "250.0000", "recipient": {"account_id": "acct_secret"}}',
        )
        await emitter.flush()
        record = sink.records[0]
        assert len(record.arg_digest) == 64  # type: ignore[attr-defined]
        assert "acct_secret" not in record.model_dump_json()  # type: ignore[attr-defined]

    async def test_a_refusal_before_verification_is_not_recorded(self) -> None:
        """No token means no principal, no task, no depth — nothing to write a record about.

        Stated because it is a real gap rather than an oversight: a flood of unauthenticated
        requests is a load-balancer problem, and inventing a record with empty identity
        fields would put unattributable rows in a chain of custody.
        """
        pipeline, sink, _ = await a_pipeline()
        emitter = pipeline._emitter
        await emitter.start()
        await pipeline.authorize(method="GET", path="/invoices/inv_001")
        await emitter.flush()
        assert sink.records == []


class TestEmitPrecedesForward:
    """ADR-026's policy is only real if the record is written before the side effect."""

    async def test_a_full_buffer_refuses_the_request(self) -> None:
        sink = CollectingSink()
        emitter = DecisionEmitter(
            sink, EmitterSettings(capacity=1, back_pressure=BackPressure.DENY)
        )
        pipeline, _, _ = await a_pipeline(emitter=emitter)

        first = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        second = await pipeline.authorize(
            method="GET", path="/invoices/inv_002", headers=bearer(a_mandate())
        )

        assert isinstance(first, Authorized)
        assert isinstance(second, Refused)
        assert second.reason_code is ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED
        assert second.status == 503

    async def test_a_refused_request_gives_its_reservation_back(self) -> None:
        """Otherwise a full audit buffer would leak budget on every refusal."""
        sink = CollectingSink()
        emitter = DecisionEmitter(
            sink, EmitterSettings(capacity=1, back_pressure=BackPressure.DENY)
        )
        pipeline, _, pool = await a_pipeline(emitter=emitter)

        await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "250.0000", "recipient": {"account_id": "acct_1"}}',
        )

        assert isinstance(result, Refused)
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before, "budget was not returned"


class TestStatusMapping:
    """Spec 09 §11."""

    @pytest.mark.parametrize(
        ("reason", "status"),
        [
            (ReasonCode.MALFORMED_REQUEST, 401),
            (ReasonCode.TOKEN_EXPIRED, 401),
            (ReasonCode.TOKEN_REVOKED, 401),
            (ReasonCode.SCOPE_NOT_GRANTED, 403),
            (ReasonCode.POLICY_DENIED, 403),
            (ReasonCode.APPROVAL_REQUIRED, 403),
            (ReasonCode.BUDGET_EXHAUSTED_MANDATE, 429),
            (ReasonCode.LEASE_UNAVAILABLE, 429),
            (ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED, 503),
            (ReasonCode.POLICY_BUNDLE_STALE, 503),
            (ReasonCode.VERIFICATION_LIMIT_EXCEEDED, 503),
            (ReasonCode.UPSTREAM_ERROR, 502),
        ],
    )
    def test_the_documented_mapping(self, reason: ReasonCode, status: int) -> None:
        assert status_for(reason) == status

    def test_every_reason_code_except_ok_has_a_status(self) -> None:
        """A code with no status would ship as a silent 500."""
        missing = [
            code.value
            for code in ReasonCode
            if code is not ReasonCode.OK and code not in _mapping()
        ]
        assert not missing, f"no HTTP status defined for {missing}"

    def test_an_unmapped_code_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(KeyError):
            status_for(ReasonCode.OK)


def _mapping() -> dict[ReasonCode, int]:
    from agentiam_pep.pipeline import _STATUS

    return _STATUS


class TestIntent:
    async def test_an_asserted_intent_that_matches_is_allowed(self) -> None:
        pipeline, _, _ = await a_pipeline()
        mandate = a_mandate()
        result = await pipeline.authorize(
            method="GET",
            path="/invoices/inv_001",
            headers=[*bearer(mandate), ("x-agentiam-intent", mandate.intent_hash)],
        )
        assert isinstance(result, Authorized)

    async def test_an_asserted_intent_that_does_not_match_is_denied(self) -> None:
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(
            method="GET",
            path="/invoices/inv_001",
            headers=[*bearer(a_mandate()), ("x-agentiam-intent", "f" * 64)],
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.INTENT_MISMATCH

    async def test_no_asserted_intent_falls_back_to_the_token(self) -> None:
        """Documented, not accidental: binding intent per request is T-032."""
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        assert isinstance(result, Authorized)


class TestCaveats:
    async def test_a_caveat_the_pipeline_is_told_about_is_enforced(self) -> None:
        """`decide()` takes caveats as an input, since a token exposes only its grant."""
        pipeline, _, _ = await a_pipeline()
        object.__setattr__(
            pipeline,
            "_caveats_for",
            lambda _token: (ScopeSubset(scopes=frozenset({"vendor:read"})),),
        )
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.SCOPE_ATTENUATED_AWAY


class TestThroughTheGateway:
    """The pipeline behind the T-018 proxy, without a container.

    The e2e slice covers this against real Postgres; these are the same paths at unit speed,
    and they cover the gateway's enforcing branch — which would otherwise be exercised only by
    a test the default `make test` does not run.
    """

    @staticmethod
    async def _app(pipeline: Pipeline) -> httpx.AsyncClient:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse as StarletteJSON
        from starlette.routing import Route

        async def upstream(request: object) -> StarletteJSON:
            return StarletteJSON({"ok": True})

        tools = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=Starlette(routes=[Route("/{path:path}", upstream, methods=["GET", "POST"])])
            ),
            base_url="http://tools",
        )
        app = create_app(
            settings=PepSettings(upstream_base_url="http://tools"),
            upstream_client=tools,
            pipeline=pipeline,
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pep")

    async def test_an_authorized_request_reaches_the_upstream(self) -> None:
        pipeline, _, _ = await a_pipeline()
        async with await self._app(pipeline) as client:
            response = await client.get(
                "/proxy/invoices/inv_001", headers=dict(bearer(a_mandate()))
            )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    async def test_a_refusal_never_reaches_the_upstream(self) -> None:
        pipeline, _, _ = await a_pipeline()
        async with await self._app(pipeline) as client:
            response = await client.get("/proxy/invoices/inv_001")
        assert response.status_code == 401
        assert response.json()["reason_code"] == ReasonCode.MALFORMED_REQUEST.value

    async def test_readyz_reports_enforcing_when_wired(self) -> None:
        pipeline, _, _ = await a_pipeline()
        async with await self._app(pipeline) as client:
            assert (await client.get("/readyz")).json()["enforcing"] is True

    async def test_a_query_string_survives_the_hop(self) -> None:
        """`request.url.query` is a `str`; treating it as bytes broke only when enforcing."""
        pipeline, _, _ = await a_pipeline()
        async with await self._app(pipeline) as client:
            response = await client.get(
                "/proxy/invoices/inv_001?a=1", headers=dict(bearer(a_mandate()))
            )
        assert response.status_code == 200

    async def test_a_json_body_is_read_for_extraction_and_still_forwarded(self) -> None:
        pipeline, _, _ = await a_pipeline()
        async with await self._app(pipeline) as client:
            response = await client.post(
                "/proxy/payments",
                headers=dict(bearer(a_mandate())),
                json={"amount": "10.0000", "recipient": {"account_id": "acct_1"}},
            )
        assert response.status_code == 200


class TestCheckThenReserveRace:
    """Step 7 asks; the PEP then holds. A concurrent request can empty the lease between.

    `decide()` calls `budget.check()`, which reserves nothing (spec 05's shape and T-021's).
    The reservation happens after the whole pipeline allows, so two requests can both pass
    step 7 and only one get the budget. That fails closed — the loser is refused — and this
    is the only test that reaches the handler, because provoking it with real concurrency
    would be timing-dependent.
    """

    async def test_the_loser_is_refused_and_recorded(self) -> None:
        pipeline, sink, pool = await a_pipeline()
        emitter = pipeline._emitter
        await emitter.start()

        def reserve_fails(*_args: object, **_kw: object) -> None:
            raise ReservationInsufficientError("another request took the last of the lease")

        object.__setattr__(pool, "reserve", reserve_fails)

        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "10.0000", "recipient": {"account_id": "acct_1"}}',
        )
        await emitter.flush()

        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.LEASE_UNAVAILABLE
        assert result.status == 429
        assert len(sink.records) == 1, "a refusal at this point must still be audited"

    async def test_a_full_buffer_during_that_refusal_does_not_mask_it(self) -> None:
        """Both things wrong at once: the lease lost the race *and* the audit buffer is full.

        The refusal must survive. Turning it into the buffer's own denial would report a
        control-plane problem for a request that was refused on budget.
        """
        sink = CollectingSink()
        emitter = DecisionEmitter(
            sink, EmitterSettings(capacity=1, back_pressure=BackPressure.DENY)
        )
        pipeline, _, pool = await a_pipeline(emitter=emitter)

        emitter.emit(a_record_placeholder(pipeline))  # fill the single slot

        def reserve_fails(*_args: object, **_kw: object) -> None:
            raise ReservationInsufficientError("lease empty")

        object.__setattr__(pool, "reserve", reserve_fails)

        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "10.0000", "recipient": {"account_id": "acct_1"}}',
        )

        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.LEASE_UNAVAILABLE


def a_record_placeholder(pipeline: Pipeline) -> DecisionRecord:
    """One throwaway record, to occupy a single-slot buffer."""
    return DecisionRecord(
        decision_id=uuid.uuid4(),
        trace_id="t",
        timestamp=NOW,
        pep_id="pep-1",
        token_chain_ids=[],
        principal_id="kc:alice",
        task_id=uuid.uuid4(),
        agent_id="agt-1",
        depth=0,
        scope="invoice:read",
        tool_id="invoice_api",
        arg_digest="a" * 64,
        outcome=Outcome.ALLOW,
        reason_code=ReasonCode.OK,
        policy_version="bundle-1",
        budget_before=Budget(),
        budget_after=Budget(),
        latency_us=1,
    )
