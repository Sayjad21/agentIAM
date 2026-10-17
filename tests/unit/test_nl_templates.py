"""The offline policy compiler — APICTA-01 Part D, T-031.

When the model is unreachable, slow, or switched off, a sentence in one of the demo's policy
shapes still compiles: a deterministic matcher turns it into the Cedar statements it adds,
plus tests that pin what it means. The statements are *added* to the current policy — the
existing policy already permits ordinary work — so every check here runs against the real
corpus policy with the rule appended, the way the console evaluates it.
"""

from __future__ import annotations

from decimal import Decimal

import cedarpy
import pytest

from agentiam_controlplane.app import evaluate_case
from agentiam_controlplane.nl_compiler.templates import SHAPES, compose, match
from agentiam_core.corpus import CORPUS, CORPUS_SOURCE
from agentiam_core.policy_testing import run_policy_tests, summarize


def _composed(sentence: str) -> tuple[str, cedarpy.PolicySet]:
    found = match(sentence)
    assert found is not None, f"no template matched: {sentence!r}"
    source = compose(CORPUS_SOURCE, found)
    return source, cedarpy.PolicySet.from_str(source)


def _own_tests_pass(sentence: str) -> None:
    found = match(sentence)
    assert found is not None
    _, engine = _composed(sentence)
    assert found.tests, "a template must say what it means with tests"
    expectations = {t.expected for t in found.tests}
    assert expectations == {True, False}, "tests must cover an allow and a deny"
    for test in found.tests:
        verdict = evaluate_case(engine, test.as_policy_test_case())
        assert verdict.allowed == test.expected, f"{test.name}: {test.description}"


class TestQuarantine:
    @pytest.mark.parametrize(
        "sentence",
        [
            "Quarantine agent agt-payer.",
            "block agt-payer",
            "Suspend agt-payer immediately",
            "agt-payer must be stopped from doing anything",
        ],
    )
    def test_it_matches_and_its_tests_pass(self, sentence: str) -> None:
        _own_tests_pass(sentence)

    def test_it_forbids_exactly_that_agent(self) -> None:
        found = match("quarantine agt-payer")
        assert found is not None
        assert 'principal == Agent::"agt-payer"' in found.cedar

    def test_it_passes_the_corpus(self) -> None:
        # The corpus pins ordinary work, none of it by agent name.
        _, engine = _composed("quarantine agt-payer")
        assert summarize(run_policy_tests(CORPUS, lambda c: evaluate_case(engine, c))).all_passed


class TestPaymentCap:
    @pytest.mark.parametrize(
        "sentence",
        [
            "No payments over 100000",
            "Agents must never pay more than 250,000 taka",
            "Forbid payments above ৳300000",
            "payments exceeding 1 lakh are not allowed",
            "Block payments greater than 50k BDT",
        ],
    )
    def test_it_matches_and_its_tests_pass(self, sentence: str) -> None:
        _own_tests_pass(sentence)

    @pytest.mark.parametrize(
        ("sentence", "limit"),
        [
            ("payments exceeding 1 lakh are not allowed", "100000.0"),
            ("Block payments greater than 50k BDT", "50000.0"),
            ("Agents must never pay more than 250,000 taka", "250000.0"),
        ],
    )
    def test_the_amount_is_read_correctly(self, sentence: str, limit: str) -> None:
        found = match(sentence)
        assert found is not None
        assert f'decimal("{limit}")' in found.cedar

    def test_unless_approved_exempts_an_elevated_request(self) -> None:
        found = match("Payments over 40000 need approval")
        assert found is not None
        assert "!context.elevated" in found.cedar
        _own_tests_pass("Payments over 40000 need approval")

    def test_a_cap_that_breaks_an_approved_workflow_is_refused_by_the_corpus(self) -> None:
        """The demo point: the gate names the approved workflow a new rule would break."""
        _, engine = _composed("No payments over 100000")
        summary = summarize(run_policy_tests(CORPUS, lambda c: evaluate_case(engine, c)))
        assert not summary.all_passed
        assert any("ceiling" in r.case.name for r in summary.failures)


class TestRoleRestriction:
    @pytest.mark.parametrize(
        "sentence",
        [
            "Only senior agents may send email",
            "only auditors can read vendors",
            "Only senior agents may write invoices",
        ],
    )
    def test_it_matches_and_its_tests_pass(self, sentence: str) -> None:
        _own_tests_pass(sentence)

    def test_it_forbids_everyone_else_rather_than_permitting_the_role(self) -> None:
        # A permit for the role would add nothing: the current policy already permits the
        # action for everyone. Only a forbid for the others restricts anything.
        found = match("only auditors can read vendors")
        assert found is not None
        assert 'principal.role != "auditor"' in found.cedar
        assert found.cedar.lstrip().startswith("forbid")


class TestDepthLimit:
    @pytest.mark.parametrize(
        "sentence",
        [
            "No invoice reads deeper than depth 2",
            "Agents deeper than 3 may not read vendors",
            "no payments beyond depth 1",
        ],
    )
    def test_it_matches_and_its_tests_pass(self, sentence: str) -> None:
        _own_tests_pass(sentence)

    def test_the_bound_is_strict(self) -> None:
        found = match("Agents deeper than 3 may not read vendors")
        assert found is not None
        assert "principal.depth > 3" in found.cedar


class TestExternalEmail:
    @pytest.mark.parametrize(
        "sentence", ["No external email", "Agents must never email outside the company"]
    )
    def test_it_matches_and_its_tests_pass(self, sentence: str) -> None:
        _own_tests_pass(sentence)


class TestWhatItDoesNotGuess:
    @pytest.mark.parametrize(
        "sentence",
        [
            "Make the system more secure",
            "Agents should be careful with money",
            "Only the CFO's agents may approve things on Tuesdays",
            "",
        ],
    )
    def test_an_unrecognised_sentence_matches_nothing(self, sentence: str) -> None:
        # Guessing a policy from a sentence it does not understand is worse than saying so.
        assert match(sentence) is None

    def test_every_shape_is_described_for_the_operator(self) -> None:
        assert len(SHAPES) >= 5
        assert all(example for _, example in SHAPES)


class TestComposition:
    def test_the_added_rule_is_labelled_and_the_current_policy_kept(self) -> None:
        source, _ = _composed("quarantine agt-payer")
        assert source.startswith(CORPUS_SOURCE.rstrip())
        assert "// added by the offline compiler" in source

    def test_amount_parsing_rejects_nonsense(self) -> None:
        assert match("No payments over lots") is None

    def test_a_zero_cap_is_still_a_cap(self) -> None:
        found = match("No payments over 0")
        assert found is not None
        assert 'decimal("0.0")' in found.cedar
        assert Decimal(0) == Decimal("0.0")
