"""The invariant checker's pure parts — reporting and the CLI's exit contract.

Everything that needs a database lives in `tests/integration/test_invariant_checker.py`.
What is here is the shape of the output, which matters more than usual: this tool is on
screen during demo Beat 4 as a live green/red indicator (T-047) and it runs as a sidecar
through every chaos scenario (T-052), so its rendering and its exit code are contracts of
their own.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from agentiam_controlplane.db.invariants import (
    CheckReport,
    InvariantKind,
    Violation,
)

MANDATE = uuid.UUID("9f2c1e40-7a3b-4d21-9c88-1b2e5f0a4d77")
BUDGET = uuid.UUID("1b2e5f0a-4d77-4d21-9c88-9f2c1e407a3b")


def a_violation(
    kind: InvariantKind = InvariantKind.COMMITTED_VS_RESERVATIONS,
    *,
    expected: Decimal = Decimal("40.0000"),
    actual: Decimal = Decimal("50.0000"),
) -> Violation:
    return Violation(
        kind=kind,
        budget_id=BUDGET,
        mandate_id=MANDATE,
        dimension="spend_bdt",
        expected=expected,
        actual=actual,
    )


class TestViolationRendering:
    def test_it_is_a_single_line(self) -> None:
        assert "\n" not in str(a_violation())

    def test_it_names_the_mandate_the_dimension_and_both_numbers(self) -> None:
        rendered = str(a_violation())
        assert str(MANDATE) in rendered
        assert "spend_bdt" in rendered
        assert "40.0000" in rendered
        assert "50.0000" in rendered

    def test_it_names_which_invariant_broke(self) -> None:
        assert InvariantKind.POOL.value in str(a_violation(InvariantKind.POOL))

    def test_it_reports_the_signed_difference(self) -> None:
        """Over- and under-counting are different bugs; the sign says which."""
        assert a_violation(expected=Decimal("40"), actual=Decimal("50")).delta == Decimal("10")
        assert a_violation(expected=Decimal("50"), actual=Decimal("40")).delta == Decimal("-10")

    def test_it_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            a_violation().actual = Decimal("1")  # type: ignore[misc]


class TestCheckReport:
    def test_no_violations_means_it_holds(self) -> None:
        assert CheckReport(budgets_checked=3, violations=(), duration_ms=1.0).holds

    def test_any_violation_means_it_does_not(self) -> None:
        report = CheckReport(budgets_checked=3, violations=(a_violation(),), duration_ms=1.0)
        assert not report.holds

    def test_an_empty_ledger_holds_rather_than_failing(self) -> None:
        """Nothing to check is not a violation. A chaos run starts here."""
        assert CheckReport(budgets_checked=0, violations=(), duration_ms=0.2).holds

    def test_the_summary_line_is_readable_when_green(self) -> None:
        summary = CheckReport(budgets_checked=42, violations=(), duration_ms=3.5).summary()
        assert "42" in summary
        assert "\n" not in summary

    def test_the_summary_line_counts_violations_when_red(self) -> None:
        report = CheckReport(
            budgets_checked=42,
            violations=(a_violation(), a_violation(InvariantKind.POOL)),
            duration_ms=3.5,
        )
        assert "2" in report.summary()

    def test_violations_are_a_tuple_so_a_report_cannot_be_edited_after_the_fact(self) -> None:
        report = CheckReport(budgets_checked=1, violations=(a_violation(),), duration_ms=1.0)
        assert isinstance(report.violations, tuple)


class TestExitCode:
    """A chaos run and CI both branch on this, so it is a contract."""

    def test_green_exits_zero(self) -> None:
        assert CheckReport(budgets_checked=9, violations=(), duration_ms=1.0).exit_code == 0

    def test_red_exits_nonzero(self) -> None:
        report = CheckReport(budgets_checked=9, violations=(a_violation(),), duration_ms=1.0)
        assert report.exit_code == 1


class TestInvariantKind:
    def test_every_kind_has_a_human_readable_description(self) -> None:
        """The console renders this next to the indicator; a bare enum name will not do."""
        for kind in InvariantKind:
            assert kind.description
            assert kind.description != kind.value

    def test_the_kinds_are_the_ones_the_tickets_name_plus_negatives(self) -> None:
        """Closed set. `allocated_vs_children` joined it with T-017's proportional split.

        Pinned rather than left open because the console filters on these values and a
        kind added without a matching filter is invisible on screen — which for this tool
        is the same as not detecting it.
        """
        assert {k.value for k in InvariantKind} == {
            "pool",
            "committed_vs_reservations",
            "leased_vs_active_leases",
            "allocated_vs_children",
            "negative_balance",
        }
