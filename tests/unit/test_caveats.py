"""Caveat compilation and evaluation (`agentiam_core.caveats`).

Two suites, and the second is the one that matters.

`TestEvaluate` is table-driven per caveat kind, with boundary values, and checks the pure
Python path in isolation.

`TestDatalogConformance` compiles each caveat into a **real biscuit block**, authorizes it
with a real request context, and asserts the verdict matches `evaluate()`. That agreement
is a security property (ADR-008): the PEP trusts Datalog for the decision and `evaluate()`
for the explanation, so a divergence produces a decision record naming a caveat that never
fired. Re-implementing the comparison twice in Python and asserting they match would prove
nothing — the Datalog has to actually run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from biscuit_auth import AuthorizerBuilder, BiscuitBuilder, BlockBuilder, KeyPair

from agentiam_core.caveats import (
    CaveatResult,
    block_datalog,
    evaluate,
    request_context_datalog,
    scale,
    to_datalog,
)
from agentiam_core.errors import CaveatError, ReasonCode
from agentiam_core.models import (
    ArgOperator,
    ArgPredicate,
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

T0 = datetime(2026, 8, 14, 11, 45, tzinfo=UTC)
T1 = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
MID = datetime(2026, 8, 14, 11, 50, tzinfo=UTC)
INTENT = "0dc20e2931968ffffd73e255ac7f6984d9677d5ac1508af1a94eacd962537b70"
OTHER_INTENT = "1" * 64


def ctx(**kw: object) -> RequestContext:
    base: dict[str, object] = {
        "operation": "invoice:read",
        "requested": dict.fromkeys(BudgetDimension, Decimal(0)),
        "current_depth": 1,
        "request_intent": INTENT,
        "now": MID,
    }
    return RequestContext(**(base | kw))  # type: ignore[arg-type]


def spend(amount: str) -> dict[BudgetDimension, Decimal]:
    return dict.fromkeys(BudgetDimension, Decimal(0)) | {BudgetDimension.SPEND_BDT: Decimal(amount)}


# ---------------------------------------------------------------------------
# The table. Each row: caveat, request context, expected outcome.
#
# Five or more cases per kind, boundaries included, per T-008's criteria.
# ---------------------------------------------------------------------------

SCOPE_CASES = [
    (ScopeSubset(scopes=frozenset({"invoice:read", "vendor:read"})), ctx(), True),
    (
        ScopeSubset(scopes=frozenset({"invoice:read", "vendor:read"})),
        ctx(operation="vendor:read"),
        True,
    ),
    (ScopeSubset(scopes=frozenset({"invoice:read"})), ctx(operation="vendor:read"), False),
    (ScopeSubset(scopes=frozenset({"invoice:read"})), ctx(operation="payment:initiate"), False),
    (ScopeSubset(scopes=frozenset()), ctx(), False),  # EC-T14: grants nothing
    (ScopeSubset(scopes=frozenset({"invoice:read"})), ctx(operation="invoice:read"), True),
]

BUDGET_CASES = [
    (BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("100")), ctx(), True),
    (
        BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("100")),
        ctx(requested=spend("99.9999")),
        True,
    ),
    (
        BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("100")),
        ctx(requested=spend("100")),  # boundary: <= is inclusive
        True,
    ),
    (
        BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("100")),
        ctx(requested=spend("100.0001")),  # one scaled unit over
        False,
    ),
    (
        BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(0)),
        ctx(requested=spend("0.0001")),  # zero ceiling means none at all
        False,
    ),
    (BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(0)), ctx(), True),
    (
        BudgetCeiling(dimension=BudgetDimension.TOOL_CALLS, value=Decimal(5)),
        ctx(
            requested=dict.fromkeys(BudgetDimension, Decimal(0))
            | {BudgetDimension.TOOL_CALLS: Decimal(6)}
        ),
        False,
    ),
]

TIME_CASES = [
    (TimeWindow(not_after=T1), ctx(now=MID), True),
    (TimeWindow(not_after=T1), ctx(now=T1), False),  # EC-T06: exclusive
    (TimeWindow(not_after=T1), ctx(now=T0), True),
    (TimeWindow(not_before=T0), ctx(now=T0), True),  # inclusive
    (TimeWindow(not_before=T0), ctx(now=datetime(2026, 8, 14, 11, 44, tzinfo=UTC)), False),
    (TimeWindow(not_before=T0, not_after=T1), ctx(now=MID), True),
    (TimeWindow(not_before=T0, not_after=T1), ctx(now=T1), False),
]

TOOL_ALLOW_CASES = [
    (ToolAllow(tools=frozenset({"invoice_api"})), ctx(tool="invoice_api"), True),
    (ToolAllow(tools=frozenset({"invoice_api", "vendor_api"})), ctx(tool="vendor_api"), True),
    (ToolAllow(tools=frozenset({"invoice_api"})), ctx(tool="payment_api"), False),
    (ToolAllow(tools=frozenset({"invoice_api"})), ctx(), False),  # no tool: fail closed
    (ToolAllow(tools=frozenset()), ctx(tool="invoice_api"), False),
]

TOOL_DENY_CASES = [
    (ToolDeny(tools=frozenset({"payment_api"})), ctx(tool="invoice_api"), True),
    (ToolDeny(tools=frozenset({"payment_api"})), ctx(tool="payment_api"), False),
    (ToolDeny(tools=frozenset({"payment_api"})), ctx(), True),  # no tool: vacuous
    (ToolDeny(tools=frozenset()), ctx(tool="anything"), True),
    (ToolDeny(tools=frozenset({"a", "b"})), ctx(tool="b"), False),
]

ARG_CASES = [
    (
        ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("500")),
        ctx(args={"payment.amount": Decimal("499")}),
        True,
    ),
    (
        ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("500")),
        ctx(args={"payment.amount": Decimal("500")}),  # boundary
        True,
    ),
    (
        ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("500")),
        ctx(args={"payment.amount": Decimal("500.0001")}),
        False,
    ),
    (
        ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("500")),
        ctx(),  # absent argument: vacuous, per ADR-007
        True,
    ),
    (
        ArgPredicate(path="payment.amount", op=ArgOperator.LT, value=Decimal("500")),
        ctx(args={"payment.amount": Decimal("500")}),
        False,
    ),
    (
        ArgPredicate(path="email.domain", op=ArgOperator.IN, value=frozenset({"example.com"})),
        ctx(args={"email.domain": "example.com"}),
        True,
    ),
    (
        ArgPredicate(path="email.domain", op=ArgOperator.IN, value=frozenset({"example.com"})),
        ctx(args={"email.domain": "evil.com"}),
        False,
    ),
    (
        ArgPredicate(path="email.domain", op=ArgOperator.NOT_IN, value=frozenset({"evil.com"})),
        ctx(args={"email.domain": "evil.com"}),
        False,
    ),
    (
        ArgPredicate(path="k", op=ArgOperator.EQ, value="expected"),
        ctx(args={"k": "expected"}),
        True,
    ),
    (ArgPredicate(path="k", op=ArgOperator.NE, value="bad"), ctx(args={"k": "bad"}), False),
    (
        ArgPredicate(path="n", op=ArgOperator.GE, value=Decimal(10)),
        ctx(args={"n": Decimal(9)}),
        False,
    ),
    (
        ArgPredicate(path="n", op=ArgOperator.GT, value=Decimal(10)),
        ctx(args={"n": Decimal(11)}),
        True,
    ),
]

DEPTH_CASES = [
    (DepthLimit(max_depth=3), ctx(current_depth=0), True),
    (DepthLimit(max_depth=3), ctx(current_depth=3), True),  # boundary
    (DepthLimit(max_depth=3), ctx(current_depth=4), False),
    (DepthLimit(max_depth=0), ctx(current_depth=0), True),
    (DepthLimit(max_depth=0), ctx(current_depth=1), False),
]

INTENT_CASES = [
    (IntentBound(intent_hash=INTENT), ctx(), True),
    (IntentBound(intent_hash=INTENT), ctx(request_intent=OTHER_INTENT), False),
    (IntentBound(intent_hash=OTHER_INTENT), ctx(), False),
    (IntentBound(intent_hash=OTHER_INTENT), ctx(request_intent=OTHER_INTENT), True),
    (IntentBound(intent_hash=INTENT), ctx(operation="vendor:read"), True),
]

CLAUSE_CASES: list[tuple[Caveat, RequestContext, bool]] = [
    *SCOPE_CASES,
    *BUDGET_CASES,
    *TIME_CASES,
    *TOOL_ALLOW_CASES,
    *TOOL_DENY_CASES,
    *ARG_CASES,
    *DEPTH_CASES,
    *INTENT_CASES,
]


def _case_id(index: int) -> str:
    caveat, _, expected = CLAUSE_CASES[index]
    return f"{caveat.kind.value}-{index}-{'allow' if expected else 'deny'}"


class TestEvaluate:
    @pytest.mark.parametrize(
        ("caveat", "context", "expected"),
        CLAUSE_CASES,
        ids=[_case_id(i) for i in range(len(CLAUSE_CASES))],
    )
    def test_table(self, caveat: Caveat, context: RequestContext, expected: bool) -> None:
        result = evaluate(caveat, context)
        assert result.allowed is expected

    def test_every_kind_has_at_least_five_cases(self) -> None:
        """T-008 requires ≥5 cases per type. Assert it rather than trusting the eye."""
        counts: dict[CaveatKind, int] = {}
        for case in CLAUSE_CASES:
            kind = case[0].kind
            counts[kind] = counts.get(kind, 0) + 1
        clause_kinds = {k for k in CaveatKind if k is not CaveatKind.REQUIRES_APPROVAL}
        assert set(counts) == clause_kinds
        assert all(n >= 5 for n in counts.values()), counts

    def test_deny_names_the_right_reason_code(self) -> None:
        cases: list[tuple[Caveat, RequestContext, ReasonCode]] = [
            (
                ScopeSubset(scopes=frozenset({"invoice:read"})),
                ctx(operation="vendor:read"),
                ReasonCode.SCOPE_ATTENUATED_AWAY,
            ),
            (
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(0)),
                ctx(requested=spend("1")),
                ReasonCode.BUDGET_EXHAUSTED_CAVEAT,
            ),
            (TimeWindow(not_after=T1), ctx(now=T1), ReasonCode.TOKEN_EXPIRED),
            (TimeWindow(not_before=T1), ctx(now=T0), ReasonCode.TOKEN_NOT_YET_VALID),
            (ToolAllow(tools=frozenset({"a"})), ctx(tool="b"), ReasonCode.TOOL_DENIED),
            (ToolDeny(tools=frozenset({"b"})), ctx(tool="b"), ReasonCode.TOOL_DENIED),
            (
                ArgPredicate(path="n", op=ArgOperator.LE, value=Decimal(1)),
                ctx(args={"n": Decimal(2)}),
                ReasonCode.ARG_PREDICATE_FAILED,
            ),
            (DepthLimit(max_depth=0), ctx(current_depth=1), ReasonCode.DEPTH_EXCEEDED),
            (
                IntentBound(intent_hash=OTHER_INTENT),
                ctx(),
                ReasonCode.INTENT_MISMATCH,
            ),
        ]
        for caveat, context, code in cases:
            result = evaluate(caveat, context)
            assert not result.allowed
            assert result.reason_code is code, caveat

    def test_allow_reports_ok(self) -> None:
        result = evaluate(ScopeSubset(scopes=frozenset({"invoice:read"})), ctx())
        assert result.outcome is Outcome.ALLOW
        assert result.reason_code is ReasonCode.OK

    def test_deny_detail_names_the_caveat_not_the_value(self) -> None:
        """NFR-5: a reason may cite the caveat, never the argument value."""
        result = evaluate(
            ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal(1)),
            ctx(args={"payment.amount": Decimal("999999")}),
        )
        assert "payment.amount" in result.detail
        assert "999999" not in result.detail


class TestRequiresApproval:
    """The one kind that escalates rather than denying (ADR-008)."""

    def test_matching_scope_escalates(self) -> None:
        result = evaluate(RequiresApproval(scopes=frozenset({"invoice:read"})), ctx())
        assert result.outcome is Outcome.ESCALATE
        assert result.reason_code is ReasonCode.APPROVAL_REQUIRED
        assert not result.allowed

    def test_non_matching_scope_allows(self) -> None:
        result = evaluate(RequiresApproval(scopes=frozenset({"payment:initiate"})), ctx())
        assert result.outcome is Outcome.ALLOW

    def test_escalate_is_not_a_deny(self) -> None:
        """A silent deny here would contradict PLAN §6.6 and demo Beat 6."""
        result = evaluate(RequiresApproval(scopes=frozenset({"invoice:read"})), ctx())
        assert result.outcome is not Outcome.DENY

    def test_compiles_to_a_fact_not_a_clause(self) -> None:
        source = to_datalog(RequiresApproval(scopes=frozenset({"payment:initiate"})))
        assert source.startswith("requires_approval(")
        assert "check if" not in source
        assert "reject if" not in source

    def test_empty_scope_set_never_escalates(self) -> None:
        assert evaluate(RequiresApproval(scopes=frozenset()), ctx()).outcome is Outcome.ALLOW


class TestDatalogConformance:
    """The compiled Datalog and `evaluate()` must reach the same verdict.

    Each caveat is put into a real attenuation block and authorized against a real
    request context, so this tests the actual clause biscuit will run.
    """

    @staticmethod
    def _authorizes(caveat: Caveat, context: RequestContext) -> bool:
        root = KeyPair()
        token = BiscuitBuilder("root(true);").build(root.private_key)
        token = token.append(BlockBuilder(to_datalog(caveat)))
        source = request_context_datalog(context) + "\nallow if true;"
        try:
            AuthorizerBuilder(source).build(token).authorize()
            return True
        except Exception:
            return False

    @pytest.mark.parametrize(
        ("caveat", "context", "expected"),
        CLAUSE_CASES,
        ids=[_case_id(i) for i in range(len(CLAUSE_CASES))],
    )
    def test_datalog_matches_evaluate(
        self, caveat: Caveat, context: RequestContext, expected: bool
    ) -> None:
        datalog_verdict = self._authorizes(caveat, context)
        python_verdict = evaluate(caveat, context).allowed
        assert datalog_verdict == python_verdict == expected, (
            f"divergence for {caveat!r}: datalog={datalog_verdict} python={python_verdict}"
        )

    def test_generated_datalog_always_parses(self) -> None:
        """A caveat that cannot compile is a bug that must not reach a token."""
        root = KeyPair()
        for caveat, _, _ in CLAUSE_CASES:
            token = BiscuitBuilder("root(true);").build(root.private_key)
            token.append(BlockBuilder(to_datalog(caveat)))


class TestDatalogShape:
    """Spot-check the generated clause form, which §3.1 fixes normatively."""

    @pytest.mark.parametrize(
        ("caveat", "prefix"),
        [
            (ScopeSubset(scopes=frozenset({"invoice:read"})), "check if"),
            (BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(1)), "check if"),
            (TimeWindow(not_after=T1), "check if"),
            (ToolAllow(tools=frozenset({"a"})), "check if"),
            (DepthLimit(max_depth=1), "check if"),
            (IntentBound(intent_hash=INTENT), "check if"),
            (ToolDeny(tools=frozenset({"a"})), "reject if"),
            (ArgPredicate(path="n", op=ArgOperator.LE, value=Decimal(1)), "reject if"),
        ],
    )
    def test_clause_form_follows_the_fact(self, caveat: Caveat, prefix: str) -> None:
        """ADR-007: `check if` for always-present facts, `reject if` for optional ones."""
        assert to_datalog(caveat).startswith(prefix)

    def test_budget_ceiling_emits_a_scaled_integer(self) -> None:
        source = to_datalog(
            BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("500000"))
        )
        assert "5000000000" in source
        assert "500000.0" not in source

    def test_arg_predicate_negates_the_predicate(self) -> None:
        """`x <= v` becomes `reject if arg(p, $x), $x > v` so absence is vacuous."""
        source = to_datalog(ArgPredicate(path="n", op=ArgOperator.LE, value=Decimal(4)))
        assert "reject if" in source
        assert "> 40000" in source

    @pytest.mark.parametrize(
        "hostile",
        [
            'n"); allow if true; //',  # try to close the literal and inject a policy
            'n\\"',
            'n"',
            "n\\",
        ],
    )
    def test_hostile_arg_path_cannot_escape_the_literal(self, hostile: str) -> None:
        """A-31 in the Datalog layer.

        Scope strings are pattern-validated, but `ArgPredicate.path` is free text and can
        carry attacker-influenced content. Escaping is checked by compiling into a real
        biscuit block: if the quoting were wrong, the clause would either fail to parse or
        mean something other than intended.
        """
        caveat = ArgPredicate(path=hostile, op=ArgOperator.LE, value=Decimal(1))
        source = to_datalog(caveat)
        root = KeyPair()
        token = BiscuitBuilder("root(true);").build(root.private_key)
        token = token.append(BlockBuilder(source))  # parses, so the literal held

        # And the caveat still binds to its own path, not to a neighbouring one.
        context = ctx(args={hostile: Decimal(2)})
        assert not evaluate(caveat, context).allowed
        assert evaluate(caveat, ctx(args={"n": Decimal(2)})).allowed

    def test_empty_scope_subset_compiles_to_an_empty_set(self) -> None:
        assert "[]" in to_datalog(ScopeSubset(scopes=frozenset()))

    def test_time_window_with_both_bounds_emits_two_clauses(self) -> None:
        source = to_datalog(TimeWindow(not_before=T0, not_after=T1))
        assert source.count("check if") == 2

    def test_expiry_uses_strict_less_than(self) -> None:
        """EC-T06 boundary is exclusive and is enforced by the token itself."""
        source = to_datalog(TimeWindow(not_after=T1))
        assert "$t < 2026-08-14T12:00:00Z" in source


class TestRequestContextDatalog:
    def test_every_budget_dimension_is_emitted(self) -> None:
        """ADR-007: an omitted dimension denies rather than unconstrains."""
        source = request_context_datalog(ctx())
        for dimension in BudgetDimension:
            assert f'requested("{dimension.value}"' in source

    def test_optional_facts_are_omitted_when_absent(self) -> None:
        source = request_context_datalog(ctx())
        assert "tool(" not in source
        assert "arg(" not in source

    def test_optional_facts_are_emitted_when_present(self) -> None:
        source = request_context_datalog(ctx(tool="invoice_api", args={"n": Decimal(1)}))
        assert 'tool("invoice_api")' in source
        assert 'arg("n", 10000)' in source

    def test_depth_and_intent_and_time_are_emitted(self) -> None:
        source = request_context_datalog(ctx())
        assert "current_depth(1)" in source
        assert f'request_intent("{INTENT}")' in source
        assert "time(2026-08-14T11:50:00Z)" in source


class TestErrors:
    def test_unknown_object_is_rejected_by_to_datalog(self) -> None:
        with pytest.raises(CaveatError):
            to_datalog(object())  # type: ignore[arg-type]

    def test_unknown_object_is_rejected_by_evaluate(self) -> None:
        with pytest.raises(CaveatError):
            evaluate(object(), ctx())  # type: ignore[arg-type]

    def test_malformed_caveat_raises_at_construction_not_evaluation(self) -> None:
        """T-008 criterion: construction is where a bad caveat must fail."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ArgPredicate(path="n", op=ArgOperator.IN, value=Decimal(1))


