"""The decision pipeline (`agentiam_core.decision`) — T-019, spec 09.

`PLAN.md` §3.2 principle 4: *every deny is explainable — a decision record names the exact
caveat, policy statement, or budget that caused it. "Denied" without a reason is a bug.*

Naming a cause is only meaningful if the pipeline agrees in advance **which** cause to name
when several are true at once, so `TestPrecedence` is the centre of this file rather than
an afterthought. Spec 09 §3 states the contract; these tests hold it.

The oracles are all local and synchronous by design (spec 09 §1): `PLAN.md` §3.1 marks
steps 2-7 as microseconds and §3.2 forbids blocking on the network to reach a verdict, so
the fakes here are not a simplification of the real thing — they are the same shape as the
in-memory caches the PEP will hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentiam_core.attenuation import attenuate
from agentiam_core.decision import (
    DRIFT_ESCALATION_THRESHOLD,
    BudgetVerdict,
    Decision,
    OracleUnavailable,
    PolicyVerdict,
    decide,
    decision_for_token_error,
)
from agentiam_core.errors import (
    DepthExceededError,
    InvalidSignatureError,
    MalformedTokenError,
    ReasonCode,
    TokenExpiredError,
    TokenNotYetValidError,
    TokenTooLargeError,
)
from agentiam_core.models import (
    ArgOperator,
    ArgPredicate,
    Budget,
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    CaveatKind,
    DepthLimit,
    IntentBound,
    Outcome,
    RequestContext,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)
from agentiam_core.tokens import (
    RootKeySet,
    VerifiedToken,
    generate_keypair,
    mint_root,
    verify,
)
from tests.fixtures.tokens import INTENT, NOW, ROOT_SCOPES, a_mandate

# --------------------------------------------------------------------------- fakes


class FakeRevocation:
    """The PEP's in-memory bloom filter plus exact set, without the bloom filter."""

    def __init__(self, revoked: set[str] | None = None, *, available: bool = True) -> None:
        """Build a revocation oracle over an explicit set."""
        self._revoked = revoked or set()
        self._available = available

    def is_revoked(self, revocation_id: str) -> bool:
        if not self._available:
            raise OracleUnavailable("revocation set could not be consulted")
        return revocation_id in self._revoked


class FakePolicy:
    def __init__(
        self,
        *,
        allowed: bool = True,
        statement: str | None = None,
        version: str = "bundle-1",
        stale: bool = False,
        available: bool = True,
    ) -> None:
        """Build a policy engine returning one fixed verdict."""
        """Build a revocation oracle over an explicit set."""
        self._verdict = PolicyVerdict(
            allowed=allowed, statement=statement, version=version, stale=stale
        )
        self._available = available

    def evaluate(self, context: RequestContext) -> PolicyVerdict:
        if not self._available:
            raise OracleUnavailable("policy bundle could not be consulted")
        return self._verdict


class FakeBudget:
    def __init__(
        self,
        *,
        ok: bool = True,
        exhausted: BudgetDimension | None = None,
        mandate_exhausted: bool = False,
        available: bool = True,
    ) -> None:
        """Build a lease-pool oracle returning one fixed verdict."""
        self._verdict = BudgetVerdict(
            ok=ok, exhausted_dimension=exhausted, mandate_exhausted=mandate_exhausted
        )
        self._available = available

    def check(self, requested: Mapping[BudgetDimension, Decimal]) -> BudgetVerdict:
        if not self._available:
            raise OracleUnavailable("lease pool could not be consulted")
        return self._verdict


class FakeDrift:
    def __init__(self, score: Decimal | None = None, *, available: bool = True) -> None:
        """Build a drift oracle returning one fixed score."""
        self._score = score
        self._available = available

    def score_for(self, context: RequestContext) -> Decimal | None:
        if not self._available:
            raise OracleUnavailable("drift service could not be consulted")
        return self._score


# --------------------------------------------------------------------------- helpers

