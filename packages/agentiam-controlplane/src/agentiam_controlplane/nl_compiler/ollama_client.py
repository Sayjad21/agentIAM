"""Ollama client for constrained generation."""

import json
import logging
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Raised when Ollama generation fails (network, timeout, or bad response)."""

    pass


#: Measured against `qwen2.5:7b-instruct-q4_0`, resident in 5.32 GB of VRAM on the
#: development machine (ADR-038):
#:
#:   cold generation (model load + inference)   216.3 s
#:   warm generation, n=5    median 45.2 s, min 24.5 s, max 233.7 s
#:
#: The original 30 s budget was below the *warm median*, so the client reported its own
#: timeout as an Ollama failure on most calls and on every first call. This is the slow
#: operator-initiated authoring path, not a request path — NFR-1 does not apply here, and
#: a budget that actually covers the work is worth more than a tidy-looking number.
DEFAULT_TIMEOUT_S: Final = 300.0

#: How long Ollama should hold the model in memory. 216.3 s cold against 45.2 s warm is
#: the whole difference between a usable authoring flow and an unusable one.
DEFAULT_KEEP_ALIVE: Final = "30m"


class OllamaClient:
    """A strictly local, deterministic client for Ollama."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT_S,
        keep_alive: str = DEFAULT_KEEP_ALIVE,
    ) -> None:
        """Initialize the client.

        The base URL is hardcoded to 127.0.0.1 to guarantee that no external API
        is ever contacted (T-028 requirement).
        """
        self._base_url = "http://127.0.0.1:11434"
        self._timeout = timeout
        self.keep_alive = keep_alive
        self._model = "qwen2.5:7b-instruct-q4_0"

    async def warm(self) -> bool:
        """Load the model and ask it to stay resident. Never raises.

        Returns:
            Whether the model answered. `False` means the first real compile will pay the
            216 s cold path; it is not a startup failure, because the console has plenty
            to do that does not involve the compiler.
        """
        try:
            await self.generate_structured("warm-up", schema={"type": "object"})
        except OllamaError as exc:
            logger.warning("Compiler model did not warm: %s", exc)
            return False
        logger.info("Compiler model %s warmed, keep_alive=%s", self._model, self.keep_alive)
        return True

    async def generate_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Generate constrained JSON output using Ollama.

        Enforces determinism by setting temperature=0 and a fixed seed.
        """
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "keep_alive": self.keep_alive,
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
