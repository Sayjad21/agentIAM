"""Evaluation script for the NL to Cedar Compiler."""

import asyncio
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any

import cedarpy
from cedarpy import Decision, is_authorized

from agentiam_controlplane.nl_compiler.compiler import compile_nl_to_policy
from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient, OllamaError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def as_request(test: dict[str, Any]) -> dict[str, Any]:
    """Build a Cedar request from a dataset row.

    **This is the bug that made the whole harness meaningless.** The dataset stores bare
    names — `"admin"`, `"system:reboot"`, `"System"` — but Cedar requires *entity uids*,
    `User::"admin"`. Passing the bare names produced `Decision.NoDecision` for every
    request, and `NoDecision` reads as "not allowed": every `expected: true` row failed
    and every `expected: false` row passed for the wrong reason.

    Measured before the fix: **0/30, even against a hand-written policy that plainly
    satisfies the cases.** The script could not report anything but failure, whatever the
    compiler produced. `app.py` had it right all along and this script did not.
    """
    return {
        "principal": f'User::"{test["principal_id"]}"',
        "action": f'Action::"{test["action"]}"',
        "resource": f'{test["resource_type"]}::"{test.get("resource_id", "r1")}"',
        "context": {},
    }


def evaluate_policy(cedar_source: str, expected_tests: list[dict[str, Any]]) -> bool:
    """Evaluate a generated Cedar policy against a set of expected tests.

    Returns True if every test produced the expected decision, False otherwise.
    """
    for test in expected_tests:
        request = as_request(test)
        try:
            # A blank entity list: these cases turn on the policy scope, not on
            # attributes or hierarchy.
            result = is_authorized(request, cedar_source, [])
            expected_decision = Decision.Allow if test["expected"] else Decision.Deny

            if result.decision != expected_decision:
                logger.info(
                    "  case failed: expected %s, got %s for %s",
                    expected_decision,
                    result.decision,
                    request,
                )
                return False
        except Exception as exc:
            logger.info("  cedar evaluation raised: %s", exc)
            return False

    return True


async def main() -> None:
    """Execute the evaluation suite against the dataset."""
    dataset_path = (
        Path(__file__).parent.parent
        / "packages"
        / "agentiam-controlplane"
        / "src"
        / "agentiam_controlplane"
        / "nl_compiler"
        / "dataset.json"
    )

    # Allow running from either the repo root or scripts dir
    if not dataset_path.exists():
        dataset_path = Path(
            "packages/agentiam-controlplane/src/agentiam_controlplane/nl_compiler/dataset.json"
        )

    with open(dataset_path) as f:
        dataset = json.load(f)

    total = len(dataset)
    passed = 0
    unparseable = 0
    ambiguous = 0
    wrong = 0
    errors = 0
    durations: list[float] = []

    # Warming first: a cold model costs 216 s against 45 s warm, and folding that into
    # case 1's timing would misreport the compiler as five times slower than it is.
    await OllamaClient().warm()

    for i, case in enumerate(dataset, start=1):
        nl = case["nl"]
        logger.info("[%d/%d] %s", i, total, nl)

        started = time.perf_counter()
        try:
            output = await compile_nl_to_policy(nl)
            durations.append(time.perf_counter() - started)

            if output.cedar_source is None:
                # Not an error: the compiler is *supposed* to ask rather than guess.
                # But these prompts are unambiguous, so asking is still a miss.
                logger.info("  -> ambiguous: %s", output.clarifying_question)
                ambiguous += 1
                continue

            try:
                cedarpy.PolicySet.from_str(output.cedar_source)
            except Exception as exc:
                logger.info("  -> generated Cedar does not parse: %s", exc)
                logger.info("     %s", output.cedar_source)
                unparseable += 1
                continue

            if evaluate_policy(output.cedar_source, case["expected_tests"]):
                logger.info("  -> PASS")
                passed += 1
            else:
                wrong += 1

        except OllamaError as exc:
            durations.append(time.perf_counter() - started)
            logger.error("  -> Ollama error: %s", exc)
            errors += 1
        except Exception as exc:
            durations.append(time.perf_counter() - started)
            logger.error("  -> unexpected error: %s", exc)
            errors += 1

    logger.info("--- Evaluation complete ---")
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


if __name__ == "__main__":
    asyncio.run(main())
