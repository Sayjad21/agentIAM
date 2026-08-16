"""Evaluation harness for the NL to Cedar compiler — dataset v2.

Two modes, and the first is the point:

    python scripts/evaluate_compiler.py --validate    # no model; checks the instrument
    python scripts/evaluate_compiler.py               # the real run

`--validate` evaluates each case's `reference_cedar` against its own tests. If that does
not score 30/30, the dataset is broken and a generation run would be measuring the dataset
rather than the compiler. **That is exactly what happened to v1**: it passed bare names
(`"admin"`) where Cedar wants entity uids, so every request returned `NoDecision` and the
harness reported 0/30 no matter what the compiler produced. Worse, v1's expectations turned
on ids the English never contained — at most 10 of 30 were winnable by any compiler, and
only by naming an individual, which is the wrong generalisation.

v2 evaluates against AgentIAM's own entity model through `agentiam_controlplane.app.
evaluate_case` — the same function the Admin Console and the activation gate use. So a case
is won by generalising (`principal.role == "senior"`) rather than by guessing an id, and a
policy that scores well here is directly activatable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import cedarpy

from agentiam_controlplane.app import evaluate_case
from agentiam_controlplane.nl_compiler.compiler import compile_nl_to_policy
from agentiam_controlplane.nl_compiler.llm import LLMError, client_from_env
from agentiam_core.policy_testing import PolicyTestCase, run_policy_tests, summarize

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DATASET = (
    Path(__file__).resolve().parent.parent
    / "packages"
    / "agentiam-controlplane"
    / "src"
    / "agentiam_controlplane"
    / "nl_compiler"
    / "dataset.json"
)


def as_case(raw: dict[str, Any]) -> PolicyTestCase:
    """One dataset row as the same `PolicyTestCase` the 51-case corpus uses."""
    return PolicyTestCase(
        name=raw["name"],
        description=raw["description"],
        operation=raw["operation"],
        expected=raw["expected"],
        role=raw.get("role", "worker"),
        tool=raw.get("tool", "invoice_api"),
        amount=Decimal(raw.get("amount", "0")),
        depth=raw.get("depth", 1),
        elevated=raw.get("elevated", False),
        environment=raw.get("environment", "production"),
    )


def score(cedar_source: str, rows: list[dict[str, Any]]) -> tuple[bool, str]:
    """Evaluate a policy against one case's tests.

    Returns:
        `(passed, detail)`. `detail` names the first failing test, for the log.
    """
    try:
        engine = cedarpy.PolicySet.from_str(cedar_source)
    except Exception as exc:
        return False, f"does not parse: {exc}"

    cases = [as_case(r) for r in rows]
    summary = summarize(run_policy_tests(cases, lambda c: evaluate_case(engine, c)))
    if summary.all_passed:
        return True, f"{summary.passed}/{summary.total}"
    first = summary.failures[0]
    return False, (
        f"{summary.passed}/{summary.total} — {first.case.name} expected "
        f"{first.case.expected}, got {first.actual}"
    )


def validate(cases: list[dict[str, Any]]) -> int:
    """Check the instrument itself: does each reference policy pass its own tests?"""
    passed = 0
    for i, case in enumerate(cases, start=1):
        ok, detail = score(case["reference_cedar"], case["tests"])
        if ok:
            passed += 1
            logger.info("[%2d/%d] PASS  %s", i, len(cases), case["nl"])
        else:
            logger.error("[%2d/%d] FAIL  %s\n         %s", i, len(cases), case["nl"], detail)
            logger.error("         %s", case["reference_cedar"].replace("\n", "\n         "))

    logger.info("\nreference policies: %d/%d", passed, len(cases))
    if passed != len(cases):
        logger.error("The dataset is not self-consistent. Fix it before measuring anything.")
    return passed


async def evaluate(cases: list[dict[str, Any]]) -> None:
    """The real run: compile each prompt and score what comes back."""
    total = len(cases)
    passed = wrong = unparseable = ambiguous = errors = 0
    durations: list[float] = []

    # Warm first. On the local backend a cold model costs 216 s against ~45 s warm
    # (ADR-038), and folding that into case 1 would misreport the compiler as five times
    # slower than it is. On Groq this is a cheap reachability check.
    client = client_from_env()
    logger.info("backend: %s", type(client).__name__)
    await client.warm()

    for i, case in enumerate(cases, start=1):
        nl = case["nl"]
        logger.info("[%2d/%d] %s", i, total, nl)

        started = time.perf_counter()
        try:
            output = await compile_nl_to_policy(nl)
            durations.append(time.perf_counter() - started)

            if output.cedar_source is None:
                # Not a crash — the compiler is meant to ask rather than guess. But these
                # prompts are unambiguous, so asking is still a miss.
                logger.info("        ambiguous: %s", output.clarifying_question)
                ambiguous += 1
                continue

            ok, detail = score(output.cedar_source, case["tests"])
            if ok:
                passed += 1
                logger.info("        PASS %s", detail)
            elif detail.startswith("does not parse"):
                unparseable += 1
                logger.info("        %s", detail)
                logger.info("        %s", output.cedar_source.replace("\n", " "))
            else:
                wrong += 1
                logger.info("        FAIL %s", detail)
                logger.info("        %s", output.cedar_source.replace("\n", " "))

        except LLMError as exc:
            durations.append(time.perf_counter() - started)
            errors += 1
            logger.error("        backend error: %s", exc)
        except Exception as exc:
            durations.append(time.perf_counter() - started)
            errors += 1
            logger.error("        unexpected error: %s", exc)

    logger.info("\n--- Evaluation complete ---")
    logger.info("dataset            : %d cases", total)
    logger.info("passed             : %d (%.0f%%)", passed, 100 * passed / total)
    logger.info("wrong decision     : %d", wrong)
    logger.info("unparseable Cedar  : %d", unparseable)
    logger.info("asked for clarity  : %d", ambiguous)
    logger.info("errors / timeouts  : %d", errors)
    if durations:
        ordered = sorted(durations)
        logger.info(
            "latency            : median %.1f s  min %.1f s  max %.1f s",
            statistics.median(ordered),
            ordered[0],
            ordered[-1],
        )


def main() -> None:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate",
        action="store_true",
        help="check the reference policies only; requires no model",
    )
    args = parser.parse_args()

    cases = json.loads(DATASET.read_text(encoding="utf-8"))["cases"]
    if args.validate:
        raise SystemExit(0 if validate(cases) == len(cases) else 1)
    asyncio.run(evaluate(cases))


if __name__ == "__main__":
    main()