_KEY = generate_keypair()
_KEY_SET = RootKeySet([_KEY.public_key])


def a_token(**mandate_kw: object) -> VerifiedToken:
    return verify(mint_root(a_mandate(**mandate_kw), _KEY.private_key), _KEY_SET, now=NOW)


def a_child_token() -> VerifiedToken:
    """A depth-1 chain, so there is an ancestor to revoke."""
    root = a_token()
    child = attenuate(
        root,
        [ScopeSubset(scopes=ROOT_SCOPES)],
        agent_id="agent:child",
        role="reader",
    )
    return verify(child, _KEY_SET, now=NOW)


def ctx(
    *,
    operation: str = "invoice:read",
    tool: str | None = "erp.invoice.get",
    requested: dict[BudgetDimension, Decimal] | None = None,
    depth: int = 1,
    intent: str = INTENT,
    now: datetime = NOW,
    args: dict[str, Decimal | int | str] | None = None,
) -> RequestContext:
    return RequestContext(
        operation=operation,
        tool=tool,
        requested=requested or dict.fromkeys(BudgetDimension, Decimal(0)),
        current_depth=depth,
        request_intent=intent,
        now=now,
        args=args or {},
    )


def run(
    *,
    caveats: tuple[Caveat, ...] = (),
    context: RequestContext | None = None,
    revocation: FakeRevocation | None = None,
    policy: FakePolicy | None = None,
    budget: FakeBudget | None = None,
    drift: FakeDrift | None = None,
    token_kw: dict[str, object] | None = None,
) -> Decision:
    return decide(
        a_token(**(token_kw or {})),
        context or ctx(),
        caveats=caveats,
        revocation=revocation or FakeRevocation(),
        policy=policy or FakePolicy(),
        budget=budget or FakeBudget(),
        drift=drift,
    )


# --------------------------------------------------------------------------- tests


class TestAllow:
    def test_a_clean_request_is_allowed(self) -> None:
        decision = run()
        assert decision.outcome is Outcome.ALLOW
        assert decision.reason_code is ReasonCode.OK
        assert decision.failing_caveat is None

    def test_ok_appears_only_on_an_allow(self) -> None:
        """Spec 09 §1, and the `DecisionRecord` model refuses the other combination."""
        denied = run(policy=FakePolicy(allowed=False, statement="forbid payments"))
        assert denied.reason_code is not ReasonCode.OK

    def test_satisfied_caveats_do_not_deny(self) -> None:
        decision = run(
            caveats=(
                ScopeSubset(scopes=frozenset({"invoice:read"})),
                ToolAllow(tools=frozenset({"erp.invoice.get"})),
                DepthLimit(max_depth=4),
            )
        )
        assert decision.outcome is Outcome.ALLOW


class TestStep2TokenErrors:
    """Steps 1-2 happen before there is a `VerifiedToken` to reason about."""

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (MalformedTokenError("bad"), ReasonCode.MALFORMED_REQUEST),
            (InvalidSignatureError("forged"), ReasonCode.TOKEN_INVALID_SIGNATURE),
            (TokenExpiredError("stale"), ReasonCode.TOKEN_EXPIRED),
            (TokenNotYetValidError("early"), ReasonCode.TOKEN_NOT_YET_VALID),
            (TokenTooLargeError("huge"), ReasonCode.TOKEN_TOO_LARGE),
            (DepthExceededError("deep"), ReasonCode.DEPTH_EXCEEDED),
        ],
    )
    def test_each_token_error_maps_to_its_own_code(
        self, error: Exception, code: ReasonCode
    ) -> None:
        """Build a drift oracle returning one fixed score."""
        decision = decision_for_token_error(error)  # type: ignore[arg-type]
        assert decision.outcome is Outcome.DENY
        assert decision.reason_code is code

    def test_the_detail_survives(self) -> None:
        """The operator needs the message, not just the category."""
        assert "forged" in decision_for_token_error(InvalidSignatureError("forged")).reason_detail


