"""Domain models (`agentiam_core.models`).

These are value objects: frozen, validated at construction, and free of I/O. The tests
that matter most here are the ones asserting what the models *refuse* — a float in a
money field, a caveat that cannot be evaluated, a mandate whose window is empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentiam_core.errors import (
    AgentIAMError,
    AttenuationError,
    BudgetError,
    CaveatError,
    ReasonCode,
    ScaleError,
)
from agentiam_core.models import (
    BUDGET_SCALE,
    ArgOperator,
    ArgPredicate,
    Budget,
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    CaveatKind,
    CaveatRef,
    DecisionRecord,
    DepthLimit,
    IntentBound,
    Mandate,
    Outcome,
    RequestContext,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
    caveat_kind_of,
)

T0 = datetime(2026, 8, 14, 11, 45, tzinfo=UTC)
T1 = T0 + timedelta(minutes=15)
INTENT = "0dc20e2931968ffffd73e255ac7f6984d9677d5ac1508af1a94eacd962537b70"


def a_budget(**kw: object) -> Budget:
    return Budget(**{"spend_bdt": Decimal("500000"), "tool_calls": 2000, **kw})  # type: ignore[arg-type]


def a_mandate(**kw: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": uuid4(),
        "task_id": uuid4(),
        "principal_id": "kc:9f2c1e40",
        "intent_hash": INTENT,
        "scopes": frozenset({"invoice:read", "payment:initiate"}),
        "budget": a_budget(),
        "max_depth": 8,
        "not_before": T0,
        "expires_at": T1,
    }
    return Mandate(**(base | kw))  # type: ignore[arg-type]


class TestBudgetMoney:
    def test_float_is_rejected_for_money(self) -> None:
        """Acceptance criterion for T-005. Money is Decimal, never float."""
        with pytest.raises(ValidationError, match="float"):
            Budget(spend_bdt=500000.0)  # type: ignore[arg-type]

    def test_float_rejected_even_when_integral(self) -> None:
        """0.1 + 0.2 problems do not announce themselves. Reject the type, not the value."""
        with pytest.raises(ValidationError, match="float"):
            Budget(spend_bdt=500000.0)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["500000", "0", "0.0001", "123.4567"])
    def test_decimal_and_str_and_int_are_accepted(self, value: str) -> None:
        assert Budget(spend_bdt=Decimal(value)).spend_bdt == Decimal(value)
        assert Budget(spend_bdt=value).spend_bdt == Decimal(value)  # type: ignore[arg-type]

    def test_int_is_accepted_for_money(self) -> None:
        assert Budget(spend_bdt=500).spend_bdt == Decimal(500)  # type: ignore[arg-type]

    def test_more_than_four_decimal_places_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Budget(spend_bdt=Decimal("0.00001"))

    def test_negative_money_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Budget(spend_bdt=Decimal("-1"))

    def test_negative_count_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Budget(tool_calls=-1)

    def test_defaults_are_zero(self) -> None:
        b = Budget()
        assert b.spend_bdt == Decimal(0)
        assert b.tool_calls == 0


class TestBudgetScaling:
    def test_scale_constant(self) -> None:
        assert BUDGET_SCALE == 10_000

    @pytest.mark.parametrize(
        ("dimension", "kwargs", "expected"),
        [
            (BudgetDimension.SPEND_BDT, {"spend_bdt": Decimal("500000")}, 5_000_000_000),
            (BudgetDimension.SPEND_BDT, {"spend_bdt": Decimal("0.0001")}, 1),
            (BudgetDimension.TOOL_CALLS, {"tool_calls": 2000}, 20_000_000),
            (BudgetDimension.ROWS_READ, {"rows_read": 500_000}, 5_000_000_000),
            (BudgetDimension.EXTERNAL_EMAILS, {"external_emails": 50}, 500_000),
            (BudgetDimension.WALL_CLOCK_S, {"wall_clock_s": 3600}, 36_000_000),
        ],
    )
    def test_scaled_matches_spec_01(
        self, dimension: BudgetDimension, kwargs: dict[str, object], expected: int
    ) -> None:
        assert Budget(**kwargs).scaled(dimension) == expected  # type: ignore[arg-type]

    def test_scaling_round_trips(self) -> None:
        b = a_budget(spend_bdt=Decimal("123.4567"))
        assert Budget.from_scaled({d: b.scaled(d) for d in BudgetDimension}) == b

    def test_every_dimension_is_covered(self) -> None:
        """A new dimension must not silently scale to zero."""
        b = a_budget()
        for dimension in BudgetDimension:
            assert isinstance(b.scaled(dimension), int)

    def test_from_scaled_rejects_non_integral_result(self) -> None:
        with pytest.raises(ScaleError):
            Budget.from_scaled({BudgetDimension.TOOL_CALLS: 5})  # not a whole call


class TestBudgetArithmetic:
    def test_covers_is_true_when_every_dimension_fits(self) -> None:
        assert a_budget().covers(Budget(spend_bdt=Decimal("1"), tool_calls=1))

    def test_covers_is_false_when_any_dimension_exceeds(self) -> None:
        assert not a_budget().covers(Budget(spend_bdt=Decimal("500001")))
        assert not a_budget().covers(Budget(tool_calls=2001))

    def test_covers_is_reflexive(self) -> None:
        assert a_budget().covers(a_budget())

    def test_min_takes_the_tighter_bound_per_dimension(self) -> None:
        left = Budget(spend_bdt=Decimal("100"), tool_calls=10)
        right = Budget(spend_bdt=Decimal("50"), tool_calls=99)
        assert left.min(right) == Budget(spend_bdt=Decimal("50"), tool_calls=10)


class TestFrozen:
    def test_budget_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            a_budget().spend_bdt = Decimal(1)

    def test_mandate_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            a_mandate().max_depth = 1

    def test_budget_is_hashable(self) -> None:
        assert len({a_budget(), a_budget()}) == 1


class TestMandate:
    def test_valid_mandate_constructs(self) -> None:
        assert a_mandate().max_depth == 8

    def test_expiry_before_not_before_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expires_at"):
            a_mandate(not_before=T1, expires_at=T0)

    def test_equal_window_is_rejected_as_empty(self) -> None:
        with pytest.raises(ValidationError, match="expires_at"):
            a_mandate(not_before=T0, expires_at=T0)

    def test_naive_datetimes_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            a_mandate(not_before=datetime(2026, 8, 14, 11, 45))

    @pytest.mark.parametrize("depth", [0, 9, -1])
    def test_max_depth_outside_1_to_8_is_rejected(self, depth: int) -> None:
        with pytest.raises(ValidationError):
            a_mandate(max_depth=depth)

    def test_empty_scope_set_is_allowed(self) -> None:
        """EC-T14: a token that may act on nothing is still a valid identity."""
        assert a_mandate(scopes=frozenset()).scopes == frozenset()

    @pytest.mark.parametrize("bad", ["not-hex", "abc", "g" * 64, INTENT.upper()])
    def test_intent_hash_must_be_64_lowercase_hex(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            a_mandate(intent_hash=bad)

    @pytest.mark.parametrize("scope", ["invoice:*", "INVOICE:READ", "invoice", ""])
    def test_malformed_scopes_are_rejected(self, scope: str) -> None:
        """No wildcards (EC-T15), lowercase, exactly one colon."""
        with pytest.raises(ValidationError):
            a_mandate(scopes=frozenset({scope}))


class TestCaveats:
    def test_all_nine_kinds_exist(self) -> None:
        """Nine types; eight compile to Datalog clauses (spec 02 §4)."""
        assert len(CaveatKind) == 9

    def test_each_caveat_reports_its_kind(self) -> None:
        cases: list[tuple[Caveat, CaveatKind]] = [
            (ScopeSubset(scopes=frozenset({"invoice:read"})), CaveatKind.SCOPE_SUBSET),
            (
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("50000")),
                CaveatKind.BUDGET_CEILING,
            ),
            (TimeWindow(not_after=T1), CaveatKind.TIME_WINDOW),
            (ToolAllow(tools=frozenset({"invoice_api"})), CaveatKind.TOOL_ALLOW),
            (ToolDeny(tools=frozenset({"payment_api"})), CaveatKind.TOOL_DENY),
            (
                ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("5000")),
                CaveatKind.ARG_PREDICATE,
            ),
            (DepthLimit(max_depth=3), CaveatKind.DEPTH_LIMIT),
            (IntentBound(intent_hash=INTENT), CaveatKind.INTENT_BOUND),
            (
                RequiresApproval(scopes=frozenset({"payment:initiate"})),
                CaveatKind.REQUIRES_APPROVAL,
            ),
        ]
        assert {c.kind for c, _ in cases} == set(CaveatKind)
        for caveat, kind in cases:
            assert caveat.kind is kind

    def test_only_requires_approval_is_a_fact(self) -> None:
        """ADR-008. Exactly eight types compile to a clause."""
        clause_kinds = {k for k in CaveatKind if k is not CaveatKind.REQUIRES_APPROVAL}
        assert len(clause_kinds) == 8

    def test_caveats_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            DepthLimit(max_depth=3).max_depth = 4

    def test_time_window_requires_at_least_one_bound(self) -> None:
        with pytest.raises(ValidationError):
            TimeWindow()

    def test_time_window_empty_interval_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TimeWindow(not_before=T1, not_after=T0)

    def test_budget_ceiling_rejects_float(self) -> None:
        with pytest.raises(ValidationError, match="float"):
            BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=1.5)  # type: ignore[arg-type]

    def test_budget_ceiling_zero_is_valid(self) -> None:
        """A zero ceiling means 'may not consume this at all' — legal and meaningful."""
        assert BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(0)).value == 0

    def test_arg_predicate_membership_requires_a_collection(self) -> None:
        with pytest.raises(ValidationError):
            ArgPredicate(path="email.domain", op=ArgOperator.IN, value=Decimal(1))

    def test_arg_predicate_comparison_requires_a_scalar(self) -> None:
        with pytest.raises(ValidationError):
            ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=frozenset({"a"}))

    def test_depth_limit_must_be_within_max_depth(self) -> None:
        with pytest.raises(ValidationError):
            DepthLimit(max_depth=9)


class TestRequestContext:
    def test_context_requires_every_budget_dimension(self) -> None:
        """Spec 01 §7 / ADR-007: an omitted dimension denies rather than unconstrains."""
        with pytest.raises(ValidationError, match="dimension"):
            RequestContext(
                operation="invoice:read",
                requested={BudgetDimension.SPEND_BDT: Decimal(0)},
                current_depth=1,
                request_intent=INTENT,
                now=T0,
            )

    def test_context_with_all_dimensions_constructs(self) -> None:
        ctx = a_context()
        assert ctx.requested[BudgetDimension.SPEND_BDT] == Decimal(0)

    def test_tool_and_args_are_optional(self) -> None:
        assert a_context().tool is None
        assert a_context().args == {}

    def test_negative_depth_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            a_context(current_depth=-1)

    def test_requested_rejects_float(self) -> None:
        with pytest.raises(ValidationError, match="float"):
            a_context(requested=dict.fromkeys(BudgetDimension, 1.0))


def a_context(**kw: object) -> RequestContext:
    base: dict[str, object] = {
        "operation": "invoice:read",
        "requested": dict.fromkeys(BudgetDimension, Decimal(0)),
        "current_depth": 1,
        "request_intent": INTENT,
        "now": T0,
    }
    return RequestContext(**(base | kw))  # type: ignore[arg-type]


class TestDecisionRecord:
    def test_deny_requires_a_reason_other_than_ok(self) -> None:
        with pytest.raises(ValidationError, match="reason_code"):
            a_decision(outcome=Outcome.DENY, reason_code=ReasonCode.OK)

    def test_allow_requires_reason_ok(self) -> None:
        with pytest.raises(ValidationError, match="reason_code"):
            a_decision(outcome=Outcome.ALLOW, reason_code=ReasonCode.TOKEN_EXPIRED)

    def test_allow_constructs(self) -> None:
        assert a_decision().outcome is Outcome.ALLOW

    def test_deny_constructs_with_a_failing_caveat(self) -> None:
        rec = a_decision(
            outcome=Outcome.DENY,
            reason_code=ReasonCode.BUDGET_EXHAUSTED_CAVEAT,
            failing_caveat=CaveatRef(block_index=2, kind=CaveatKind.BUDGET_CEILING),
        )
        assert rec.failing_caveat is not None
        assert rec.failing_caveat.block_index == 2

    def test_record_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            a_decision().latency_us = 1

    @pytest.mark.parametrize(
        "digest",
        [
            '{"amount": 500}',  # the arguments themselves, stringified
            "payment.amount=500",
            "abc",
            "A" * 64,  # uppercase hex is not the canonical form
        ],
    )
    def test_arg_digest_must_be_a_hash_not_raw_args(self, digest: str) -> None:
        """NFR-5 / rule 10: no PII in decision records, only a digest."""
        with pytest.raises(ValidationError, match="arg_digest"):
            a_decision(arg_digest=digest)

    def test_arg_digest_rejects_non_string(self) -> None:
        with pytest.raises(ValidationError):
            a_decision(arg_digest={"amount": 500})


def a_decision(**kw: object) -> DecisionRecord:
    base: dict[str, object] = {
        "decision_id": uuid4(),
        "trace_id": "0af7651916cd43dd8448eb211c80319c",
        "timestamp": T0,
        "pep_id": "pep-1",
        "token_chain_ids": ["a" * 128],
        "principal_id": "kc:9f2c1e40",
        "task_id": uuid4(),
        "agent_id": "agt_01",
        "depth": 1,
        "scope": "invoice:read",
        "tool_id": "invoice_api",
        "arg_digest": "b" * 64,
        "outcome": Outcome.ALLOW,
        "reason_code": ReasonCode.OK,
        "policy_version": "v14",
        "budget_before": a_budget(),
        "budget_after": a_budget(),
        "latency_us": 420,
    }
    return DecisionRecord(**(base | kw))  # type: ignore[arg-type]


class TestValidationEdges:
    """The remaining refusal paths. Each is a guard that must actually fire."""

    def test_scope_subset_rejects_malformed_scope(self) -> None:
        with pytest.raises(ValidationError, match="malformed scope"):
            ScopeSubset(scopes=frozenset({"Invoice:Read"}))

    def test_time_window_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            TimeWindow(not_after=datetime(2026, 8, 14, 12, 0))

    def test_intent_bound_rejects_bad_hash(self) -> None:
        with pytest.raises(ValidationError, match="64 lowercase hex"):
            IntentBound(intent_hash="nope")

    def test_request_context_rejects_naive_now(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            a_context(now=datetime(2026, 8, 14, 11, 45))

    def test_request_context_rejects_float_in_args(self) -> None:
        with pytest.raises(ValidationError, match="float"):
            a_context(args={"payment.amount": 1.5})

    def test_request_context_accepts_non_dict_requested_before_validation(self) -> None:
        """The float scan must not crash on a non-dict; the type error comes after."""
        with pytest.raises(ValidationError):
            a_context(requested="not-a-dict")

    def test_decision_record_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            a_decision(timestamp=datetime(2026, 8, 14, 11, 45))

    def test_decision_record_rejects_float_drift_score(self) -> None:
        with pytest.raises(ValidationError, match="float"):
            a_decision(drift_score=0.8)

    def test_decision_record_rejects_float_inside_drift_features(self) -> None:
        """Rule 4 has to reach inside the dict — a float there is still a float."""
        with pytest.raises(ValidationError, match="float"):
            a_decision(drift_features={"f5": 1.0})

    def test_decision_record_rejects_a_non_string_feature_name(self) -> None:
        # The keys are feature names that land in an audit record and, later, a research
        # dataset. A non-string key would serialize to something no reader can interpret.
        with pytest.raises(ValidationError, match="feature names"):
            a_decision(drift_features={5: Decimal("1.0")})

    def test_decision_record_accepts_decimal_drift_features(self) -> None:
        record = a_decision(drift_features={"f5": Decimal("1.0000")})
        assert record.drift_features == {"f5": Decimal("1.0000")}

    def test_drift_features_default_to_absent(self) -> None:
        # Absent means not measured; it must not be confused with an empty measurement.
        assert a_decision().drift_features is None

    def test_to_scaled_rejects_inexact_value(self) -> None:
        """Guarded upstream by decimal_places, so exercise the helper directly."""
        from agentiam_core.models import _to_scaled

        with pytest.raises(ScaleError, match="exactly at scale"):
            _to_scaled(Decimal("0.000001"))


class TestCaveatKindOf:
    def test_returns_the_kind_for_a_caveat(self) -> None:
        assert caveat_kind_of(DepthLimit(max_depth=1)) is CaveatKind.DEPTH_LIMIT

    def test_raises_for_a_non_caveat(self) -> None:
        with pytest.raises(CaveatError, match="not a caveat"):
            caveat_kind_of(object())

    def test_raises_for_something_with_a_wrong_kind_attribute(self) -> None:
        class Impostor:
            kind = "budget_ceiling"

        with pytest.raises(CaveatError, match="not a caveat"):
            caveat_kind_of(Impostor())


class TestErrors:
    def test_reason_code_can_be_attached_to_an_error(self) -> None:
        err = AgentIAMError("nope", reason_code=ReasonCode.TOKEN_EXPIRED)
        assert err.reason_code is ReasonCode.TOKEN_EXPIRED

    def test_reason_code_defaults_to_none(self) -> None:
        assert AgentIAMError("nope").reason_code is None

    def test_attenuation_error_is_an_agentiam_error(self) -> None:
        assert issubclass(AttenuationError, AgentIAMError)

    def test_scale_error_is_a_budget_error(self) -> None:
        assert issubclass(ScaleError, BudgetError)


class TestReasonCodes:
    def test_closed_enum_matches_plan_6_9(self) -> None:
        expected = {
            "OK",
            "TOKEN_INVALID_SIGNATURE",
            "TOKEN_EXPIRED",
            "TOKEN_NOT_YET_VALID",
            "TOKEN_REVOKED",
            "ANCESTOR_REVOKED",
            "SCOPE_NOT_GRANTED",
            "SCOPE_ATTENUATED_AWAY",
            "TOOL_DENIED",
            "ARG_PREDICATE_FAILED",
            "DEPTH_EXCEEDED",
            "BUDGET_EXHAUSTED_MANDATE",
            "BUDGET_EXHAUSTED_CAVEAT",
            "LEASE_UNAVAILABLE",
            "LEASE_NOT_ACTIVE",
            "RATE_LIMITED",
            "POLICY_DENIED",
            "POLICY_BUNDLE_STALE",
            "DRIFT_ESCALATION",
            "APPROVAL_REQUIRED",
            "INTENT_MISMATCH",
            "CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED",
            "MALFORMED_REQUEST",
            "TOKEN_TOO_LARGE",
            "UPSTREAM_ERROR",
            "VERIFICATION_LIMIT_EXCEEDED",  # added by T-020; PLAN §6.9 supersession note
        }
        assert {c.name for c in ReasonCode} == expected
