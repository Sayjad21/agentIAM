"""The Cedar policy engine — spec 05, T-024.

Step 5 of the pipeline: *what does the organization permit at all, regardless of token?*

The acceptance criterion is a conformance suite of at least 30 request/expectation pairs, and
`TestConformanceCorpus` is it — one realistic bundle, exercised from every direction. The
tests worth reading first are `TestNoDecisionFailsClosed`, because the natural spelling of the
decision check lets a corrupt bundle through, and `TestBundleIsParsedOnce`, because the naive
arrangement costs 17% of NFR-1's budget.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import pytest

from agentiam_core.decision import OracleUnavailable
from agentiam_core.models import BudgetDimension, RequestContext
from agentiam_core.policy_testing import PolicyTestCase
from agentiam_pep.policy import (
    AgentPrincipal,
    CedarEngine,
    OpaEngine,
    PolicyBundle,
    PolicyBundleError,
    ToolFacts,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
TASK = uuid.uuid4()

#: A bundle shaped like an organization's actual rules rather than a syntax demo.
SOURCE = """
permit(principal, action == Action::"invoice:read", resource);
permit(principal, action == Action::"vendor:read", resource);

permit(principal, action == Action::"invoice:write", resource)
when { principal.role == "senior" };

permit(principal, action == Action::"payment:initiate", resource)
when {
  context.amount.lessThanOrEqual(decimal("500000.0")) && principal.depth <= 2
};

permit(principal, action == Action::"email:send", resource)
when { !resource.is_external };

forbid(principal, action, resource)
when { resource.sensitivity == "critical" && principal.role != "senior" };