class TestStep3Revocation:
    def test_a_revoked_token_is_denied(self) -> None:
        token = a_token()
        revoked = FakeRevocation({token.revocation_ids[-1]})
        decision = decide(
            token,
            ctx(),
            caveats=(),
            revocation=revoked,
            policy=FakePolicy(),
            budget=FakeBudget(),
        )
        assert decision.outcome is Outcome.DENY
        assert decision.reason_code is ReasonCode.TOKEN_REVOKED

    def test_a_revoked_ancestor_kills_the_chain(self) -> None:
        """INV-10: no resurrection. Revoking a parent needs no child enumerated.

        Needs a real attenuated chain — a root token has exactly one revocation id, so
        this was briefly a skipped test, which is not a test. `ANCESTOR_REVOKED` is
        claimed reachable in spec 09 §7 and this is what makes that claim true.
        """
        child = a_child_token()
        assert len(child.revocation_ids) >= 2, "the chain must have an ancestor to revoke"

        decision = decide(
            child,
            ctx(),
            caveats=(),
            revocation=FakeRevocation({child.revocation_ids[0]}),
            policy=FakePolicy(),
            budget=FakeBudget(),
        )
        assert decision.outcome is Outcome.DENY
        assert decision.reason_code is ReasonCode.ANCESTOR_REVOKED

    def test_the_chains_own_id_is_reported_as_itself_not_an_ancestor(self) -> None:
        """The last id is this token's own; anything earlier is an ancestor."""
        child = a_child_token()
        decision = decide(
            child,
            ctx(),
            caveats=(),
            revocation=FakeRevocation({child.revocation_ids[-1]}),
            policy=FakePolicy(),
            budget=FakeBudget(),
        )
        assert decision.reason_code is ReasonCode.TOKEN_REVOKED

    def test_an_unrevoked_token_passes(self) -> None:
        assert run(revocation=FakeRevocation({"some-other-id"})).outcome is Outcome.ALLOW


