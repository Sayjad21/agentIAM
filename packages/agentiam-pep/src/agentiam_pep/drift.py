"""Drift detection and feature extraction — T-032, T-033, APICTA-07; spec 06.

Three things live here, in increasing order of how much they can hurt:

* `EmbeddingClient` — the only place that talks to the embedding model. It holds **one**
  `httpx.AsyncClient` for the process, caches by text, shares one in-flight fetch per text,
  and knows how to warm the model.
* `FeatureExtractor` — f1, f2 and f5 (spec 06 §5). Observational: it never raises, never
  waits for the model, and a missing vector degrades to an absent feature rather than to a
  denial.
* `RuleBasedDriftOracle` — the v0 score `decide()` actually escalates on (spec 06 §2).

**Nothing here blocks the event loop, and nothing on the decision path waits unboundedly.**
`decide()` is pure and synchronous, so the oracle's `score_for()` answers only from what it
already holds and raises `ScoreNotReady` when it would need the model. The pipeline then
awaits `assess()`, which waits at most `assess_timeout_s` before giving up on *this* request.
It used to be otherwise: `score_for()` called a synchronous `httpx.Client` from inside
`decide()`, so one uncached request against a hung model stalled every request in the process
for the client's full timeout (CH-8, STATUS gap 22).

**A fetch that outlives the bound is not cancelled.** It finishes in the background and fills
the cache, so a slow model costs one request its drift score rather than costing every later
request a timeout. Fetches are single-flight per text, so a burst of requests cannot open a
burst of connections to the model.

**Why the warm-up is a correctness requirement, not an optimisation.** Spec 06 §4.1 measured a
cold embedding call at 14,244 ms against `nomic-embed-text`. Ollama evicts an idle model after
roughly five minutes, so `warm()` sets `keep_alive` instead of just firing one request.

The pure arithmetic — the cosine, the two action renderings, f5 entire — lives in
`agentiam_core.drift_features`, since none of it needs I/O.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

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
    "DEFAULT_ASSESS_TIMEOUT_S",
    "AssessingDriftOracle",
    "EmbeddingClient",
    "FeatureExtractor",
    "RuleBasedDriftOracle",
    "ScoreNotReady",
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

#: How long one request may wait for the model before its drift score is "not assessed".
#: Measured for APICTA-07 against `nomic-embed-text` on the development laptop (CPU, the GPU
#: driver unavailable): an uncached warm embedding is p50 ~7 ms and p99 8-10 ms across three
#: runs of 200, a cold one 2.8 s, and an occasional stall of 4.7-8.5 s. 120 ms is over ten
#: times the warm p99, so it does not quietly turn drift off, and it is the bound that keeps
#: those stalls off the request.
DEFAULT_ASSESS_TIMEOUT_S: Final = 0.12


class ScoreNotReady(OracleUnavailable):
    """The score needs the embedding model, and `decide()` may not wait for it.

    An `OracleUnavailable`, deliberately: any caller that hands this oracle straight to
    `decide()` without awaiting `assess()` first gets drift's fail-open (spec 09 §5) rather
    than a blocked loop or a denial.
    """


@runtime_checkable
class AssessingDriftOracle(Protocol):
    """A drift oracle whose answer may need I/O, which the pipeline awaits before deciding."""

    def score_for(self, context: RequestContext) -> Decimal | None:
        """Answer without I/O, or raise `ScoreNotReady`."""
        ...

    async def assess(self, context: RequestContext) -> Decimal | None:
        """Answer, waiting a bounded time for the model if necessary."""
        ...


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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Open the shared client. Nothing is fetched until `warm`, `embed` or `prefetch`.

        Args:
            base_url: The Ollama endpoint.
            model: The embedding model name.
            timeout: Bound on one fetch. A fetch outlives a request's own, shorter wait.
            warm_timeout: Bound on the warm-up, which can be a cold model load.
            keep_alive: How long Ollama should keep the model resident after a call.
            cache_max: Most vectors retained.
            transport: Injected by tests to stand in for the model.
        """
        self.model = model
        self.timeout = timeout
        self.warm_timeout = warm_timeout
        self.keep_alive = keep_alive
        self.cache_max = cache_max
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._cache: dict[str, list[float]] = {}
        self._inflight: dict[str, asyncio.Task[list[float]]] = {}

    # -- lifecycle ----------------------------------------------------------------

    async def warm(self) -> bool:
        """Load the model and ask it to stay resident. Never raises.

        Returns:
            Whether the model answered. `False` means drift will fail open until it does;
            it is not a startup failure, because an advisory heuristic must not stop a
            PEP from serving.
        """
        try:
            await self._post("warm-up", timeout=self.warm_timeout, keep_alive=self.keep_alive)
        except OracleUnavailable as exc:
            logger.warning("Embedding model did not warm: %s", exc)
            return False
        logger.info("Embedding model %s warmed, keep_alive=%s", self.model, self.keep_alive)
        return True

    async def drain(self) -> None:
        """Wait for every in-flight fetch to finish, whatever its outcome."""
        while self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)

    async def aclose(self) -> None:
        """Abandon in-flight fetches and release the shared connection."""
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._client.aclose()

    # -- reading ------------------------------------------------------------------

    def cached(self, text: str) -> list[float] | None:
        """The vector for `text` if already held. Never does I/O."""
        return self._cache.get(text)

    def prefetch(self, text: str) -> None:
        """Start fetching `text` in the background, unless it is held or already in flight.

        A no-op outside a running event loop, so synchronous callers can call it freely.
        """
        if text in self._cache or text in self._inflight:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._start(text)

    async def embed(self, text: str) -> list[float]:
        """The vector for `text`, from cache when possible.

        Shielded: a caller that stops waiting — `assess()` hitting its bound — leaves the
        fetch running, so its result still lands in the cache.

        Raises:
            OracleUnavailable: The model could not be reached or returned nothing usable.
        """
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        task = self._inflight.get(text) or self._start(text)
        return await asyncio.shield(task)

    # -- internals ----------------------------------------------------------------

    def _start(self, text: str) -> asyncio.Task[list[float]]:
        task = asyncio.get_running_loop().create_task(self._fetch(text))
        self._inflight[text] = task
        task.add_done_callback(lambda done: self._settle(text, done))
        return task

    def _settle(self, text: str, task: asyncio.Task[list[float]]) -> None:
        self._inflight.pop(text, None)
        if not task.cancelled():
            # Retrieved here so a failure nobody awaited — every caller already gave up —
            # is not reported as "Task exception was never retrieved".
            task.exception()

    async def _fetch(self, text: str) -> list[float]:
        vector = await self._post(text, timeout=self.timeout)
        if len(self._cache) >= self.cache_max:
            # Cheapest bound that cannot be steered: drop the oldest insertion.
            self._cache.pop(next(iter(self._cache)))
        self._cache[text] = vector
        return vector

    async def _post(
        self, text: str, *, timeout: float, keep_alive: str | None = None
    ) -> list[float]:
        payload: dict[str, object] = {"model": self.model, "prompt": text}
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive

        try:
            response = await self._client.post("/api/embeddings", json=payload, timeout=timeout)
            response.raise_for_status()
            vector = response.json().get("embedding")
        except httpx.HTTPStatusError as exc:
            raise OracleUnavailable(f"embedding HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise OracleUnavailable(f"embedding network error: {exc!r}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise OracleUnavailable(f"embedding bad response: {exc}") from exc

        if not vector:
            raise OracleUnavailable("embedding model returned an empty vector")
        return [float(x) for x in vector]


class FeatureExtractor:
    """f1, f2 and f5 for one request — spec 06 §5.

    Observational by contract: `extract` never raises, never denies and never waits. The
    record it feeds is written synchronously, so f1 and f2 come from vectors already held;
    a missing vector is fetched in the background for next time and the feature is absent
    now. f3, f4 and f6 need per-task history the PEP does not hold, and are deferred
    (ADR-036).
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
            task_vector = _held(self._embeddings, task)
            template_vector = _held(self._embeddings, template)
            rendered_vector = _held(self._embeddings, rendered)
            f1 = (
                _score(cosine_similarity(task_vector, template_vector))
                if task_vector is not None and template_vector is not None
                else None
            )
            f2 = (
                _score(cosine_similarity(task_vector, rendered_vector))
                if task_vector is not None and rendered_vector is not None
                else None
            )
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
        assess_timeout_s: float = DEFAULT_ASSESS_TIMEOUT_S,
    ) -> None:
        """Reuse a shared `EmbeddingClient` when given one, so the warm-up carries over.

        Args:
            base_url: The Ollama endpoint, when no client is supplied.
            model: The embedding model, when no client is supplied.
            timeout: Bound on one background fetch, when no client is supplied.
            embeddings: A shared client.
            assess_timeout_s: How long one request may wait for the model.
        """
        self._embeddings = embeddings or EmbeddingClient(
            base_url=base_url, model=model, timeout=timeout
        )
        self.assess_timeout_s = assess_timeout_s
        self._scores: dict[tuple[str, str], Decimal] = {}
        #: Outcomes of `assess()` calls that needed the model: `fetched`, `timeout`, `error`.
        #: Exported by the PEP's `/metrics`.
        self.outcomes: Counter[str] = Counter()

    @property
    def embeddings(self) -> EmbeddingClient:
        """The underlying client, so a caller can warm and close it."""
        return self._embeddings

    def score_for(self, context: RequestContext) -> Decimal | None:
        """The drift score from what is already held. Never does I/O.

        Returns:
            The score, or `None` when drift does not apply (an intent text is missing).

        Raises:
            ScoreNotReady: The score needs the model. `decide()` treats this as unavailable
                and fails open; the pipeline awaits `assess()` instead.
        """
        task = context.task_intent_text
        action = context.action_intent_text
        if not task or not action:
            return None

        cached = self._scores.get((task, action))
        if cached is not None:
            return cached

        score = _rule_score(task, action)
        if score is None:
            task_vector = self._embeddings.cached(task)
            action_vector = self._embeddings.cached(action)
            if task_vector is None or action_vector is None:
                raise ScoreNotReady("drift needs the embedding model")
            score = _cosine_score(task_vector, action_vector)
        self._remember(task, action, score)
        return score

    async def assess(self, context: RequestContext) -> Decimal | None:
        """The drift score, waiting at most `assess_timeout_s` for the model.

        Returns:
            The score, or `None` when drift does not apply, the model failed, or the bound
            passed. `None` is spec 06 §2.1's fail-open: drift is advisory and never denies.
        """
        try:
            return self.score_for(context)
        except ScoreNotReady:
            pass

        task = context.task_intent_text
        action = context.action_intent_text
        if not task or not action:  # pragma: no cover - `score_for` returned None above
            return None

        try:
            task_vector, action_vector = await asyncio.wait_for(
                asyncio.gather(self._embeddings.embed(task), self._embeddings.embed(action)),
                timeout=self.assess_timeout_s,
            )
        except TimeoutError:
            self.outcomes["timeout"] += 1
            logger.info(
                "drift not assessed: the embedding model took longer than %.0f ms",
                self.assess_timeout_s * 1000,
            )
            return None
        except OracleUnavailable as exc:
            self.outcomes["error"] += 1
            logger.info("drift not assessed: %s", exc)
            return None

        self.outcomes["fetched"] += 1
        score = _cosine_score(task_vector, action_vector)
        self._remember(task, action, score)
        return score

    def _remember(self, task: str, action: str, score: Decimal) -> None:
        if len(self._scores) >= _CACHE_MAX:
            self._scores.pop(next(iter(self._scores)))
        self._scores[(task, action)] = score


def _held(embeddings: EmbeddingClient, text: str) -> list[float] | None:
    """The cached vector, or `None` after asking for it to be fetched in the background."""
    vector = embeddings.cached(text)
    if vector is None:
        embeddings.prefetch(text)
    return vector


def _rule_score(task: str, action: str) -> Decimal | None:
    """The two cases v0 scores without a model, or `None` when it needs one."""
    if task == action:
        return Decimal("0.0")
    task_words = set(task.lower().split())
    action_words = set(action.lower().split())
    if action_words and action_words.issubset(task_words):
        return Decimal("0.1")
    return None


def _cosine_score(task_vector: list[float], action_vector: list[float]) -> Decimal:
    similarity = cosine_similarity(task_vector, action_vector)
    if similarity < _SIMILARITY_THRESHOLD:
        return _DRIFT_THRESHOLD + Decimal("0.1")

    # Linear from (0.3 similarity -> 0.7 drift) to (1.0 similarity -> 0.0 drift),
    # clamped so only a strictly orthogonal pair crosses the escalation threshold.
    raw = 1.0 - ((similarity - _SIMILARITY_THRESHOLD) / (1.0 - _SIMILARITY_THRESHOLD))
    return _score(min(0.7, max(0.0, raw)))


def _score(value: float) -> Decimal:
    """One place where a float becomes the `Decimal` the record demands (Rule 4)."""
    return Decimal(str(value)).quantize(_QUANTUM)
