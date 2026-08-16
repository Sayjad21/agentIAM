from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentiam_controlplane.nl_compiler.compiler import CompilerOutput, compile_nl_to_policy
from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient


@pytest.mark.asyncio
async def test_compile_nl_to_policy_validates_and_parses() -> None:
    """Ensure the compiler successfully parses the model's JSON into Pydantic models."""
    mock_response = {
        "cedar_source": (
            'permit(principal, action == Action::"invoice:write", resource) '
            'when { principal.role == "senior" };'
        ),
        "tests": [
            {
                "name": "senior_can_write",
                "description": "Seniors are allowed to write invoices",
                "expected": True,
                "role": "senior",
                "operation": "invoice:write",
                "tool": "invoice_api",
            },
            {
                "name": "worker_cannot_write",
                "description": "Workers are denied",
                "expected": False,
                "role": "worker",
                "operation": "invoice:write",
                "tool": "invoice_api",
            },
        ],
    }

    mock_client = AsyncMock(spec=OllamaClient)
    mock_client.generate_structured.return_value = mock_response

    output = await compile_nl_to_policy("Admins can do anything.", client=mock_client)

    # Verify Pydantic parsing
    assert isinstance(output, CompilerOutput)
    assert output.cedar_source is not None
    assert 'principal.role == "senior"' in output.cedar_source
    assert len(output.tests) == 2

    t1 = output.tests[0]
    assert t1.name == "senior_can_write"
    assert t1.expected is True

    t2 = output.tests[1]
    assert t2.name == "worker_cannot_write"
    assert t2.expected is False

    # The generated test must convert to the core PolicyTestCase, because that is what
    # the shared evaluator takes. If this drifts, the console's dual gate silently
    # stops testing what the PEP would enforce.
    core_case = t1.as_policy_test_case()
    assert core_case.operation == "invoice:write"
    assert core_case.role == "senior"
    assert core_case.amount == Decimal(0)

    # Verify the client was called with the correct prompt and schema
    mock_client.generate_structured.assert_called_once()
    _, kwargs = mock_client.generate_structured.call_args
    assert "Admins can do anything." in kwargs["prompt"]
    assert "schema" in kwargs
    assert kwargs["schema"]["title"] == "CompilerOutput"


@pytest.mark.asyncio
async def test_compile_nl_to_policy_handles_ambiguity() -> None:
    """Ensure the compiler surfaces a clarifying question when the input is ambiguous."""
    mock_response: dict[str, Any] = {
        "clarifying_question": "Who is 'someone' and what resource are they acting on?",
        "cedar_source": None,
        "tests": [],
    }

    mock_client = AsyncMock(spec=OllamaClient)
    mock_client.generate_structured.return_value = mock_response

    output = await compile_nl_to_policy("Someone can do something.", client=mock_client)

    assert isinstance(output, CompilerOutput)
    assert output.clarifying_question == "Who is 'someone' and what resource are they acting on?"
    assert output.cedar_source is None
    assert len(output.tests) == 0