forbid(principal, action == Action::"payment:initiate", resource)
when { decimal("1000000.0").lessThan(context.amount) };
"""

TOOLS = {
    "invoice_api": ToolFacts(tool_id="invoice_api", server="erp", sensitivity="low"),
    "vendor_api": ToolFacts(tool_id="vendor_api", server="erp", sensitivity="low"),
    "payment_api": ToolFacts(
        tool_id="payment_api", server="bank", sensitivity="critical", is_external=True
    ),
    "email_internal": ToolFacts(tool_id="email_internal", server="mail", sensitivity="low"),
    "email_external": ToolFacts(
        tool_id="email_external", server="mail", sensitivity="medium", is_external=True
    ),
}


def a_bundle(source: str = SOURCE, version: str = "bundle-1") -> PolicyBundle:
    return PolicyBundle(version=version, cedar_source=source)


def an_engine(**over: object) -> CedarEngine:
    return CedarEngine(a_bundle(), tools=TOOLS, **over)  # type: ignore[arg-type]


def a_principal(role: str = "worker") -> AgentPrincipal:
    return AgentPrincipal(agent_id="agt-1", role=role, principal_id="kc:alice", task_id=TASK)


def ctx(
    operation: str,
    *,
    tool: str | None = "invoice_api",
    amount: str = "0",
    depth: int = 1,
) -> RequestContext:
    requested = dict.fromkeys(BudgetDimension, Decimal(0))
    requested[BudgetDimension.SPEND_BDT] = Decimal(amount)
    return RequestContext(
        operation=operation,
        requested=requested,
        current_depth=depth,
        request_intent="a" * 64,
        now=NOW,
        tool=tool,
        args={},
    )


class TestConformanceCorpus:
    """The ≥30 request/expectation pairs T-024 asks for.

    Each row is `(role, operation, tool, amount, depth, expected_allow, why)`. The `why`
    column is not decoration — a corpus whose rows nobody can explain is a corpus nobody will
    maintain when a policy changes.
    """

    CASES: ClassVar[list[tuple[str, str, str | None, str, int, bool, str]]] = [
        # --- plain permits, no conditions -------------------------------------------
        ("worker", "invoice:read", "invoice_api", "0", 1, True, "unconditional permit"),
        ("worker", "vendor:read", "vendor_api", "0", 1, True, "unconditional permit"),
        ("senior", "invoice:read", "invoice_api", "0", 1, True, "role is irrelevant here"),
        ("worker", "invoice:read", "invoice_api", "0", 8, True, "depth is irrelevant here"),
        # --- nothing matches: deny by default ---------------------------------------
        ("worker", "admin:write", "invoice_api", "0", 1, False, "no permit mentions it"),
        ("senior", "admin:write", "invoice_api", "0", 1, False, "seniority is not a permit"),
        ("worker", "vendor:negotiate", "vendor_api", "0", 1, False, "no permit mentions it"),
        # --- role-conditioned permit -------------------------------------------------
        ("senior", "invoice:write", "invoice_api", "0", 1, True, "role satisfies the guard"),
        ("worker", "invoice:write", "invoice_api", "0", 1, False, "role fails the guard"),
        ("", "invoice:write", "invoice_api", "0", 1, False, "empty role fails the guard"),
        # --- amount- and depth-conditioned permit ------------------------------------
        ("worker", "payment:initiate", "invoice_api", "1000", 1, True, "under both limits"),
        ("worker", "payment:initiate", "invoice_api", "500000", 1, True, "exactly at the ceiling"),
        ("worker", "payment:initiate", "invoice_api", "500001", 1, False, "one over the ceiling"),
        ("worker", "payment:initiate", "invoice_api", "0", 2, True, "exactly at max depth"),
        ("worker", "payment:initiate", "invoice_api", "0", 3, False, "one past max depth"),
        ("worker", "payment:initiate", "invoice_api", "0", 8, False, "far past max depth"),
        ("senior", "payment:initiate", "invoice_api", "1000", 1, True, "seniority does not bypass"),
        ("senior", "payment:initiate", "invoice_api", "500001", 1, False, "nor does it raise it"),
        # --- resource-conditioned permit ---------------------------------------------
        ("worker", "email:send", "email_internal", "0", 1, True, "internal tool permitted"),
        ("worker", "email:send", "email_external", "0", 1, False, "external tool is not"),
        ("senior", "email:send", "email_external", "0", 1, False, "role does not override it"),
        # --- forbid beats permit ------------------------------------------------------
        (
            "worker",
            "invoice:read",
            "payment_api",
            "0",
            1,
            False,
            "critical tool, non-senior: forbid wins over the unconditional permit",
        ),
        (
            "senior",
            "invoice:read",
            "payment_api",
            "0",
            1,
            True,
            "critical tool, senior: the forbid's guard does not fire",
        ),
        (
            "worker",
            "payment:initiate",
            "payment_api",
            "1000",
            1,
            False,
            "would be permitted on amount, but the sensitivity forbid fires",
        ),
        (
            "senior",
            "payment:initiate",
            "payment_api",
            "1000",
            1,
            True,
            "senior escapes the sensitivity forbid and is under the ceiling",
        ),
        (
            "senior",
            "payment:initiate",
            "payment_api",
            "1000001",
            1,
            False,
            "the amount forbid applies to everyone, including seniors",
        ),
        (
            "senior",
            "payment:initiate",
            "invoice_api",
            "1000001",
            1,
            False,
            "and it is not about the tool",
        ),
        # --- boundaries on the second forbid -----------------------------------------
        (
            "senior",
            "payment:initiate",
            "invoice_api",
            "1000000",
            1,
            False,
            "at the forbid boundary the permit's own ceiling has already failed",
        ),
        # --- tools the catalogue does not know ---------------------------------------
        ("worker", "invoice:read", "unknown_tool", "0", 1, True, "unknown tool defaults to safe"),
        ("worker", "invoice:read", None, "0", 1, True, "a call with no tool at all"),
        # --- depth boundaries on an unconditioned action -----------------------------
        ("worker", "vendor:read", "vendor_api", "999999", 1, True, "amount is irrelevant here"),
        ("worker", "vendor:read", "payment_api", "0", 1, False, "sensitivity forbid still fires"),
    ]

    @pytest.mark.parametrize(
        ("role", "operation", "tool", "amount", "depth", "expected", "why"), CASES
    )
    def test_case(
        self,
        role: str,
        operation: str,
        tool: str | None,
        amount: str,
        depth: int,
        expected: bool,
        why: str,
    ) -> None:
        engine = an_engine().bound(a_principal(role))
        verdict = engine.evaluate(ctx(operation, tool=tool, amount=amount, depth=depth))
        assert verdict.allowed is expected, why

    def test_the_corpus_is_at_least_thirty_cases(self) -> None:
        """`PLAN.md` T-024 asks for ≥30, and a corpus that quietly shrinks is a weakened test."""
        assert len(self.CASES) >= 30

    def test_the_corpus_exercises_both_outcomes(self) -> None:
        """All-allow or all-deny would pass against a broken engine."""
        outcomes = {case[5] for case in self.CASES}
        assert outcomes == {True, False}


class TestNamingTheCause:
    """`PLAN.md` §3.2 principle 4 — every deny names what caused it."""

    def test_a_denial_names_the_deciding_policy(self) -> None:
        engine = an_engine().bound(a_principal("worker"))
        verdict = engine.evaluate(ctx("invoice:read", tool="payment_api"))
        assert not verdict.allowed
        assert verdict.statement, "spec 09 puts this in reason_detail; empty is a bug"

    def test_an_allow_also_names_the_policy_that_permitted_it(self) -> None:
        """Useful in the console: *why was this allowed?* is asked as often as *why not?*."""
        engine = an_engine().bound(a_principal())
        verdict = engine.evaluate(ctx("invoice:read"))
        assert verdict.allowed
        assert verdict.statement

    def test_a_default_deny_has_no_statement_to_name(self) -> None:
        """Nothing matched, so there is no policy to blame — and `None` says so honestly."""
        engine = an_engine().bound(a_principal())
        verdict = engine.evaluate(ctx("admin:write"))
        assert not verdict.allowed
        assert verdict.statement is None

    def test_the_bundle_version_is_reported(self) -> None:
        engine = CedarEngine(a_bundle(version="bundle-7"), tools=TOOLS).bound(a_principal())
        assert engine.evaluate(ctx("invoice:read")).version == "bundle-7"


class TestNoDecisionFailsClosed:
    """Spec 05 §4 — `cedarpy.Decision` has three members and the third is a trap."""

    def test_a_bundle_that_cannot_parse_is_refused_at_load(self) -> None:
        """So `NoDecision` is unreachable in production rather than merely handled."""
        with pytest.raises(PolicyBundleError, match="parse"):
            CedarEngine(a_bundle(source="this is not cedar at all"), tools=TOOLS)

    def test_an_empty_bundle_loads_and_denies_everything(self) -> None:
        """Deny by default, measured rather than assumed."""
        engine = CedarEngine(a_bundle(source=""), tools=TOOLS).bound(a_principal())
        assert not engine.evaluate(ctx("invoice:read")).allowed

    def test_anything_that_is_not_allow_is_a_denial(self) -> None:
        """The guard against a future Cedar release adding a fourth decision value.

        Written as `decision is Allow` rather than `decision == Deny`, so an unrecognised
        outcome fails closed instead of falling through as "not denied".
        """
        engine = an_engine()

        class ThirdThing:
            pass

        verdict = engine._verdict_from(ThirdThing(), [])
        assert not verdict.allowed


class TestStaleness:
    """The bundle can be loaded and still not trustworthy — spec 09 checks this first."""

    def test_a_stale_engine_reports_stale(self) -> None:
        engine = an_engine(stale=True).bound(a_principal())
        assert engine.evaluate(ctx("invoice:read")).stale

    def test_a_fresh_engine_does_not(self) -> None:
        assert not an_engine().bound(a_principal()).evaluate(ctx("invoice:read")).stale


class TestBundleIsParsedOnce:
    """Spec 05 §6 — the naive arrangement costs 17% of NFR-1's budget."""

    def test_the_policy_set_is_built_at_construction(self) -> None:
        engine = an_engine()
        assert engine.policy_set is not None

    def test_two_engines_over_one_bundle_do_not_share_mutable_state(self) -> None:
        bundle = a_bundle()
        first = CedarEngine(bundle, tools=TOOLS).bound(a_principal("worker"))
        second = CedarEngine(bundle, tools=TOOLS).bound(a_principal("senior"))
        assert not first.evaluate(ctx("invoice:write")).allowed
        assert second.evaluate(ctx("invoice:write")).allowed


