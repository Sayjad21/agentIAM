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

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_GEMINI_MODEL",
    "DEFAULT_GROQ_MODEL",
    "GeminiClient",
    "GroqClient",
    "LLMClient",
    "LLMError",
    "client_from_env",
    "load_dotenv",
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

#: Free-tier rate limits are per-minute, and a 30-case evaluation walks straight into them
#: — measured: 20 of 30 cases returned HTTP 429 on the first full run. Retrying is not
#: optional politeness, it is what makes the backend usable at all.
DEFAULT_MAX_RETRIES = 5

#: Cap on a single backoff wait, so a hostile `Retry-After` cannot stall the console.
MAX_BACKOFF_S = 30.0

#: Statuses worth retrying: rate limiting, and transient upstream failures.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

#: Google's Gemini API. Chosen as the second hosted option because the limit that actually
#: bites on Groq's free tier — 100,000 tokens per *day*, about 55 compiles — has no
#: equivalent here: the free quota is metered in requests per day (1,500) with a
#: 1,000,000 token-per-minute ceiling, so a 30-case evaluation costs 30 requests rather
#: than half a day's budget.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Fast, cheap, and strong enough for schema-constrained Cedar.
#:
#: **Pinned deliberately, not `gemini-flash-latest`.** An alias that moves under you
#: changes the measurement basis between two evaluation runs without anything in the
#: repository changing, which is how a benchmark quietly stops meaning anything. Retiring
#: a pinned model fails loudly with a 404 instead — as `gemini-2.0-flash` did here, which
#: is what prompted the note.
#: An alias, reluctantly, and the reluctance is the point.
#:
#: Pinning is the right instinct — a moving target changes the measurement basis between
#: two runs with nothing in the repository changing. But measured on this account:
#: `gemini-2.0-flash` is retired (404), `gemini-2.5-flash-lite` is "no longer available to
#: new users" (404), and plain `gemini-2.5-flash` reports
#: `generate_content_free_tier_requests, limit: 20` — too small to finish a 30-case
#: evaluation on one key. `gemini-flash-lite-latest` is what actually answers.
#:
#: So the alias is used for availability and the **resolved** version is logged on every
#: call (`modelVersion` in the response), which keeps a run attributable after the fact.
#: On a paid tier, pin plain `gemini-2.5-flash` here instead.
DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"

_BACKEND_ENV = "AGENTIAM_LLM_BACKEND"
_KEY_ENV = "GROQ_API_KEY"
_GEMINI_KEY_ENV = "GEMINI_API_KEY"


def _limit_detail(body: str) -> str:
    """The provider's own explanation of a rate limit, trimmed and de-secreted.

    Worth extracting rather than discarding: Groq's 429 body was the only place the
    binding limit appeared at all — a **daily** token cap that no response header
    mentioned — and two pacing values were guessed wrong before anyone read it. Logging
    this turns "429 again" into a number you can pace against.

    Truncated because provider errors can be long, and scrubbed of anything key-shaped
    because this reaches logs.

    **Truncation starts from the useful part, not the beginning.** Gemini leads with two
    documentation URLs and puts `Quota exceeded for <metric>, limit <n>` after them — a
    naive first-300-characters cut threw away the only sentence worth logging, which is
    exactly what happened on the first run that used this.
    """
    scrubbed = re.sub(r"(gsk_|AIzaSy)[A-Za-z0-9_-]+", "<redacted>", body)
    flat = " ".join(scrubbed.split())

    # Skip the preamble when the provider buries the metric behind boilerplate.
    marker = flat.find("Quota exceeded")
    if marker > 0:
        flat = flat[marker:]
    return flat[:400]


#: Gemini reports its reset in the response *body* rather than a `Retry-After` header:
#: `"Please retry in 28.660239155s."`. Measured consequence of missing it: the client fell
#: back to a 1 s backoff, retried long before the window reopened, and **each premature
#: retry spent another request against the very quota it was waiting on** — the retry loop
#: amplified the throttle it existed to survive.
_RETRY_IN_RE = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)