class TestStep4Caveats:
    def test_a_scope_outside_the_grant_is_not_granted(self) -> None:
        """Never in the mandate — distinct from given away by a delegation step."""
        decision = run(context=ctx(operation="admin:write"))
        assert decision.reason_code is ReasonCode.SCOPE_NOT_GRANTED

    def test_a_scope_narrowed_away_is_attenuated_away(self) -> None:
        """In the grant, removed by a caveat. The operator's fix is a different one."""
        decision = run(
            caveats=(ScopeSubset(scopes=frozenset({"invoice:read"})),),
            context=ctx(operation="payment:initiate"),
        )
        assert decision.reason_code is ReasonCode.SCOPE_ATTENUATED_AWAY
        assert decision.failing_caveat is not None
        assert decision.failing_caveat.kind is CaveatKind.SCOPE_SUBSET

    def test_a_denied_tool(self) -> None:
        decision = run(caveats=(ToolDeny(tools=frozenset({"erp.invoice.get"})),))
        assert decision.reason_code is ReasonCode.TOOL_DENIED
        assert decision.failing_caveat is not None

    def test_a_tool_outside_an_allow_list(self) -> None:
        decision = run(caveats=(ToolAllow(tools=frozenset({"erp.other"})),))
        assert decision.reason_code is ReasonCode.TOOL_DENIED

    def test_a_failing_argument_predicate(self) -> None:
        decision = run(
            caveats=(
                ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("1000")),
            ),
            context=ctx(args={"payment.amount": Decimal("5000")}),
        )
        assert decision.reason_code is ReasonCode.ARG_PREDICATE_FAILED

    def test_a_mismatched_intent(self) -> None:
        other = "f" * 64
        decision = run(caveats=(IntentBound(intent_hash=INTENT),), context=ctx(intent=other))
        assert decision.reason_code is ReasonCode.INTENT_MISMATCH

    def test_a_budget_ceiling_caveat(self) -> None:
        decision = run(
            caveats=(BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("100")),),
            context=ctx(
                requested={
                    **dict.fromkeys(BudgetDimension, Decimal(0)),
                    BudgetDimension.SPEND_BDT: Decimal("500"),
                }
            ),
        )
        assert decision.reason_code is ReasonCode.BUDGET_EXHAUSTED_CAVEAT

    def test_a_depth_limit_caveat(self) -> None:
        decision = run(caveats=(DepthLimit(max_depth=1),), context=ctx(depth=5))
        assert decision.reason_code is ReasonCode.DEPTH_EXCEEDED

    def test_an_expired_time_window_caveat(self) -> None:
        decision = run(
            caveats=(TimeWindow(not_after=NOW - timedelta(minutes=1)),),
            context=ctx(now=NOW),
        )
        assert decision.reason_code is ReasonCode.TOKEN_EXPIRED

    def test_requires_approval_escalates_rather_than_denies(self) -> None:
        """Escalation is a first-class outcome (`PLAN.md` §6.9), not a flavour of deny."""
        decision = run(caveats=(RequiresApproval(scopes=frozenset({"invoice:read"})),))
        assert decision.outcome is Outcome.ESCALATE
        assert decision.reason_code is ReasonCode.APPROVAL_REQUIRED

    def test_the_failing_caveat_is_named(self) -> None:
        decision = run(caveats=(ToolDeny(tools=frozenset({"erp.invoice.get"})),))
        assert decision.failing_caveat is not None
        assert decision.failing_caveat.kind is CaveatKind.TOOL_DENY
        assert decision.failing_caveat.detail

    def test_the_reason_detail_never_quotes_an_argument_value(self) -> None:
        """Spec 09 §8. Copying the payload into the reason is a PII leak by another route."""
        decision = run(
            caveats=(ArgPredicate(path="payment.account", op=ArgOperator.EQ, value="ACC-PUBLIC"),),
            context=ctx(args={"payment.account": "ACC-SECRET-4451"}),
        )
        assert decision.reason_code is ReasonCode.ARG_PREDICATE_FAILED
        assert "ACC-SECRET-4451" not in decision.reason_detail


class TestStep5Policy:
    def test_a_policy_denial_names_the_statement(self) -> None:
        decision = run(policy=FakePolicy(allowed=False, statement="forbid_weekend_payments"))
        assert decision.outcome is Outcome.DENY
        assert decision.reason_code is ReasonCode.POLICY_DENIED
        assert "forbid_weekend_payments" in decision.reason_detail

    def test_a_stale_bundle_has_its_own_code(self) -> None:
        """Distinct from POLICY_DENIED: the operator's fix is to refresh, not to edit."""
        decision = run(policy=FakePolicy(stale=True))
        assert decision.outcome is Outcome.DENY
        assert decision.reason_code is ReasonCode.POLICY_BUNDLE_STALE

    def test_a_stale_bundle_denies_even_when_it_would_allow(self) -> None:
        decision = run(policy=FakePolicy(allowed=True, stale=True))
        assert decision.outcome is Outcome.DENY


class TestStep6Drift:
    def test_a_high_score_escalates(self) -> None:
        decision = run(drift=FakeDrift(DRIFT_ESCALATION_THRESHOLD + Decimal("0.1")))
        assert decision.outcome is Outcome.ESCALATE
        assert decision.reason_code is ReasonCode.DRIFT_ESCALATION

    def test_a_low_score_allows_and_is_recorded(self) -> None:
        decision = run(drift=FakeDrift(Decimal("0.2")))
        assert decision.outcome is Outcome.ALLOW
        assert decision.drift_score == Decimal("0.2")

    def test_drift_never_denies(self) -> None:
        """Spec 09 §3.3. A heuristic over natural language must not stop a payment."""
        decision = run(drift=FakeDrift(Decimal("1.0")))
        assert decision.outcome is not Outcome.DENY

    def test_an_absent_drift_oracle_is_not_a_failure(self) -> None:
        assert run(drift=None).outcome is Outcome.ALLOW

    def test_an_unavailable_drift_oracle_does_not_fail_closed(self) -> None:
        """Spec 09 §5's exception, and it is deliberate.

        Failing closed here would let an outage of an advisory heuristic stop every
        payment in the system.
        """
        decision = run(drift=FakeDrift(available=False))
        assert decision.outcome is Outcome.ALLOW
        assert decision.drift_score is None