class TestBoundEngine:
    def test_binding_does_not_reparse_the_bundle(self) -> None:
        engine = an_engine()
        assert engine.bound(a_principal()).policy_set is engine.policy_set

    def test_two_bindings_of_one_engine_are_independent(self) -> None:
        engine = an_engine()
        worker = engine.bound(a_principal("worker"))
        senior = engine.bound(a_principal("senior"))
        assert not worker.evaluate(ctx("invoice:write")).allowed
        assert senior.evaluate(ctx("invoice:write")).allowed

    def test_an_unbound_engine_cannot_evaluate(self) -> None:
        """`evaluate()` needs the token's facts, which only the PEP has (spec 05 §2)."""
        assert not hasattr(an_engine(), "evaluate")


class TestOpaEngineStub:
    """Spec 05 §7 — the seam is demonstrated, not asserted."""

    def test_it_satisfies_the_protocol_shape(self) -> None:
        assert hasattr(OpaEngine, "evaluate")

    def test_it_raises_rather_than_pretending(self) -> None:
        with pytest.raises(NotImplementedError, match="OPA"):
            OpaEngine(endpoint="http://opa:8181").evaluate(ctx("invoice:read"))

    def test_the_error_says_where_the_work_is(self) -> None:
        with pytest.raises(NotImplementedError, match=r"§21|deferred"):
            OpaEngine(endpoint="http://opa:8181").evaluate(ctx("invoice:read"))


