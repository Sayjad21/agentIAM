"""The LLM backend behind the NL→Cedar compiler — ADR-040.

Two implementations of one narrow interface:

* `GroqClient` — hosted inference. The default while AgentIAM is a prototype, because a
  4-bit 7B on the development GPU took a **median 45.2 s** per policy (ADR-038) and a
  cloud CPU VM would be markedly worse. That is not a demo, it is a wait.
* `OllamaClient` — strictly local, in `ollama_client.py`. Unchanged, still selectable, and
  the documented destination once inference hardware is funded.

**Selection is configuration, not code.** `AGENTIAM_LLM_BACKEND=ollama` moves the whole
compiler back on-premises with no edit, which is the property that makes the migration
promise in ADR-040 credible rather than aspirational.

The interface is deliberately tiny — one structured-generation call and a warm-up — because
everything else about the two backends differs, and a wider seam would leak one into the
other.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_GROQ_MODEL",
    "GroqClient",
    "LLMClient",
    "LLMError",
    "client_from_env",
]

#: Groq's OpenAI-compatible endpoint.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

#: A 70B-class instruct model. Chosen over the smaller/faster options because the failure
#: mode being fixed was *quality* — the local 7B wrote `Resource::*` and put conditions in
#: the policy scope (STATUS gap 19).
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"

#: Hosted inference is fast; this is a real ceiling rather than the 300 s the local path
#: needs (ADR-038).
DEFAULT_TIMEOUT_S = 60.0

_BACKEND_ENV = "AGENTIAM_LLM_BACKEND"
_KEY_ENV = "GROQ_API_KEY"


class LLMError(Exception):
    """Generation failed — network, timeout, auth, or an unusable response.

    One error type across backends so callers, and T-031's template fallback, do not have
    to know which one is configured.
    """


@runtime_checkable
class LLMClient(Protocol):
    """What the compiler needs from a model, and nothing else."""

    async def generate_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return JSON conforming to `schema`.

        Raises:
            LLMError: The backend could not be reached, or returned nothing usable.
        """
        ...

    async def warm(self) -> bool:
        """Make the first real call fast. Never raises; reports whether it worked."""
        ...


class GroqClient:
    """Hosted inference over Groq's OpenAI-compatible API.

    The API key is read from `GROQ_API_KEY` and never accepted as a literal in code, so it
    cannot reach the repository. A missing key raises at construction rather than at the
    first request: failing when the console starts is far easier to diagnose than failing
    the first time an operator writes a policy on stage.
    """

    def __init__(
        self,
        model: str = DEFAULT_GROQ_MODEL,
        timeout: float = DEFAULT_TIMEOUT_S,
        api_key: str | None = None,
    ) -> None:
        """Read the key from the environment unless one is supplied (tests supply one)."""
        key = api_key if api_key is not None else os.environ.get(_KEY_ENV)
        if not key:
            raise LLMError(
                f"{_KEY_ENV} is not set. Export it, or set {_BACKEND_ENV}=ollama to run "
                f"the compiler on a local model instead."
            )
        self._api_key = key
        self._model = model
        self._timeout = timeout

    async def generate_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Generate JSON for `prompt`, constrained to `schema`.

        `temperature=0` and a fixed `seed` are sent, but hosted determinism is
        best-effort — see ADR-040. The compiler does not rely on it: what makes a
        generated policy safe to activate is the corpus gate, not reproducibility.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You reply with a single JSON object and nothing else. It must "
                        f"conform to this JSON schema:\n{json.dumps(schema)}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "seed": 42,
            "response_format": {"type": "json_object"},
        }

        try:
            async with httpx.AsyncClient(
                base_url=GROQ_BASE_URL, timeout=httpx.Timeout(self._timeout)
            ) as client:
                response = await client.post(
                    "/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise LLMError(f"Groq generation timed out after {self._timeout}s") from exc
        except httpx.HTTPStatusError as exc:
            # Deliberately not echoing the response body: it can repeat request content,
            # and this string reaches the console UI.
            raise LLMError(f"Groq returned HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise LLMError(f"Groq network error: {exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Groq returned an unexpected response shape") from exc

        if not content:
            raise LLMError("Groq returned an empty response")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Groq returned invalid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise LLMError("Groq returned JSON that is not an object")
        return parsed

    async def warm(self) -> bool:
        """A no-op that reports reachability. Hosted inference has no model to load."""
        try:
            await self.generate_structured("Reply with {}", {"type": "object", "properties": {}})
        except LLMError as exc:
            logger.warning("Groq did not respond to the warm-up: %s", exc)
            return False
        return True


def client_from_env() -> LLMClient:
    """The configured backend.

    `AGENTIAM_LLM_BACKEND` selects explicitly (`groq` or `ollama`). With nothing set, a
    present `GROQ_API_KEY` means Groq and its absence means Ollama — so a machine with no
    key still runs, on the local model, rather than failing at import.
    """
    from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient

    backend = os.environ.get(_BACKEND_ENV, "").strip().lower()

    if backend == "ollama":
        return OllamaClient()
    if backend == "groq":
        return GroqClient()
    if backend:
        raise LLMError(f"{_BACKEND_ENV} must be 'groq' or 'ollama', got {backend!r}")

    if os.environ.get(_KEY_ENV):
        return GroqClient()
    logger.info("No %s set; falling back to the local Ollama backend.", _KEY_ENV)
    return OllamaClient()