class TestStep7Budget:
    def test_an_exhausted_mandate(self) -> None:
        decision = run(budget=FakeBudget(ok=False, mandate_exhausted=True))
        assert decision.reason_code is ReasonCode.BUDGET_EXHAUSTED_MANDATE

    def test_an_unavailable_lease(self) -> None:
        decision = run(budget=FakeBudget(ok=False, exhausted=BudgetDimension.SPEND_BDT))
        assert decision.reason_code is ReasonCode.LEASE_UNAVAILABLE

    def test_the_dimension_is_named(self) -> None:
        decision = run(budget=FakeBudget(ok=False, exhausted=BudgetDimension.TOOL_CALLS))
        assert "tool_calls" in decision.reason_detail


class TestPrecedence:
    """Spec 09 §3. The first failing step wins, in order.

    Reporting the "most severe" failure instead means a revoked token can be reported as
    `SCOPE_NOT_GRANTED`, and the operator spends an afternoon adjusting scopes on a
    credential that was killed hours ago.
    """

    def test_revocation_beats_a_caveat_failure(self) -> None:
        token = a_token()
        decision = decide(
            token,
            ctx(operation="admin:write"),
            caveats=(ToolDeny(tools=frozenset({"erp.invoice.get"})),),
            revocation=FakeRevocation({token.revocation_ids[-1]}),
            policy=FakePolicy(allowed=False, statement="s"),
            budget=FakeBudget(ok=False, mandate_exhausted=True),
        )
        assert decision.reason_code is ReasonCode.TOKEN_REVOKED

    def test_a_caveat_failure_beats_policy(self) -> None:
        decision = run(
            caveats=(ToolDeny(tools=frozenset({"erp.invoice.get"})),),
            policy=FakePolicy(allowed=False, statement="s"),
        )
        assert decision.reason_code is ReasonCode.TOOL_DENIED

    def test_policy_beats_drift(self) -> None:
        decision = run(
            policy=FakePolicy(allowed=False, statement="s"),
            drift=FakeDrift(Decimal("0.99")),
        )
        assert decision.reason_code is ReasonCode.POLICY_DENIED

    def test_policy_beats_budget(self) -> None:
        decision = run(
            policy=FakePolicy(allowed=False, statement="s"),
            budget=FakeBudget(ok=False, mandate_exhausted=True),
        )
        assert decision.reason_code is ReasonCode.POLICY_DENIED

    def test_drift_escalation_beats_budget(self) -> None:
        decision = run(
            drift=FakeDrift(Decimal("0.99")),
            budget=FakeBudget(ok=False, mandate_exhausted=True),
        )
        assert decision.reason_code is ReasonCode.DRIFT_ESCALATION

    def test_deny_beats_escalate_within_step_four(self) -> None:
        """Spec 09 §3.1, INV-8.

        Approval cannot grant authority the token does not have, so escalating asks a
        human a question with only one correct answer.
        """
        decision = run(
            caveats=(
                RequiresApproval(scopes=frozenset({"invoice:read"})),
                ToolDeny(tools=frozenset({"erp.invoice.get"})),
            )
        )
        assert decision.outcome is Outcome.DENY
        assert decision.reason_code is ReasonCode.TOOL_DENIED

    def test_deny_beats_escalate_regardless_of_order(self) -> None:
        decision = run(
            caveats=(
                ToolDeny(tools=frozenset({"erp.invoice.get"})),
                RequiresApproval(scopes=frozenset({"invoice:read"})),
            )
        )
        assert decision.outcome is Outcome.DENY

    def test_the_caveat_nearer_the_root_is_reported(self) -> None:
        """Spec 09 §3.2: the broader restriction, and the larger decision to remove."""
        decision = run(
            caveats=(
                ScopeSubset(scopes=frozenset({"vendor:read"})),
                ToolDeny(tools=frozenset({"erp.invoice.get"})),
            ),
            context=ctx(operation="invoice:read"),
        )
        assert decision.reason_code is ReasonCode.SCOPE_ATTENUATED_AWAY