class TestOracleUnavailable:
    """`decide()` catches this and fails closed with its own reason code."""

    def test_an_engine_with_no_bundle_is_unavailable(self) -> None:
        engine = CedarEngine.unavailable("bundle never fetched").bound(a_principal())
        with pytest.raises(OracleUnavailable, match="never fetched"):
            engine.evaluate(ctx("invoice:read"))


@pytest.mark.perf
class TestPolicyCost:
    """Spec 05 §6, and the number that changed what T-019 could claim about NFR-1."""

    def test_one_authorize_stays_inside_the_budget(self, benchmark: object) -> None:
        engine = an_engine().bound(a_principal())
        context = ctx("payment:initiate", amount="1000")

        benchmark(lambda: engine.evaluate(context))  # type: ignore[operator]

        timings = sorted(benchmark.stats.stats.data)  # type: ignore[attr-defined]
        p99 = timings[int(len(timings) * 0.99)]
        assert p99 < 0.0005, (
            f"p99 was {p99 * 1e6:.0f} us; step 5 must stay a minority of NFR-1's 1 ms, and "
            f"the pre-parsed arrangement measured ~80 us"
        )


# ---------------------------------------------------------------------------
# T-026: The 50-case corpus, run against the real engine
# ---------------------------------------------------------------------------


class TestT026Corpus:
    """The ≥50 case corpus from T-026, validated against the real Cedar engine.

    This is the second half of T-026's acceptance criterion: the corpus must be
    *correct* — every case must produce the expected verdict against the demo's
    organization policy.
    """

    def test_the_corpus_has_at_least_fifty_cases(self) -> None:
        """`PLAN.md` T-026 requires ≥50 cases derived from demo workflows."""
        from agentiam_pep.corpus import CORPUS

        assert len(CORPUS) >= 50, f"T-026 requires ≥50; have {len(CORPUS)}"

    def test_the_corpus_exercises_both_outcomes(self) -> None:
        """All-allow or all-deny would pass against a broken engine."""
        from agentiam_pep.corpus import CORPUS

        outcomes = {case.expected for case in CORPUS}
        assert outcomes == {True, False}

    def test_every_case_has_a_name_and_description(self) -> None:
        """A corpus whose rows nobody can explain is a corpus nobody will maintain."""
        from agentiam_pep.corpus import CORPUS

        for case in CORPUS:
            assert case.name, f"case at index {CORPUS.index(case)} has no name"
            assert case.description, f"{case.name} has no description"

    def test_every_case_has_tags(self) -> None:
        """Tags trace each case to a demo beat or safety category."""
        from agentiam_pep.corpus import CORPUS

        for case in CORPUS:
            assert case.tags, f"{case.name} has no tags"

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(c, id=c.name)
            for c in __import__("agentiam_pep.corpus", fromlist=["CORPUS"]).CORPUS
        ],
    )
    def test_case_against_real_engine(self, case: PolicyTestCase) -> None:
        """Every corpus case must produce the expected verdict against the real engine."""
        engine = an_engine().bound(a_principal(case.role))
        verdict = engine.evaluate(
            ctx(
                case.operation,
                tool=case.tool,
                amount=str(case.amount),
                depth=case.depth,
            )
        )
        assert verdict.allowed is case.expected, (
            f"{case.name}: expected {'allow' if case.expected else 'deny'}, "
            f"got {'allow' if verdict.allowed else 'deny'} — {case.description}"
        )
