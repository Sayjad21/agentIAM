"""The red-team suite's Postgres/Redis-backed half — T-051, `PLAN.md` §12.

A-17, A-18 (budget) and A-29, A-30 (infrastructure) — the four named attacks in T-051's
scope (`ROADMAP.md` line 288) that need a real database or a real Redis, so they cannot
live in `tests/security/test_redteam_suite.py` (unit-level, no `tests/integration/conftest.py`
fixtures reachable from that directory). A-19..A-22 and A-31..A-33 are not in the named
subset and are not repeated here.

Three of these attacks are the exact scenario an existing test already proves — this file
adds the adversarial framing `PLAN.md` §12 asks for and cites the original rather than
duplicating its assertions wholesale; only A-18's TTL-reclaim shape (reserve, then abandon
rather than crash) is new.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy import text, update
from sqlalchemy.pool import NullPool

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.audit import append, verify_chain
from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import SKEW_ALLOWANCE, acquire, reap
from agentiam_controlplane.db.models import AuditRecordRow
from agentiam_controlplane.db.revocation_publisher import RedisRevocationPublisher
from agentiam_controlplane.errors import LeaseUnavailableError
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.errors import ReasonCode
from agentiam_core.models import Budget, DecisionRecord, Outcome
from agentiam_core.tokens import generate_keypair
from agentiam_pep.revocation import RedisRevocationSet
from tests.integration.conftest import make_budget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = [pytest.mark.integration, pytest.mark.security]

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=60)
DIMENSION = "spend_bdt"


# =============================================================================
# 12.3 Budget attacks — A-17, A-18, TM-06, TM-07
# =============================================================================


class TestA17SiblingSwarm:
    """A-17: 20 concurrent sub-agents all spending against a shared ceiling.

    TM-06, mitigated. `tests/integration/test_sibling_budgets.py::TestSharedPool` already
    proves this at the
    literal shape `PLAN.md` §9 names — 3 PEP instances, each its own engine, not merely its
    own task, because "a single shared session would serialize in the client and prove
    nothing about the database." This widens the swarm from 3 to 20 to match A-17's own
    wording, keeping the same real-Postgres discipline.
    """

    async def test_twenty_concurrent_pep_instances_cannot_exceed_the_pool(
        self, postgres_url: str, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("1000.0000"))

        async def pep(name: str) -> Decimal:
            engine = make_engine(postgres_url, poolclass=NullPool)
            try:
                async with make_session_factory(engine)() as s:
                    lease = await acquire(
                        s,
                        mandate_id=mandate_id,
                        dimension=DIMENSION,
                        requested=Decimal("100.0000"),
                        pep_id=name,
                        ttl=TTL,
                        now=NOW,
                    )
                    return lease.granted
            except LeaseUnavailableError:
                return Decimal("0.0000")
            finally:
                await engine.dispose()

        grants = await asyncio.gather(*(pep(f"pep-{i}") for i in range(20)))

        assert sum(grants) == Decimal("1000.0000"), f"grants were {grants}"
        assert all(g <= Decimal("100.0000") for g in grants)


class TestA18ReserveThenNeverCommit:
    """A-18: reserve budget and never commit, straying past a legitimate holder's use.

    TM-07, **partially mitigated**.
    `tests/integration/test_lease_pool_crash.py` already proves the harder version of this
    — a PEP process killed outright — via a real child process and `Popen.kill()`. This is
    the softer, simpler version the attack actually names: no crash at all, just a holder
    that reserves and deliberately never calls `RELEASE` or commits anything. The TTL
    reaper does not distinguish "crashed" from "abandoned on purpose," so the same bound
    applies either way — proven directly rather than inferred from the crash test.

    **Bound:** at most `max_fraction x available` per holder (25% default), reclaimed after
    `ttl + S` at the latest (`docs/threat-model.md` §5.3). **Why partial:** the attacker
    still strands real budget for that window — a legitimate co-tenant cannot spend it
    until the reaper runs, even though nothing is lost permanently.
    """

    async def test_an_abandoned_reservation_strands_budget_until_the_reaper_reclaims_it(
        self, migrated_engine: AsyncEngine
    ) -> None:
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id, total=Decimal("100.0000"))
        factory = make_session_factory(migrated_engine)

        async with factory() as s:
            lease = await acquire(
                s,
                mandate_id=mandate_id,
                dimension=DIMENSION,
                requested=Decimal("60.0000"),
                pep_id="attacker-pep",
                ttl=TTL,
                now=NOW,
            )
        # No RELEASE. No commit. The holder simply stops, on purpose.

        early = NOW + TTL + SKEW_ALLOWANCE - timedelta(seconds=1)
        async with factory() as s:
            reclaimed_early = await reap(s, now=early)
        assert lease.id not in reclaimed_early, (
            "a reap before the skew margin passes must not reclaim — reclaiming early "
            "would re-issue budget a lagging legitimate holder might still believe it has"
        )

        late = NOW + TTL + SKEW_ALLOWANCE + timedelta(seconds=1)
        async with factory() as s:
            reclaimed_late = await reap(s, now=late)
        assert lease.id in reclaimed_late, "past the margin, the abandoned reservation comes back"


# =============================================================================
# 12.5 Infrastructure — A-29, A-30, TM-08, TM-12
# =============================================================================


_KEY_PAIR = generate_keypair()
_SETTINGS = ControlPlaneSettings(
    root_private_key=_KEY_PAIR.private_key,
    approvers=frozenset({"kc:manager"}),
    session_secret_key="test-session-secret",  # noqa: S106 - throwaway test signing key
)


async def _revoke(client: httpx.AsyncClient, block_id: str) -> None:
    response = await client.post(
        "/v1/revocations",
        json={
            "block_id": block_id,
            "scope": "token",
            "reason": "red-team A-29",
            "revoked_by": "kc:manager",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201


class TestA29RevocationSuppressionByBlockingPubSub:
    """A-29: block pub/sub to suppress revocation and keep a killed token alive.

    TM-08, mitigated. `tests/integration/test_revocation_consumer.py::
    test_pull_alone_converges_when_the_push_connection_is_entirely_broken` already proves
    exactly this — spec 07 §5.2's claim that "push is an optimization; pull is the source
    of truth." Restated here as the attack: an attacker who can block the PEP's pub/sub
    connection (a firewall rule, a network partition) cannot suppress revocation, only
    delay it up to `pull_interval_s`.
    """

    async def test_blocking_the_push_channel_only_delays_revocation_not_suppresses_it(
        self, migrated_engine: AsyncEngine, redis_url: str
    ) -> None:
        factory = make_session_factory(migrated_engine)
        real_redis = redis_asyncio.Redis.from_url(redis_url)
        app = create_app(
            session_factory=factory,
            escalation_settings=_SETTINGS,
            revocation_publisher=RedisRevocationPublisher(real_redis),
            now=lambda: NOW,
        )
        control_plane_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cp"
        )

        # Simulates the attacker's firewall rule: every subscribe attempt to this port
        # fails, forever. The push path is not merely slow — it never works at all.
        blocked_redis = redis_asyncio.Redis(
            host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2
        )
        oracle = RedisRevocationSet(
            redis_client=blocked_redis,
            control_plane_client=control_plane_client,
            pull_interval_s=0.2,
            staleness_limit_s=30.0,
            resubscribe_delay_s=0.1,
            now=lambda: datetime.now(UTC),
        )

        try:
            await oracle.start()
            block_id = "z" * 128
            assert oracle.is_revoked(block_id) is False

            await _revoke(control_plane_client, block_id)

            converged = False
            for _ in range(20):
                await asyncio.sleep(0.2)
                if oracle.is_revoked(block_id):
                    converged = True
                    break

            assert converged, (
                "blocking the push channel must not suppress revocation — the pull "
                "backstop has to converge on its own, within a bounded number of "
                "pull_interval_s ticks"
            )
        finally:
            await oracle.aclose()
            await control_plane_client.aclose()
            await real_redis.aclose()
            await blocked_redis.aclose()


def _a_record(
    *, outcome: Outcome, reason_code: ReasonCode, scope: str = "payment:initiate"
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=uuid.uuid4(),
        trace_id="trace-redteam",
        timestamp=NOW,
        pep_id="pep-1",
        token_chain_ids=["blk_root"],
        principal_id="kc:alice",
        task_id=uuid.uuid4(),
        agent_id="agt-1",
        depth=1,
        scope=scope,
        tool_id="payment_api",
        arg_digest="a" * 64,
        outcome=outcome,
        reason_code=reason_code,
        policy_version="bundle-1",
        budget_before=Budget(spend_bdt=Decimal(100000)),
        budget_after=Budget(spend_bdt=Decimal(100000)),
        latency_us=12,
    )


class TestA30AuditTampering:
    """A-30: alter an audit record to hide what actually happened. TM-12, mitigated.

    `tests/integration/test_audit_chain.py::test_tampering_with_a_record_is_detected`
    already proves this generically (NFR-6). The attack this restates is the concrete one
    that matters for this system's threat model: covering up a *denied* payment by
    rewriting the record to say it was allowed — not an abstract field edit, the specific
    thing an attacker with transient database access would actually want.
    """

    async def test_rewriting_a_denied_payment_into_an_allowed_one_is_detected(
        self, session: AsyncSession
    ) -> None:
        allowed = _a_record(outcome=Outcome.ALLOW, reason_code=ReasonCode.OK)
        denied = _a_record(outcome=Outcome.DENY, reason_code=ReasonCode.BUDGET_EXHAUSTED_MANDATE)
        await append(session, [allowed, denied], now=NOW)

        async with session.begin():
            # Same technique as `test_audit_chain.py::test_tampering_with_a_record_is_detected`
            # (`jsonb_set`, in place, on the already-hashed record) — here specifically
            # flipping the field that matters most: whether the payment happened.
            await session.execute(
                update(AuditRecordRow)
                .where(AuditRecordRow.seq == 2)
                .values(record=text("jsonb_set(record, '{outcome}', '\"allow\"')"))
            )

        result = await verify_chain(session)
        assert not result.ok, "a DENY rewritten to an ALLOW must not verify as intact"
        assert result.first_bad_seq == 2
