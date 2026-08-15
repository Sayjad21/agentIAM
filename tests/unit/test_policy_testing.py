"""The policy test runner — T-026, spec 05 §5.5.

Tests for the pure-computation side: `run_policy_tests` and `summarize`. These are
independent of Cedar and the PEP — they test the counting and reporting logic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agentiam_core.decision import PolicyVerdict
from agentiam_core.policy_testing import (
    PolicyTestCase,
    PolicyTestResult,
    run_policy_tests,
    summarize,
)


def _case(
    name: str = "c1",
    *,
    expected: bool = True,
    operation: str = "invoice:read",
    role: str = "worker",
) -> PolicyTestCase:
    return PolicyTestCase(
        name=name,
        description="a test case",
        operation=operation,
        expected=expected,
        role=role,
    )


def _always_allow(case: PolicyTestCase) -> PolicyVerdict:
    return PolicyVerdict(allowed=True, statement="policy0", version="v1", stale=False)


def _always_deny(case: PolicyTestCase) -> PolicyVerdict:
    return PolicyVerdict(allowed=False, statement="forbid0", version="v1", stale=False)


def _explode(case: PolicyTestCase) -> PolicyVerdict:
    msg = "engine is broken"
    raise RuntimeError(msg)


class TestRunPolicyTests:
    """The runner executes every case and never short-circuits."""

    def test_all_passing(self) -> None:
        cases = [_case("c1", expected=True), _case("c2", expected=True)]
        results = run_policy_tests(cases, _always_allow)
        assert all(r.passed for r in results)
        assert len(results) == 2

    def test_a_failing_case(self) -> None:
        cases = [_case("c1", expected=False)]  # expects deny, gets allow
        results = run_policy_tests(cases, _always_allow)
        assert not results[0].passed
        assert results[0].actual is True

    def test_an_error_is_recorded_not_raised(self) -> None:
        """A broken engine must not crash the corpus run — report, don't crash."""
        cases = [_case("c1")]
        results = run_policy_tests(cases, _explode)
        assert len(results) == 1
        assert not results[0].passed
        assert results[0].error is not None
        assert "broken" in results[0].error

    def test_never_short_circuits(self) -> None:
        """All cases run even after a failure — the report needs all of them."""
        call_count = 0

        def counting_allow(case: PolicyTestCase) -> PolicyVerdict:
            nonlocal call_count
            call_count += 1
            return PolicyVerdict(allowed=True, statement=None, version="v1", stale=False)

        cases = [_case("c1", expected=False), _case("c2", expected=True)]
        results = run_policy_tests(cases, counting_allow)
        assert call_count == 2
        assert not results[0].passed
        assert results[1].passed

    def test_statement_is_captured(self) -> None:
        results = run_policy_tests([_case()], _always_allow)
        assert results[0].statement == "policy0"

    def test_empty_corpus_returns_empty_results(self) -> None:
        results = run_policy_tests([], _always_allow)
        assert results == []


class TestSummarize:
    """Counting logic and the gate decision."""

    def test_all_pass(self) -> None:
        results = run_policy_tests([_case(expected=True)], _always_allow)
        summary = summarize(results)
        assert summary.all_passed
        assert summary.total == 1
        assert summary.passed == 1
        assert summary.failed == 0
        assert summary.errors == 0
        assert summary.failures == ()

    def test_one_failure(self) -> None:
        results = run_policy_tests([_case(expected=False)], _always_allow)
        summary = summarize(results)
        assert not summary.all_passed
        assert summary.failed == 1
        assert len(summary.failures) == 1

    def test_an_error_counts_as_failure(self) -> None:
        results = run_policy_tests([_case()], _explode)
        summary = summarize(results)
        assert not summary.all_passed
        assert summary.errors == 1
        assert summary.failed == 1

    def test_empty_corpus_passes_vacuously(self) -> None:
        """Zero tests means none fail. The operator should see `total=0` and be warned."""
        summary = summarize([])
        assert summary.all_passed
        assert summary.total == 0

    def test_mixed_results(self) -> None:
        passing = PolicyTestResult(case=_case("pass", expected=True), actual=True, passed=True)
        failing = PolicyTestResult(case=_case("fail", expected=False), actual=True, passed=False)
        errored = PolicyTestResult(case=_case("err"), actual=False, passed=False, error="boom")
        summary = summarize([passing, failing, errored])
        assert summary.total == 3
        assert summary.passed == 1
        assert summary.failed == 2
        assert summary.errors == 1
        assert not summary.all_passed
        assert len(summary.failures) == 2


class TestPolicyTestCase:
    """The case model — frozen, with sensible defaults."""

    def test_is_frozen(self) -> None:
        case = _case()
        with pytest.raises(AttributeError):
            case.name = "mutated"  # type: ignore[misc]

    def test_defaults(self) -> None:
        case = _case()
        assert case.role == "worker"
        assert case.depth == 1
        assert case.amount == Decimal(0)
        assert case.tags == ()
        assert case.tool == "invoice_api"

    def test_tags_are_a_tuple(self) -> None:
        """Tuples, not lists — frozen dataclass, hashable."""
        case = PolicyTestCase(
            name="tagged",
            description="test",
            operation="invoice:read",
            expected=True,
            tags=("demo-beat-3", "least-privilege"),
        )
        assert "demo-beat-3" in case.tags
