"""The pluggable LLM backend — ADR-040.

The tests that matter are the ones about *selection*. ADR-040 promises that moving the
compiler back on-premises is a configuration change, and a promise nothing checks is a
promise that quietly stops being true.

No test here needs a real API key: `GroqClient` takes one directly so the environment is
never a prerequisite for running the suite.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agentiam_controlplane.nl_compiler.llm import (
    DEFAULT_GROQ_MODEL,
    GroqClient,
    LLMError,
    client_from_env,
)
from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient, OllamaError

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def a_response(content: str) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"choices": [{"message": {"content": content}}]})
    return response


class TestBackendSelection:
    """ADR-040's migration promise, made checkable."""

    def test_ollama_is_selected_explicitly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTIAM_LLM_BACKEND", "ollama")
        monkeypatch.setenv("GROQ_API_KEY", "irrelevant-when-explicit")
        assert isinstance(client_from_env(), OllamaClient)

    def test_groq_is_selected_explicitly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTIAM_LLM_BACKEND", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        assert isinstance(client_from_env(), GroqClient)

    def test_a_key_alone_selects_groq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTIAM_LLM_BACKEND", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        assert isinstance(client_from_env(), GroqClient)

    def test_no_key_falls_back_to_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A machine with no key must still run, on the local model, rather than failing
        # at import — which is what makes "production moves to local" a config flip.
        monkeypatch.delenv("AGENTIAM_LLM_BACKEND", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert isinstance(client_from_env(), OllamaClient)

    def test_an_unknown_backend_is_refused_rather_than_guessed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTIAM_LLM_BACKEND", "openai")
        with pytest.raises(LLMError, match="must be"):
            client_from_env()

    def test_selecting_groq_without_a_key_says_how_to_fix_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTIAM_LLM_BACKEND", "groq")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(LLMError, match="GROQ_API_KEY"):
            client_from_env()


class TestGroqClient:
    def test_a_missing_key_fails_at_construction_not_at_first_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Failing when the console starts is far easier to diagnose than failing the
        # first time an operator writes a policy on stage.
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(LLMError):
            GroqClient()

    @pytest.mark.asyncio
    async def test_the_key_is_sent_as_a_bearer_token(self) -> None:
        client = GroqClient(api_key="secret-key")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response('{"ok": true}')
            await client.generate_structured("prompt", SCHEMA)
            _, kwargs = post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer secret-key"

    @pytest.mark.asyncio
    async def test_determinism_parameters_are_sent(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response('{"ok": true}')
            await client.generate_structured("prompt", SCHEMA)
            _, kwargs = post.call_args
            assert kwargs["json"]["temperature"] == 0
            assert kwargs["json"]["seed"] == 42
            assert kwargs["json"]["response_format"] == {"type": "json_object"}
            assert kwargs["json"]["model"] == DEFAULT_GROQ_MODEL

    @pytest.mark.asyncio
    async def test_the_schema_reaches_the_model(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response('{"ok": true}')
            await client.generate_structured("compile this", SCHEMA)
            _, kwargs = post.call_args
            system, user = kwargs["json"]["messages"]
            assert json.dumps(SCHEMA) in system["content"]
            assert user["content"] == "compile this"

    @pytest.mark.asyncio
    async def test_the_parsed_object_is_returned(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response('{"ok": true}')
            assert await client.generate_structured("p", SCHEMA) == {"ok": True}

    @pytest.mark.asyncio
    async def test_a_timeout_is_an_llm_error(self) -> None:
        client = GroqClient(api_key="k", timeout=0.1)
        with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("t")):
            with pytest.raises(LLMError, match="timed out"):
                await client.generate_structured("p", SCHEMA)

    @pytest.mark.asyncio
    async def test_a_network_error_is_an_llm_error(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("refused")):
            with pytest.raises(LLMError, match="network error"):
                await client.generate_structured("p", SCHEMA)

    @pytest.mark.asyncio
    async def test_an_http_error_does_not_echo_the_response_body(self) -> None:
        # The message reaches the console UI, and a Groq error body can repeat the
        # request content back — which is the operator's policy text.
        client = GroqClient(api_key="k")
        response = MagicMock(status_code=401, text="invalid api key for org acme-corp")
        failing = MagicMock()
        failing.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("e", request=MagicMock(), response=response)
        )
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=failing):
            with pytest.raises(LLMError) as exc:
                await client.generate_structured("p", SCHEMA)
        assert "401" in str(exc.value)
        assert "acme-corp" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_invalid_json_is_an_llm_error(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response("this is not json")
            with pytest.raises(LLMError, match="invalid JSON"):
                await client.generate_structured("p", SCHEMA)

    @pytest.mark.asyncio
    async def test_a_json_array_is_refused(self) -> None:
        # The compiler validates a dict; an array would fail later and less clearly.
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response("[1, 2, 3]")
            with pytest.raises(LLMError, match="not an object"):
                await client.generate_structured("p", SCHEMA)

    @pytest.mark.asyncio
    async def test_an_unexpected_response_shape_is_an_llm_error(self) -> None:
        client = GroqClient(api_key="k")
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"unexpected": True})
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=response):
            with pytest.raises(LLMError, match="unexpected response shape"):
                await client.generate_structured("p", SCHEMA)

    @pytest.mark.asyncio
    async def test_an_empty_completion_is_an_llm_error(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response("")
            with pytest.raises(LLMError, match="empty"):
                await client.generate_structured("p", SCHEMA)

    @pytest.mark.asyncio
    async def test_warm_reports_reachability_without_raising(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("down")):
            assert await client.warm() is False

    @pytest.mark.asyncio
    async def test_warm_succeeds_when_the_api_answers(self) -> None:
        client = GroqClient(api_key="k")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.return_value = a_response("{}")
            assert await client.warm() is True


class TestErrorCompatibility:
    def test_an_ollama_error_is_an_llm_error(self) -> None:
        # So a caller — and T-031's template fallback — can catch one type regardless of
        # which backend is configured.
        assert issubclass(OllamaError, LLMError)
