from unittest.mock import AsyncMock

import pytest

from agentiam_controlplane.nl_compiler.compiler import CompilerOutput, compile_nl_to_policy
from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient


@pytest.mark.asyncio
async def test_compile_nl_to_policy_validates_and_parses() -> None:
    """Ensure the compiler successfully parses the model's JSON into Pydantic models."""
    mock_response = {
        "cedar_source": 'permit(principal == User::"admin", action, resource);',
        "tests": [
            {
                "name": "admin_can_do_anything",
                "description": "Admins are allowed all actions",
                "expected": True,
                "principal_id": "admin",
                "action": "system:reboot",
                "resource_type": "System",
                "resource_id": "host1",
            },
            {
                "name": "guest_cannot_reboot",
                "description": "Guests are denied",
                "expected": False,
                "principal_id": "guest",
                "action": "system:reboot",
                "resource_type": "System",
                "resource_id": "host1",
            },
        ],
    }

    mock_client = AsyncMock(spec=OllamaClient)
    mock_client.generate_structured.return_value = mock_response

    output = await compile_nl_to_policy("Admins can do anything.", client=mock_client)

    # Verify Pydantic parsing
    assert isinstance(output, CompilerOutput)
    assert 'permit(principal == User::"admin", action, resource);' in output.cedar_source
    assert len(output.tests) == 2

    t1 = output.tests[0]
    assert t1.name == "admin_can_do_anything"
    assert t1.expected is True

    t2 = output.tests[1]
    assert t2.name == "guest_cannot_reboot"
    assert t2.expected is False

    # Verify the client was called with the correct prompt and schema
    mock_client.generate_structured.assert_called_once()
    _, kwargs = mock_client.generate_structured.call_args
    assert "Admins can do anything." in kwargs["prompt"]
    assert "schema" in kwargs
    assert kwargs["schema"]["title"] == "CompilerOutput"


@pytest.mark.asyncio
async def test_compile_nl_to_policy_handles_ambiguity() -> None:
    """Ensure the compiler surfaces a clarifying question when the input is ambiguous."""
    mock_response = {
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