def _retry_delay(retry_after: str | None, attempt: int, body: str = "") -> float:
    """How long to wait before retry `attempt`, in seconds.

    Precedence, most authoritative first: the `Retry-After` header, then a "retry in Xs"
    hint in the body, then exponential backoff. The provider knows when its own window
    reopens; guessing earlier is not merely wasteful, it is counterproductive when the
    guess itself consumes quota.

    Every source is clamped, so neither a hostile header nor a malformed body can stall
    the console.
    """
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF_S)
        except ValueError:
            # Retry-After may also be an HTTP date. Not worth parsing: fall through.
            pass

    match = _RETRY_IN_RE.search(body)
    if match:
        # A second of slack: retrying at the exact boundary tends to land just inside it.
        return min(float(match.group(1)) + 1.0, MAX_BACKOFF_S)

    return min(2.0**attempt, MAX_BACKOFF_S)


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
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Read the key from the environment unless one is supplied (tests supply one).

        `sleep` is injected so the retry tests do not spend real seconds waiting.
        """
        key = api_key if api_key is not None else os.environ.get(_KEY_ENV)
        if not key:
            raise LLMError(
                f"{_KEY_ENV} is not set. Export it, or set {_BACKEND_ENV}=ollama to run "
                f"the compiler on a local model instead."
            )
        self._api_key = key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep or asyncio.sleep

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

        data = await self._post_with_retry(payload)

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

    async def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the completion, retrying rate limits and transient upstream failures.

        Measured: a 30-case evaluation run hit HTTP 429 on 20 of 30 requests against the
        free tier. Without this the backend is unusable for anything but single calls.

        `Retry-After` is honoured when the provider sends it, since it knows when the
        window resets better than any backoff curve — but it is clamped, so a bad header
        cannot stall the console for minutes. Otherwise the wait doubles from one second.
        """
        last: str = "no attempt was made"

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=GROQ_BASE_URL, timeout=httpx.Timeout(self._timeout)
                ) as client:
                    response = await client.post(
                        "/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                    if response.status_code in _RETRYABLE:
                        last = f"Groq returned HTTP {response.status_code}"
                        if attempt == self._max_retries:
                            break
                        delay = _retry_delay(
                            response.headers.get("retry-after"), attempt, response.text
                        )
                        logger.warning(
                            "%s; retrying in %.1fs — %s",
                            last,
                            delay,
                            _limit_detail(response.text),
                        )
                        await self._sleep(delay)
                        continue

                    response.raise_for_status()
                    parsed: dict[str, Any] = response.json()
                    return parsed

            except httpx.TimeoutException as exc:
                raise LLMError(f"Groq generation timed out after {self._timeout}s") from exc
            except httpx.HTTPStatusError as exc:
                # Deliberately not echoing the response body: it can repeat request
                # content, and this string reaches the console UI.
                raise LLMError(f"Groq returned HTTP {exc.response.status_code}") from exc
            except httpx.RequestError as exc:
                raise LLMError(f"Groq network error: {exc}") from exc

        raise LLMError(f"{last} after {self._max_retries} retries")

    async def warm(self) -> bool:
        """A no-op that reports reachability. Hosted inference has no model to load."""
        try:
            await self.generate_structured("Reply with {}", {"type": "object", "properties": {}})
        except LLMError as exc:
            logger.warning("Groq did not respond to the warm-up: %s", exc)
            return False
        return True


def load_dotenv(start: Path | None = None) -> None:
    """Populate the environment from the nearest `.env`, without overwriting it.

    Deliberately hand-rolled rather than a dependency: it reads `KEY=value` lines and
    ignores blanks and `#` comments, which is the whole of what this project's `.env`
    needs.

    **A real environment variable always wins.** `.env` is a developer convenience;
    whatever a deployment actually exports must not be silently overridden by a file that
    happens to be lying around.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return


class GeminiClient:
    """Hosted inference over Google's Gemini API.

    Exists because of a measured limit rather than a preference: Groq's free tier caps at
    100,000 tokens per **day**, which is roughly 55 compiles, and a single 30-case
    evaluation run costs more than half of that (ADR-040). Gemini's free quota is metered
    per request instead, so evaluation stops being rationed.

    **Read the privacy trade before pointing production at this.** Google's *unpaid* tier
    grants them the right to use submitted content to improve their products, and what is
    submitted here is the operator's policy text. Acceptable for a prototype, on the same
    reasoning as ADR-040; not acceptable for a customer deployment. The paid tier drops
    that clause, and `AGENTIAM_LLM_BACKEND=ollama` avoids it entirely.
    """

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_MODEL,
        timeout: float = DEFAULT_TIMEOUT_S,
        api_key: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Read the key from `GEMINI_API_KEY` unless one is supplied."""
        key = api_key if api_key is not None else os.environ.get(_GEMINI_KEY_ENV)
        if not key:
            raise LLMError(
                f"{_GEMINI_KEY_ENV} is not set. Export it, or set {_BACKEND_ENV} to "
                f"'groq' or 'ollama'."
            )
        self._api_key = key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep or asyncio.sleep
        #: What the alias resolved to on the last successful call, for attribution.
        self._resolved_model: str | None = None

    async def generate_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Generate JSON for `prompt`.

        `responseMimeType` pins the output to JSON; the schema travels in the prompt
        rather than in `responseSchema`, because Gemini's structured-output schema dialect
        rejects the `$defs` and `anyOf` that Pydantic emits for optional fields. Pydantic
        validates the result either way, so the weaker constraint costs nothing.
        """
        payload: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "Reply with a single JSON object and nothing else, "
                                f"conforming to this JSON schema:\n{json.dumps(schema)}\n\n"
                                f"{prompt}"
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }

        data = await self._post_with_retry(payload)

        # `DEFAULT_GEMINI_MODEL` is an alias, so record what it actually resolved to.
        # Without this a measurement is unattributable the moment Google moves the alias,
        # which is the whole objection to aliases — answered rather than ignored.
        resolved = data.get("modelVersion")
        if resolved and resolved != self._resolved_model:
            self._resolved_model = str(resolved)
            logger.info("Gemini %r resolved to %s", self._model, resolved)

        try:
            content = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Gemini returned an unexpected response shape") from exc

        if not content:
            raise LLMError("Gemini returned an empty response")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Gemini returned invalid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise LLMError("Gemini returned JSON that is not an object")
        return parsed

    async def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the generation, retrying rate limits and transient upstream failures."""
        last = "no attempt was made"
        path = f"/models/{self._model}:generateContent"

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=GEMINI_BASE_URL, timeout=httpx.Timeout(self._timeout)
                ) as client:
                    response = await client.post(
                        path,
                        json=payload,
                        # The key goes in a header, not the query string: a URL lands in
                        # proxy and server logs in a way a header does not.
                        headers={"x-goog-api-key": self._api_key},
                    )
                    if response.status_code in _RETRYABLE:
                        last = f"Gemini returned HTTP {response.status_code}"
                        if attempt == self._max_retries:
                            break
                        delay = _retry_delay(
                            response.headers.get("retry-after"), attempt, response.text
                        )
                        logger.warning(
                            "%s; retrying in %.1fs — %s",
                            last,
                            delay,
                            _limit_detail(response.text),
                        )
                        await self._sleep(delay)
                        continue

                    response.raise_for_status()
                    parsed: dict[str, Any] = response.json()
                    return parsed

            except httpx.TimeoutException as exc:
                raise LLMError(f"Gemini generation timed out after {self._timeout}s") from exc
            except httpx.HTTPStatusError as exc:
                raise LLMError(f"Gemini returned HTTP {exc.response.status_code}") from exc
            except httpx.RequestError as exc:
                raise LLMError(f"Gemini network error: {exc}") from exc

        raise LLMError(f"{last} after {self._max_retries} retries")

    async def warm(self) -> bool:
        """Reachability check. Hosted inference has no model to load."""
        try:
            await self.generate_structured("Reply with {}", {"type": "object"})
        except LLMError as exc:
            logger.warning("Gemini did not respond to the warm-up: %s", exc)
            return False
        return True


def client_from_env() -> LLMClient:
    """The configured backend.

    `AGENTIAM_LLM_BACKEND` selects explicitly (`groq` or `ollama`). With nothing set, a
    present `GROQ_API_KEY` means Groq and its absence means Ollama — so a machine with no
    key still runs, on the local model, rather than failing at import.

    A `.env` beside the repository root is read first, so a developer does not have to
    export anything to run the console.
    """
    from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient

    load_dotenv()
    backend = os.environ.get(_BACKEND_ENV, "").strip().lower()

    if backend == "ollama":
        return OllamaClient()
    if backend == "groq":
        return GroqClient()
    if backend == "gemini":
        return GeminiClient()
    if backend:
        raise LLMError(f"{_BACKEND_ENV} must be 'groq', 'gemini' or 'ollama', got {backend!r}")

    # Gemini first among the hosted options: its free quota is metered per request rather
    # than by a daily token budget, so it is the one that survives an evaluation run.
    if os.environ.get(_GEMINI_KEY_ENV):
        return GeminiClient()
    if os.environ.get(_KEY_ENV):
        return GroqClient()
    logger.info("No hosted API key set; falling back to the local Ollama backend.")
    return OllamaClient()
