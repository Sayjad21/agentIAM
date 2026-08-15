from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient, OllamaError


@pytest.fixture
def client() -> OllamaClient:
    return OllamaClient(timeout=1.0)


@pytest.mark.asyncio
async def test_ollama_client_only_connects_to_localhost(client: OllamaClient) -> None:
    """Ensure the client strictly enforces the base URL to prevent external API calls."""
    assert client._base_url == "http://127.0.0.1:11434", "Must only connect to local Ollama"

    with patch("httpx.AsyncClient.post") as mock_post:
        # Simulate a timeout just to stop the actual request; we only care about what was sent
        mock_post.side_effect = httpx.TimeoutException("mocked timeout")

        with pytest.raises(OllamaError):
            await client.generate_structured("test", schema={"type": "object"})

        # Verify the mocked call hit the right path
        mock_post.assert_called_once()
        args, _ = mock_post.call_args
        assert args[0] == "/api/generate"


@pytest.mark.asyncio
async def test_determinism_and_schema_constraints_are_sent(client: OllamaClient) -> None:
    """Verify temperature=0, seed=42, and the correct schema are passed to Ollama."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:

        class MockResponse:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {"response": '{"status":"ok"}'}

        mock_post.return_value = MockResponse()

        schema = {"type": "object", "properties": {"status": {"type": "string"}}}
        result = await client.generate_structured("prompt", schema=schema)

        assert result == {"status": "ok"}

        # Assert payload constraints
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        assert payload["model"] == "qwen2.5:7b-instruct-q4_0"
        assert payload["prompt"] == "prompt"
        assert payload["format"] == schema
        assert payload["options"]["temperature"] == 0.0
        assert payload["options"]["seed"] == 42
        assert payload["stream"] is False


@pytest.mark.asyncio
async def test_timeout_raises_ollama_error(client: OllamaClient) -> None:
    """Timeout exceptions from httpx must be caught and raised as OllamaError."""
    with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timeout")):
        with pytest.raises(OllamaError, match="timed out"):
            await client.generate_structured("prompt", schema={})


@pytest.mark.asyncio
async def test_connection_error_raises_ollama_error(client: OllamaClient) -> None:
    """Network errors from httpx must be caught and raised as OllamaError."""
    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("connection refused")):
        with pytest.raises(OllamaError, match="network error"):
            await client.generate_structured("prompt", schema={})


@pytest.mark.asyncio
async def test_bad_json_response_raises_ollama_error(client: OllamaClient) -> None:
    """If Ollama returns invalid JSON, it should raise an OllamaError."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:

        class MockResponse:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {"response": "this is not json"}

        mock_post.return_value = MockResponse()

        with pytest.raises(OllamaError, match="invalid JSON"):
            await client.generate_structured("prompt", schema={})


@pytest.mark.asyncio
async def test_http_status_error_raises_ollama_error(client: OllamaClient) -> None:
    """Non-200 HTTP responses must be caught and raised as OllamaError."""

    class MockResponse:
        status_code = 500
        text = "Internal Server Error"

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "error",
                request=MagicMock(),
                response=self,  # type: ignore
            )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=MockResponse()):
        with pytest.raises(OllamaError, match="HTTP 500"):
            await client.generate_structured("prompt", schema={})
