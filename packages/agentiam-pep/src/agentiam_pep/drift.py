"""Drift detection and feature extraction — T-032, T-033; spec 06.

Three things live here, in increasing order of how much they can hurt:

* `EmbeddingClient` — the only place that talks to the embedding model. It holds **one**
  `httpx.Client` for the process, caches by text, and knows how to warm the model.
* `FeatureExtractor` — f1, f2 and f5 (spec 06 §5). Observational: it never raises, and a
  missing model degrades to absent features rather than to a denial.
* `RuleBasedDriftOracle` — the v0 score `decide()` actually escalates on (spec 06 §2).

**Why the warm-up is a correctness requirement, not an optimisation.** Spec 06 §4.1 measured
a cold embedding call at 14,244 ms against `nomic-embed-text`, and the hot path allows 2 s.
So the first scored request after the model is evicted does not merely run slowly — it times
out and fails open, every time. Ollama evicts an idle model after roughly five minutes, so
this recurs during normal operation rather than only at boot, which is why `warm()` sets
`keep_alive` instead of just firing one request.

The pure arithmetic — the cosine, the two action renderings, f5 entire — lives in
`agentiam_core.drift_features`, since none of it needs I/O.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx

from agentiam_core.decision import DriftOracle, OracleUnavailable
from agentiam_core.drift_features import (
    DriftFeatures,
    action_template,
    argument_entity_overlap,
    cosine_similarity,
    rendered_action,
)

if TYPE_CHECKING:
    from agentiam_core.models import RequestContext

logger = logging.getLogger(__name__)

__all__ = [
    "EmbeddingClient",
    "FeatureExtractor",
    "RuleBasedDriftOracle",
    "cosine_similarity",
]

#: A score above this in v0 means high divergence (spec 06 §2).
_DRIFT_THRESHOLD: Final = Decimal("0.7")

#: Below this cosine, the two intents are treated as orthogonal.
_SIMILARITY_THRESHOLD: Final = 0.3

_QUANTUM: Final = Decimal("0.0001")

#: Cap on cached embeddings. Action intent text is caller-controlled, so this is the bound
#: on what a caller can make the process retain.
_CACHE_MAX: Final = 2048


class EmbeddingClient:
    """One process-wide connection to the local embedding model, with a warm-up.

    Caching is by text rather than by request identity: two requests asserting the same
    action ask the same question, whatever their digests. Failures are **not** cached —
    the cold window is precisely when calls fail, and remembering that would keep drift
    dead until the process restarted.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout: float = 2.0,
        warm_timeout: float = 60.0,
        keep_alive: str = "30m",
        cache_max: int = _CACHE_MAX,
    ) -> None:
        """Open the shared client. Nothing is fetched until `warm` or `embed` is called."""
        self.model = model
        self.timeout = timeout
        self.warm_timeout = warm_timeout
        self.keep_alive = keep_alive
        self.cache_max = cache_max
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._cache: dict[str, list[float]] = {}

    # -- lifecycle ----------------------------------------------------------------

    def warm(self) -> bool:
        """Load the model and ask it to stay resident. Never raises.

        Returns:
            Whether the model answered. `False` means drift will fail open until it does;
            it is not a startup failure, because an advisory heuristic must not stop a
            PEP from serving.
        """
        try:
            self._post("warm-up", timeout=self.warm_timeout, keep_alive=self.keep_alive)
        except OracleUnavailable as exc:
            logger.warning("Embedding model did not warm: %s", exc)
            return False
        logger.info("Embedding model %s warmed, keep_alive=%s", self.model, self.keep_alive)
        return True

    def close(self) -> None:
        """Release the shared connection."""
        self._client.close()

    # -- the hot path -------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """The vector for `text`, from cache when possible.

        Raises:
            OracleUnavailable: The model could not be reached or returned nothing usable.
        """
        cached = self._cache.get(text)
        if cached is not None:
            return cached

        vector = self._post(text, timeout=self.timeout)
        if len(self._cache) >= self.cache_max:
            # Cheapest bound that cannot be steered: drop the oldest insertion.
            self._cache.pop(next(iter(self._cache)))
        self._cache[text] = vector
        return vector

    # -- internals ----------------------------------------------------------------

    def _post(self, text: str, *, timeout: float, keep_alive: str | None = None) -> list[float]:
        payload: dict[str, object] = {"model": self.model, "prompt": text}
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive

        try:
            response = self._client.post("/api/embeddings", json=payload, timeout=timeout)
            response.raise_for_status()
            vector = response.json().get("embedding")
        except httpx.HTTPStatusError as exc:
            raise OracleUnavailable(f"embedding HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise OracleUnavailable(f"embedding network error: {exc}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise OracleUnavailable(f"embedding bad response: {exc}") from exc

        if not vector:
            raise OracleUnavailable("embedding model returned an empty vector")
        return [float(x) for x in vector]


class FeatureExtractor:
    """f1, f2 and f5 for one request — spec 06 §5.

    Observational by contract: `extract` never raises and never denies. f3, f4 and f6 need
    per-task history the PEP does not hold, and are deferred (ADR-036).
    """

    def __init__(self, embeddings: EmbeddingClient | None) -> None:
        """`None` disables f1 and f2; f5 needs no model and is computed regardless."""
        self._embeddings = embeddings

    def extract(self, context: RequestContext) -> DriftFeatures:
        """Compute what is computable. Absent means *not computed*, never *zero*."""
        task = context.task_intent_text
        if not task:
            # Without the authenticated task text there is nothing to compare against,
            # and spec 06 §1.1 is what makes that text trustworthy in the first place.
            return DriftFeatures()

        try:
            f5 = argument_entity_overlap(task, context.args)
        # Deliberately broad: features are observational, and a malformed argument must
        # not turn into a denial (spec 06 §5.3).
        except Exception:
            logger.warning("f5 extraction failed", exc_info=True)
            f5 = None

        f1, f2 = self._embedding_features(task, context)
        return DriftFeatures(f1=f1, f2=f2, f5=f5)

    def _embedding_features(
        self, task: str, context: RequestContext
    ) -> tuple[Decimal | None, Decimal | None]:
        if self._embeddings is None:
            return None, None

        try:
            template = action_template(context.operation, context.tool)
            rendered = rendered_action(context.operation, context.tool, context.args)
            task_vector = self._embeddings.embed(task)
            f1 = _score(cosine_similarity(task_vector, self._embeddings.embed(template)))
            f2 = _score(cosine_similarity(task_vector, self._embeddings.embed(rendered)))
        except OracleUnavailable as exc:
            logger.info("f1/f2 unavailable: %s", exc)
            return None, None
        # Deliberately broad, for the same reason: nothing here may reach the decision.
        except Exception:
            logger.warning("f1/f2 extraction failed", exc_info=True)
            return None, None
        return f1, f2


class RuleBasedDriftOracle(DriftOracle):
    """The v0 score `decide()` escalates on — spec 06 §2.

    Deterministic given the model: identical intents short-circuit to 0.0, an action whose
    words are a subset of the task scores 0.1, and anything else is scored by cosine.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout: float = 2.0,
        embeddings: EmbeddingClient | None = None,
    ) -> None:
        """Reuse a shared `EmbeddingClient` when given one, so the warm-up carries over."""
        self._embeddings = embeddings or EmbeddingClient(
            base_url=base_url, model=model, timeout=timeout
        )
        self._scores: dict[tuple[str, str], Decimal] = {}

    @property
    def embeddings(self) -> EmbeddingClient:
        """The underlying client, so a caller can warm it at startup."""
        return self._embeddings

    def score_for(self, context: RequestContext) -> Decimal | None:
        """The drift score, or `None` when drift cannot be assessed.

        `None` is a fail-open: spec 06 §2.1 forbids an advisory heuristic from denying.
        """
        task = context.task_intent_text
        action = context.action_intent_text
        if not task or not action:
            return None
        return self._score(task, action)

    def _score(self, task: str, action: str) -> Decimal:
        key = (task, action)
        cached = self._scores.get(key)
        if cached is not None:
            return cached

        score = self._compute(task, action)
        if len(self._scores) >= _CACHE_MAX:
            self._scores.pop(next(iter(self._scores)))
        self._scores[key] = score
        return score

    def _compute(self, task: str, action: str) -> Decimal:
        if task == action:
            return Decimal("0.0")

        task_words = set(task.lower().split())
        action_words = set(action.lower().split())
        if action_words and action_words.issubset(task_words):
            return Decimal("0.1")

        similarity = cosine_similarity(self._embeddings.embed(task), self._embeddings.embed(action))
        if similarity < _SIMILARITY_THRESHOLD:
            return _DRIFT_THRESHOLD + Decimal("0.1")

        # Linear from (0.3 similarity -> 0.7 drift) to (1.0 similarity -> 0.0 drift),
        # clamped so only a strictly orthogonal pair crosses the escalation threshold.
        raw = 1.0 - ((similarity - _SIMILARITY_THRESHOLD) / (1.0 - _SIMILARITY_THRESHOLD))
        return _score(min(0.7, max(0.0, raw)))


def _score(value: float) -> Decimal:
    """One place where a float becomes the `Decimal` the record demands (Rule 4)."""
    return Decimal(str(value)).quantize(_QUANTUM)
