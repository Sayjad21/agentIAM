"""Evaluation script for the NL to Cedar Compiler."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from cedarpy import Decision, is_authorized

from agentiam_controlplane.nl_compiler.compiler import compile_nl_to_policy
from agentiam_controlplane.nl_compiler.ollama_client import OllamaError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def evaluate_policy(cedar_source: str, expected_tests: list[dict[str, Any]]) -> bool:
    """Evaluate a generated Cedar policy against a set of expected tests.

    Returns True if all tests pass, False otherwise.
    """
    for test in expected_tests:
        request = {
            "principal": test["principal_id"],
            "action": test["action"],
            "resource": test["resource_type"],
            "context": {},
        }
        try:
            # We assume a blank entities list for simplicity in these tests
            result = is_authorized(request, cedar_source, [])
            expected_decision = Decision.Allow if test["expected"] else Decision.Deny

            if result.decision != expected_decision:
                logger.error(
                    "Test failed. Expected %s, got %s for request %s",
                    expected_decision,
                    result.decision,
                    request,
                )
                return False
        except Exception as exc:
            logger.error("Cedar evaluation failed with exception: %s", exc)
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

    passed = 0
    failed = 0
    errors = 0

    for i, case in enumerate(dataset):
        nl = case["nl"]
        logger.info("Evaluating case %d/30: %s", i + 1, nl)

        try:
            output = await compile_nl_to_policy(nl)

            if output.cedar_source is None:
                logger.warning("Case %d FAILED (ambiguous or no source)", i + 1)
                failed += 1
            elif evaluate_policy(output.cedar_source, case["expected_tests"]):
                logger.info("Case %d PASSED", i + 1)
                passed += 1
            else:
                logger.warning("Case %d FAILED evaluation", i + 1)
                failed += 1

        except OllamaError as exc:
            logger.error("Ollama error on case %d: %s", i + 1, exc)
            errors += 1
        except Exception as exc:
            logger.error("Unexpected error on case %d: %s", i + 1, exc)
            errors += 1

    logger.info("--- Evaluation Complete ---")
    logger.info("Total passed: %d/30", passed)
    logger.info("Total failed: %d/30", failed)
    logger.info("Total errors: %d/30", errors)


if __name__ == "__main__":
    asyncio.run(main())