class TestFailClosed:
    """Spec 09 §5. Unavailable is not the same as 'says no', and both must refuse."""

    def test_an_unavailable_revocation_set_denies(self) -> None:
        decision = run(revocation=FakeRevocation(available=False))
        assert decision.outcome is Outcome.DENY
        assert decision.reason_code is ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED

    def test_an_unavailable_policy_engine_denies(self) -> None:
        decision = run(policy=FakePolicy(available=False))
        assert decision.reason_code is ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED

    def test_an_unavailable_lease_pool_denies(self) -> None:
        decision = run(budget=FakeBudget(available=False))
        assert decision.reason_code is ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED

    def test_the_detail_says_which_dependency(self) -> None:
        """Failing closed without naming the dependency is an unactionable page."""
        assert "policy" in run(policy=FakePolicy(available=False)).reason_detail.lower()


class TestReasonCodeCoverage:
    def test_every_code_is_reachable_or_declared_unreachable(self) -> None:
        """`PLAN.md` §6.9: every code is reachable, and spec 09 §7 is the map.

        `RATE_LIMITED` is the one exception — the `RateLimit` caveat was dropped
        (`ROADMAP.md` Part 1) — and it stays in the enum so the console's filter list is
        stable across the change.
        """
        produced_here = {
            ReasonCode.OK,
            ReasonCode.MALFORMED_REQUEST,
            ReasonCode.TOKEN_INVALID_SIGNATURE,
            ReasonCode.TOKEN_EXPIRED,
            ReasonCode.TOKEN_NOT_YET_VALID,
            ReasonCode.TOKEN_TOO_LARGE,
            ReasonCode.DEPTH_EXCEEDED,
            ReasonCode.TOKEN_REVOKED,
            ReasonCode.ANCESTOR_REVOKED,
            ReasonCode.SCOPE_NOT_GRANTED,
            ReasonCode.SCOPE_ATTENUATED_AWAY,
            ReasonCode.TOOL_DENIED,
            ReasonCode.ARG_PREDICATE_FAILED,
            ReasonCode.INTENT_MISMATCH,
            ReasonCode.BUDGET_EXHAUSTED_CAVEAT,
            ReasonCode.APPROVAL_REQUIRED,
            ReasonCode.POLICY_DENIED,
            ReasonCode.POLICY_BUNDLE_STALE,
            ReasonCode.DRIFT_ESCALATION,
            ReasonCode.BUDGET_EXHAUSTED_MANDATE,
            ReasonCode.LEASE_UNAVAILABLE,
            ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED,
        }
        elsewhere = {
            ReasonCode.UPSTREAM_ERROR,  # T-018, post-decision (spec 09 §6)
            ReasonCode.LEASE_NOT_ACTIVE,  # T-014, the ledger (ADR-009)
            # T-020, step 2: the Datalog engine ran out of budget reading the token.
            # `tokens.verify` raises it; TM-25's residual, closed there rather than here.
            ReasonCode.VERIFICATION_LIMIT_EXCEEDED,
        }
        unreachable = {ReasonCode.RATE_LIMITED}

        assert produced_here | elsewhere | unreachable == set(ReasonCode)