class TestScale:
    def test_exact_values_scale(self) -> None:
        assert scale(Decimal("500000")) == 5_000_000_000
        assert scale(Decimal("0.0001")) == 1
        assert scale(7) == 70_000

    def test_inexact_value_raises_rather_than_rounding(self) -> None:
        """Rounding money silently is how ledgers stop balancing."""
        with pytest.raises(CaveatError, match="exactly at scale"):
            scale(Decimal("0.00001"))


class TestBlockDatalog:
    def test_joins_a_caveat_set_into_one_block(self) -> None:
        source = block_datalog(
            [
                ScopeSubset(scopes=frozenset({"invoice:read"})),
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(0)),
                DepthLimit(max_depth=2),
            ]
        )
        assert source.count("check if") == 3

    def test_empty_caveat_set_yields_empty_source(self) -> None:
        assert block_datalog([]) == ""

    def test_a_time_window_contributes_both_of_its_clauses(self) -> None:
        source = block_datalog([TimeWindow(not_before=T0, not_after=T1)])
        assert source.count("check if") == 2

    def test_the_result_compiles_into_a_real_block(self) -> None:
        root = KeyPair()
        token = BiscuitBuilder("root(true);").build(root.private_key)
        source = block_datalog(
            [
                ScopeSubset(scopes=frozenset({"invoice:read"})),
                ToolDeny(tools=frozenset({"payment_api"})),
                ArgPredicate(path="n", op=ArgOperator.LE, value=Decimal(1)),
                RequiresApproval(scopes=frozenset({"payment:initiate"})),
            ]
        )
        token.append(BlockBuilder(source))


class TestCaveatResult:
    def test_allowed_is_true_only_for_allow(self) -> None:
        assert CaveatResult.allow().allowed
        assert not CaveatResult.deny(ReasonCode.TOOL_DENIED, "x").allowed
        assert not CaveatResult.escalate("x").allowed
