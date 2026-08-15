"""Ollama client for constrained generation."""

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Raised when Ollama generation fails (network, timeout, or bad response)."""

    pass


class OllamaClient:
    """A strictly local, deterministic client for Ollama."""

    def __init__(self, timeout: float = 30.0) -> None:
        """Initialize the client.

        The base URL is hardcoded to 127.0.0.1 to guarantee that no external API
        is ever contacted (T-028 requirement).
        """
        self._base_url = "http://127.0.0.1:11434"
        self._timeout = timeout
        self._model = "qwen2.5:7b-instruct-q4_0"

    async def generate_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Generate constrained JSON output using Ollama.

        Enforces determinism by setting temperature=0 and a fixed seed.
        """
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": {
                "temperature": 0.0,
                "seed": 42,
            },
        }

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=httpx.Timeout(self._timeout)
            ) as client:
                response = await client.post("/api/generate", json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise OllamaError(f"Ollama generation timed out after {self._timeout}s") from exc
        except httpx.RequestError as exc:
            raise OllamaError(f"Ollama network error: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaError(
                f"Ollama returned HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except Exception as exc:
            raise OllamaError(f"Unexpected error communicating with Ollama: {exc}") from exc

        response_text = data.get("response")
        if not response_text:
            raise OllamaError("Ollama returned an empty response")

        try:
            return json.loads(response_text)  # type: ignore[no-any-return]
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Ollama returned invalid JSON: {exc}") from exc