class TestDecisionShape:
    def test_a_decision_is_frozen(self) -> None:
        """It becomes an audit record; something that can be edited afterwards is not one."""
        decision = run()
        with pytest.raises(AttributeError):
            decision.outcome = Outcome.DENY  # type: ignore[misc]

    def test_every_deny_carries_a_detail(self) -> None:
        """P-18. A code alone tells the operator the category, not the instance."""
        for decision in (
            run(context=ctx(operation="admin:write")),
            run(policy=FakePolicy(allowed=False, statement="s")),
            run(budget=FakeBudget(ok=False, mandate_exhausted=True)),
            run(revocation=FakeRevocation(available=False)),
        ):
            assert decision.outcome is not Outcome.ALLOW
            assert decision.reason_detail, decision.reason_code

    def test_it_builds_a_valid_decision_record(self) -> None:
        """The end of the pipeline: `DecisionRecord`'s validators must accept the output.

        The model cross-checks outcome against reason code, so a pipeline that produced an
        inconsistent pair would fail here rather than at T-022.
        """
        from agentiam_core.models import DecisionRecord

        decision = run(caveats=(ToolDeny(tools=frozenset({"erp.invoice.get"})),))
        token = a_token()
        record = DecisionRecord(
            decision_id=uuid.uuid4(),
            trace_id="trace-1",
            timestamp=datetime.now(UTC),
            pep_id="pep-1",
            token_chain_ids=list(token.revocation_ids),
            principal_id=token.principal_id,
            task_id=token.task_id,
            agent_id="agent:1",
            depth=1,
            scope="invoice:read",
            tool_id="erp.invoice.get",
            arg_digest="0" * 64,
            outcome=decision.outcome,
            reason_code=decision.reason_code,
            reason_detail=decision.reason_detail,
            failing_caveat=decision.failing_caveat,
            policy_version="bundle-1",
            budget_before=Budget(),
            budget_after=Budget(),
            latency_us=42,
        )
        assert record.reason_code is ReasonCode.TOOL_DENIED


@pytest.mark.perf
class TestNfr1:
    def test_the_pure_decision_is_under_a_millisecond(self, benchmark: object) -> None:
        """NFR-1, and `PLAN.md` §17 R-2: p99 over 2 ms by M8 triggers a port to Rust.

        Benchmarked with the oracles warm, which is the real shape — they are in-memory
        caches, not services (spec 09 §1).
        """
        token = a_token()
        context = ctx()
        caveats = (
            ScopeSubset(scopes=ROOT_SCOPES),
            ToolAllow(tools=frozenset({"erp.invoice.get"})),
            DepthLimit(max_depth=8),
            BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("1000")),
        )
        revocation, policy, budget = FakeRevocation(), FakePolicy(), FakeBudget()

        def one_decision() -> Decision:
            return decide(
                token,
                context,
                caveats=caveats,
                revocation=revocation,
                policy=policy,
                budget=budget,
            )

        result = benchmark(one_decision)  # type: ignore[operator]
        assert result.outcome is Outcome.ALLOW

        # NFR-1 is a p99 claim, so assert on p99 rather than on the mean a reader would
        # otherwise have to take on trust. `benchmark` prints the table; this makes the
        # number a gate.
        timings = sorted(benchmark.stats.stats.data)  # type: ignore[attr-defined]
        p99 = timings[int(len(timings) * 0.99)]
        assert p99 < 0.001, f"p99 was {p99 * 1e6:.1f} us, over the 1 ms NFR-1 budget"


class TestAllowedProperty:
    """`Decision.allowed` is the one-line answer the PEP branches on."""

    def test_allow_is_allowed(self) -> None:
        assert run().allowed

    def test_a_deny_is_not_allowed(self) -> None:
        assert not run(policy=FakePolicy(allowed=False)).allowed

    def test_an_escalation_is_not_allowed(self) -> None:
        """INV-8: escalation is not permission — it is a question waiting for an answer."""
        decision = run(caveats=(RequiresApproval(scopes=frozenset({"invoice:read"})),))
        assert decision.outcome is Outcome.ESCALATE
        assert not decision.allowed
