"""f1/f2 extraction and the startup warm-up — T-033, spec 06 §4.1 and §5.

The warm-up tests are the ones that matter. Spec 06 §4.1 measured a 14,244 ms cold call
against a 2 s hot-path timeout, so without a warm-up the first scored request after the
model is evicted *always* fails open — drift is absent rather than slow. A test that only
checked the warm path would never see it.

Since APICTA-07 the extractor reads vectors the client already holds and fetches misses in
the background, so a test that wants f1/f2 computed fills the cache first (`await
client.embed(...)`, or `drain()` after a first extraction). That is what the PEP does: the
first request for a new text records the features as absent, the next records them.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from agentiam_core.decision import OracleUnavailable
from agentiam_core.drift_features import DriftFeatures, action_template, rendered_action
from agentiam_core.models import RequestContext
from agentiam_pep.drift import EmbeddingClient, FeatureExtractor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TASK = "Pay invoice INV-2291 from vendor Rahman Textiles for 45000 BDT"


def a_context(**over: Any) -> RequestContext:
    base: dict[str, Any] = {
        "operation": "payment:initiate",
        "tool": "payment_api",
        "args": {"payment.amount": 450000000},
        "task_intent_text": TASK,
        "action_intent_text": "Pay Rahman Textiles",
    }
    return RequestContext.model_construct(**(base | over))


class RecordingModel:
    """A fake embedding endpoint that remembers every request and answers as configured."""

    def __init__(self) -> None:
        """Answer `[1.0, 0.0]` to everything until told otherwise."""
        self.requests: list[httpx.Request] = []
        self.vectors: dict[str, list[float]] = {}
        self.default: list[float] = [1.0, 0.0]
        self.raise_error: Exception | None = None
        self.response: httpx.Response | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        if self.response is not None:
            return self.response
        prompt = json.loads(request.content)["prompt"]
        return httpx.Response(200, json={"embedding": self.vectors.get(prompt, self.default)})

    def payload(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)  # type: ignore[no-any-return]

    def timeout(self, index: int = -1) -> float:
        return float(self.requests[index].extensions["timeout"]["read"])


@pytest.fixture
def model() -> RecordingModel:
    return RecordingModel()


@pytest.fixture
async def client(model: RecordingModel) -> AsyncIterator[EmbeddingClient]:
    c = EmbeddingClient(timeout=0.5, warm_timeout=1.0, transport=httpx.MockTransport(model.handle))
    yield c
    await c.aclose()


async def fill(client: EmbeddingClient, context: RequestContext) -> None:
    """Fetch the three texts f1/f2 need, the way the PEP's background prefetch would."""
    await client.embed(context.task_intent_text or "")
    await client.embed(action_template(context.operation, context.tool))
    await client.embed(rendered_action(context.operation, context.tool, context.args))


