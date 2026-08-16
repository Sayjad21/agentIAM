"""The pluggable LLM backend — ADR-040.

The tests that matter are the ones about *selection*. ADR-040 promises that moving the
compiler back on-premises is a configuration change, and a promise nothing checks is a
promise that quietly stops being true.

No test here needs a real API key: `GroqClient` takes one directly so the environment is
never a prerequisite for running the suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agentiam_controlplane.nl_compiler.llm import (
    DEFAULT_GROQ_MODEL,
    GroqClient,
    LLMError,
    client_from_env,
    load_dotenv,
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

    @pytest.fixture(autouse=True)
    def _no_dotenv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ignore the developer's real `.env` here.

        These tests are about the *selection* rules, and a `.env` on the machine running
        them would otherwise decide the answer — `test_no_key_falls_back_to_local` would
        pass or fail depending on whether the author happened to have a key configured.
        """
        monkeypatch.setattr(
            "agentiam_controlplane.nl_compiler.llm.load_dotenv", lambda *a, **k: None
        )

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


class TestRateLimitRetry:
    """Measured: 20 of 30 requests returned HTTP 429 on the first full evaluation run.

    Retrying is not politeness here, it is what makes the hosted backend usable for more
    than one call at a time. Every test injects `sleep`, so none of them actually waits.
    """

    @staticmethod
    def _client(**kw: Any) -> tuple[GroqClient, list[float]]:
        waited: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waited.append(seconds)

        return GroqClient(api_key="k", sleep=fake_sleep, **kw), waited

    @staticmethod
    def _status(code: int, headers: dict[str, str] | None = None) -> MagicMock:
        response = MagicMock()
        response.status_code = code
        response.headers = headers or {}
        return response

    @pytest.mark.asyncio
    async def test_a_429_is_retried_and_then_succeeds(self) -> None:
        client, waited = self._client()
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.side_effect = [self._status(429), a_response('{"ok": true}')]
            assert await client.generate_structured("p", SCHEMA) == {"ok": True}
        assert post.call_count == 2
        assert len(waited) == 1

    @pytest.mark.asyncio
    async def test_retry_after_is_honoured(self) -> None:
        client, waited = self._client()
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.side_effect = [
                self._status(429, {"retry-after": "7"}),
                a_response('{"ok": true}'),
            ]
            await client.generate_structured("p", SCHEMA)
        assert waited == [7.0]

    @pytest.mark.asyncio
    async def test_a_hostile_retry_after_is_clamped(self) -> None:
        # A provider asking for an hour must not stall the console for an hour.
        client, waited = self._client()
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.side_effect = [
                self._status(429, {"retry-after": "3600"}),
                a_response('{"ok": true}'),
            ]
            await client.generate_structured("p", SCHEMA)
        assert waited == [30.0]

    @pytest.mark.asyncio
    async def test_a_non_numeric_retry_after_falls_back_to_backoff(self) -> None:
        client, waited = self._client()
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.side_effect = [
                self._status(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                a_response('{"ok": true}'),
            ]
            await client.generate_structured("p", SCHEMA)
        assert waited == [1.0]

    @pytest.mark.asyncio
    async def test_backoff_doubles_without_a_header(self) -> None:
        client, waited = self._client(max_retries=3)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.side_effect = [
                self._status(429),
                self._status(429),
                self._status(429),
                a_response('{"ok": true}'),
            ]
            await client.generate_structured("p", SCHEMA)
        assert waited == [1.0, 2.0, 4.0]

    @pytest.mark.asyncio
    async def test_retries_are_bounded(self) -> None:
        # A permanently rate-limited key must surface an error, not spin forever.
        client, waited = self._client(max_retries=2)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.side_effect = [self._status(429)] * 3
            with pytest.raises(LLMError, match="after 2 retries"):
                await client.generate_structured("p", SCHEMA)
        assert post.call_count == 3
        assert len(waited) == 2

    @pytest.mark.asyncio
    async def test_transient_server_errors_are_retried_too(self) -> None:
        client, _ = self._client()
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
            post.side_effect = [self._status(503), a_response('{"ok": true}')]
            assert await client.generate_structured("p", SCHEMA) == {"ok": True}

    @pytest.mark.asyncio
    async def test_an_auth_failure_is_not_retried(self) -> None:
        # A bad key will still be bad in one second. Retrying only delays the diagnosis.
        client, waited = self._client()
        response = MagicMock(status_code=401, text="invalid key")
        failing = MagicMock()
        failing.status_code = 401
        failing.headers = {}
        failing.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("e", request=MagicMock(), response=response)
        )
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=failing):
            with pytest.raises(LLMError, match="401"):
                await client.generate_structured("p", SCHEMA)
        assert waited == []


class TestDotenvLoading:
    """`.env` is a developer convenience and must never outrank a real deployment."""

    def test_values_are_read_from_a_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text("GROQ_API_KEY=from-file\n", encoding="utf-8")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        load_dotenv(tmp_path)
        assert os.environ["GROQ_API_KEY"] == "from-file"

    def test_a_real_environment_variable_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stale `.env` lying around must not silently override what a deployment
        # actually exported. This is the rule worth pinning.
        (tmp_path / ".env").write_text("GROQ_API_KEY=from-file\n", encoding="utf-8")
        monkeypatch.setenv("GROQ_API_KEY", "from-environment")
        load_dotenv(tmp_path)
        assert os.environ["GROQ_API_KEY"] == "from-environment"

    def test_comments_blanks_and_junk_lines_are_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text(
            "# a comment\n\nAGENTIAM_TEST_VALUE=ok\nnot-a-pair\n", encoding="utf-8"
        )
        monkeypatch.delenv("AGENTIAM_TEST_VALUE", raising=False)
        load_dotenv(tmp_path)
        assert os.environ["AGENTIAM_TEST_VALUE"] == "ok"

    def test_quotes_are_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".env").write_text('AGENTIAM_TEST_VALUE="quoted"\n', encoding="utf-8")
        monkeypatch.delenv("AGENTIAM_TEST_VALUE", raising=False)
        load_dotenv(tmp_path)
        assert os.environ["AGENTIAM_TEST_VALUE"] == "quoted"

    def test_a_missing_dotenv_is_not_an_error(self, tmp_path: Path) -> None:
        load_dotenv(tmp_path / "nowhere")

    def test_the_search_walks_upward(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Running a script from `scripts/` must still find the repository-root `.env`.
        (tmp_path / ".env").write_text("AGENTIAM_TEST_VALUE=found\n", encoding="utf-8")
        nested = tmp_path / "packages" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.delenv("AGENTIAM_TEST_VALUE", raising=False)
        load_dotenv(nested)
        assert os.environ["AGENTIAM_TEST_VALUE"] == "found"


class TestErrorCompatibility:
    def test_an_ollama_error_is_an_llm_error(self) -> None:
        # So a caller — and T-031's template fallback — can catch one type regardless of
        # which backend is configured.
        assert issubclass(OllamaError, LLMError)
