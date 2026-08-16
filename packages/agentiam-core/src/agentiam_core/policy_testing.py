"""Policy test cases and the runner — spec 05 §5.5, T-026.

A policy test case describes one request scenario and its expected Cedar outcome.
`run_policy_tests` evaluates a list of cases against a `PolicyEngine` and returns
results. `summarize` counts them.

This lives in core because it is pure computation — no I/O, no clock, no network —
and the activation gate that consumes it exists on the PEP side, while a future
control-plane publish endpoint will need it too. Both depend on `agentiam-core`
already; neither should depend on the other.

Rule 4: money is always `Decimal`, never `float`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agentiam_core.decision import PolicyVerdict

__all__ = [
    "PolicyTestCase",
    "PolicyTestResult",
    "PolicyTestSummary",
    "run_policy_tests",
    "summarize",
]


@dataclass(frozen=True, slots=True)
class PolicyTestCase:
    """One scenario: a request and the verdict the organization's policy must produce.

    `name` is an identifier (`"worker_can_read_invoices"`), not prose. `description`
    is the `why` column — not decoration; a corpus whose rows nobody can explain is a
    corpus nobody will maintain when the policy changes.
    """

    name: str
    description: str
    operation: str
    expected: bool
    role: str = "worker"
    tool: str | None = "invoice_api"
    amount: Decimal = Decimal(0)
    depth: int = 1
    agent_id: str = "agt-1"
    principal_id: str = "kc:alice"
    task_id: str = "00000000-0000-0000-0000-000000000000"
    tags: tuple[str, ...] = ()
    #: Whether an approver elevated this request (`context.elevated`). Defaults to the
    #: value the evaluator previously hardcoded, so the existing corpus is unaffected.
    elevated: bool = False
    #: `context.environment`. Same reasoning: the default is what was hardcoded before.
    environment: str = "production"


@dataclass(frozen=True, slots=True)
class PolicyTestResult:
    """What happened when one case ran."""

    case: PolicyTestCase
    actual: bool
    passed: bool
    statement: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyTestSummary:
    """Counts from a test run. `all_passed` is the activation gate's decision."""

    total: int
    passed: int
    failed: int
    errors: int
    all_passed: bool
    failures: tuple[PolicyTestResult, ...] = field(default_factory=tuple)


def run_policy_tests(
    cases: Sequence[PolicyTestCase],
    evaluate: Callable[[PolicyTestCase], PolicyVerdict],
) -> list[PolicyTestResult]:
    """Run every case. Never short-circuits — the failure report needs all of them.

    `evaluate` takes a `PolicyTestCase` and returns a `PolicyVerdict`. The caller
    is responsible for binding the engine and building the `RequestContext` from the
    case's fields — this function does not import any PEP or Cedar code.
    """
    results: list[PolicyTestResult] = []
    for case in cases:
        try:
            verdict = evaluate(case)
            results.append(
                PolicyTestResult(
                    case=case,
                    actual=verdict.allowed,
                    passed=verdict.allowed is case.expected,
                    statement=verdict.statement,
                )
            )
        except Exception as exc:
            results.append(
                PolicyTestResult(
                    case=case,
                    actual=not case.expected,  # opposite of expected = failure
                    passed=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def summarize(results: Sequence[PolicyTestResult]) -> PolicyTestSummary:
    """Count the results and decide whether the gate passes.

    An empty result list passes — with zero tests, none fail. That is the
    mathematically correct reading of "a bundle cannot be activated while any
    attached policy test fails". An operator deploying without a corpus should
    see the `total=0` in the summary and be warned, but not blocked.
    """
    passed = sum(1 for r in results if r.passed)
    errors = sum(1 for r in results if r.error is not None)
    failed = len(results) - passed
    failures = tuple(r for r in results if not r.passed)
    return PolicyTestSummary(
        total=len(results),
        passed=passed,
        failed=failed,
        errors=errors,
        all_passed=failed == 0,
        failures=failures,
    )