class TestEmbeddingClient:
    async def test_embed_returns_the_vector(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        model.default = [1.0, 0.0, 0.0]
        assert await client.embed("hello") == [1.0, 0.0, 0.0]

    async def test_embed_caches_so_a_repeat_costs_no_call(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        await client.embed("same text")
        await client.embed("same text")
        assert len(model.requests) == 1

    async def test_a_network_error_is_oracle_unavailable(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        model.raise_error = httpx.ConnectError("connection refused")
        with pytest.raises(OracleUnavailable):
            await client.embed("hello")

    async def test_an_empty_embedding_is_oracle_unavailable(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        model.default = []
        with pytest.raises(OracleUnavailable):
            await client.embed("hello")

    async def test_a_failed_embed_is_not_cached(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # The cold window is exactly when calls fail. Caching the failure would keep
        # drift dead until the process restarts.
        model.raise_error = httpx.ConnectError("down")
        with pytest.raises(OracleUnavailable):
            await client.embed("hello")

        model.raise_error = None
        assert await client.embed("hello") == [1.0, 0.0]

    async def test_a_malformed_response_body_is_oracle_unavailable(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # Ollama answering 200 with something that is not the expected shape must be a
        # fail-open, not an unhandled error escaping onto the request path.
        model.response = httpx.Response(200, content=b"not json")
        with pytest.raises(OracleUnavailable, match="bad response"):
            await client.embed("hello")

    async def test_cached_never_does_io(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        assert client.cached("hello") is None
        assert model.requests == []


class TestCacheBound:
    """Action intent text is caller-controlled, so the bound is what stops it growing."""

    async def test_the_cache_evicts_once_it_is_full(self, model: RecordingModel) -> None:
        # A bound never observed to evict is not a bound. Injected small rather than
        # inserting 2,048 entries to watch the same branch fire.
        client = EmbeddingClient(cache_max=2, transport=httpx.MockTransport(model.handle))
        for text in ("one", "two", "three"):
            await client.embed(text)

        assert client.cached("one") is None  # oldest insertion dropped
        assert client.cached("three") is not None
        await client.aclose()

    async def test_an_evicted_entry_is_refetched_rather_than_lost(
        self, model: RecordingModel
    ) -> None:
        client = EmbeddingClient(cache_max=2, transport=httpx.MockTransport(model.handle))
        for text in ("one", "two", "three"):
            await client.embed(text)

        before = len(model.requests)
        assert await client.embed("one") == [1.0, 0.0]
        assert len(model.requests) == before + 1
        await client.aclose()


class TestWarmUp:
    async def test_warm_reports_success(self, client: EmbeddingClient) -> None:
        assert await client.warm() is True

    async def test_warm_asks_the_model_to_stay_resident(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # Ollama evicts an idle model after ~5 minutes, so a one-shot warm-up would
        # leave the 14 s cold path reachable during normal operation (spec 06 §4.1).
        await client.warm()
        assert model.payload()["keep_alive"] == client.keep_alive

    async def test_warm_allows_far_longer_than_a_fetch(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # 14,244 ms measured cold: warming with the per-fetch budget would time out every
        # time and warm nothing.
        await client.warm()
        assert model.timeout() == client.warm_timeout
        assert client.warm_timeout > client.timeout

    async def test_warm_never_raises_when_ollama_is_down(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # Startup must not fail because an advisory heuristic is unavailable.
        model.raise_error = httpx.ConnectError("connection refused")
        assert await client.warm() is False


class TestFeatureExtractor:
    async def test_all_three_features_are_computed(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        context = a_context()
        model.vectors = {
            TASK: [1.0, 0.0],
            action_template(context.operation, context.tool): [1.0, 0.0],
            rendered_action(context.operation, context.tool, context.args): [0.0, 1.0],
        }
        await fill(client, context)
        features = FeatureExtractor(client).extract(context)
        assert features.f1 == Decimal("1.0000")
        assert features.f2 == Decimal("0.0000")
        assert features.f5 == Decimal("1.0000")

    async def test_f1_and_f2_differ_for_the_same_request(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # Spec 06 §5.1: they embed different strings, so they are two features.
        context = a_context()
        model.vectors = {
            TASK: [1.0, 0.0],
            action_template(context.operation, context.tool): [0.7071, 0.7071],
            rendered_action(context.operation, context.tool, context.args): [0.0, 1.0],
        }
        await fill(client, context)
        features = FeatureExtractor(client).extract(context)
        assert features.f1 != features.f2

    def test_f5_is_computed_without_any_embedding_model(self) -> None:
        # f5 is the feature that survives Ollama being down, and the only one that can
        # see an amount attack.
        features = FeatureExtractor(None).extract(a_context())
        assert features.f5 == Decimal("1.0000")
        assert features.f1 is None
        assert features.f2 is None

    async def test_an_unavailable_model_leaves_f1_and_f2_absent_not_zero(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # Absent means "not computed". Zero would be a false observation of total
        # divergence, and would poison the deferred dataset (spec 06 §5.3).
        model.raise_error = httpx.ConnectError("down")
        extractor = FeatureExtractor(client)
        extractor.extract(a_context())
        await client.drain()
        features = extractor.extract(a_context())
        assert features.f1 is None
        assert features.f2 is None
        assert features.f5 == Decimal("1.0000")

    def test_a_missing_task_intent_yields_no_features_at_all(self) -> None:
        features = FeatureExtractor(None).extract(a_context(task_intent_text=None))
        assert features == DriftFeatures()

    async def test_extraction_never_raises(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        # Spec 06 §5.3: features are observational, so a failure degrades to absent
        # features rather than propagating into the decision.
        await fill(client, a_context())
        client._cache[TASK] = []  # a vector cosine cannot use
        assert FeatureExtractor(client).extract(a_context()) is not None

    async def test_a_held_request_costs_no_model_call(
        self, model: RecordingModel, client: EmbeddingClient
    ) -> None:
        context = a_context()
        await fill(client, context)
        before = len(model.requests)
        extractor = FeatureExtractor(client)
        extractor.extract(context)
        extractor.extract(context)
        assert len(model.requests) == before

    async def test_f5_catches_the_amount_attack_while_f2_does_not(
        self, client: EmbeddingClient
    ) -> None:
        """The measurement that justifies f5 existing (spec 06 §5.1)."""
        inflated = a_context(args={"payment.amount": 95000000000})
        await fill(client, inflated)
        assert FeatureExtractor(client).extract(inflated).f5 == Decimal("0.0000")


@pytest.mark.perf
class TestFeatureExtractionLatency:
    """T-033's fourth acceptance criterion: p99 measured and reported.

    Two numbers, because they answer different questions and only one can run in CI:

    * **Here** — the extraction work the PEP does per request once vectors are in hand:
      f5, both action renderings, and two cosines over 768 dimensions. Deterministic, so
      it is a gate rather than an observation.
    * **Spec 06 §4.1/§4.2** — the model-backed number, measured against a real Ollama:
      17.8 ms median for one warm embedding, 83.3 ms for a full cache miss, 14,244 ms
      cold. That cannot be a CI gate without shipping a 260 MB model into the runner.

    PLAN.md:1130 allows this path to be slow. The point of the number is that it is
    *known*, and that the cold case is known to be catastrophic rather than merely slow.
    """

    def test_p99_extraction_over_768_dimensions(self, benchmark: object) -> None:
        vector_a = [float(i % 7) for i in range(768)]
        vector_b = [float((i + 3) % 7) for i in range(768)]

        # Vectors pinned directly so the benchmark measures extraction, not the network.
        cached = EmbeddingClient(timeout=0.1)
        cached._cache[TASK] = vector_a
        cached._cache["payment:initiate using payment_api"] = vector_b
        cached._cache["payment:initiate using payment_api with amount=45000"] = vector_b

        extractor = FeatureExtractor(cached)
        context = a_context()

        result = benchmark(lambda: extractor.extract(context))  # type: ignore[operator]
        assert result.f5 == Decimal("1.0000")

        timings = sorted(benchmark.stats.stats.data)  # type: ignore[attr-defined]
        p99 = timings[int(len(timings) * 0.99)]
        # Generous against NFR-1's 1 ms, because this is off the decision's critical
        # path by design. It exists to catch an order-of-magnitude regression — an
        # accidental re-embed, or a cosine that stopped being cached.
        assert p99 < 0.002, f"p99 was {p99 * 1e6:.0f} us for cached-vector extraction"
