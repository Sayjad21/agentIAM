"""f1/f2 extraction and the startup warm-up — T-033, spec 06 §4.1 and §5.

The warm-up tests are the ones that matter. Spec 06 §4.1 measured a 14,244 ms cold call
against a 2 s hot-path timeout, so without a warm-up the first scored request after the
model is evicted *always* fails open — drift is absent rather than slow. A test that only
checked the warm path would never see it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentiam_core.decision import OracleUnavailable
from agentiam_core.drift_features import DriftFeatures
from agentiam_core.models import RequestContext
from agentiam_pep.drift import EmbeddingClient, FeatureExtractor

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def embedding(*values: float) -> MagicMock:
    return MagicMock(status_code=200, json=lambda: {"embedding": list(values)})


@pytest.fixture
def client() -> Iterator[EmbeddingClient]:
    c = EmbeddingClient(timeout=0.1, warm_timeout=1.0)
    yield c
    c.close()


class TestEmbeddingClient:
    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_embed_returns_the_vector(self, post: MagicMock, client: EmbeddingClient) -> None:
        post.return_value = embedding(1.0, 0.0, 0.0)
        assert client.embed("hello") == [1.0, 0.0, 0.0]

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_embed_caches_so_a_repeat_costs_no_call(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        post.return_value = embedding(1.0, 0.0)
        client.embed("same text")
        client.embed("same text")
        assert post.call_count == 1

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_a_network_error_is_oracle_unavailable(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        post.side_effect = httpx.RequestError("connection refused")
        with pytest.raises(OracleUnavailable):
            client.embed("hello")

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_an_empty_embedding_is_oracle_unavailable(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        post.return_value = MagicMock(status_code=200, json=lambda: {"embedding": []})
        with pytest.raises(OracleUnavailable):
            client.embed("hello")

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_a_failed_embed_is_not_cached(self, post: MagicMock, client: EmbeddingClient) -> None:
        # The cold window is exactly when calls fail. Caching the failure would keep
        # drift dead until the process restarts.
        post.side_effect = httpx.RequestError("down")
        with pytest.raises(OracleUnavailable):
            client.embed("hello")

        post.side_effect = None
        post.return_value = embedding(1.0, 0.0)
        assert client.embed("hello") == [1.0, 0.0]


class TestCacheBound:
    """Action intent text is caller-controlled, so the bound is what stops it growing."""

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_the_cache_evicts_once_it_is_full(self, post: MagicMock) -> None:
        # A bound never observed to evict is not a bound. Injected small rather than
        # inserting 2,048 entries to watch the same branch fire.
        client = EmbeddingClient(timeout=0.1, cache_max=2)
        post.return_value = embedding(1.0, 0.0)
        for text in ("one", "two", "three"):
            client.embed(text)

        assert len(client._cache) <= 2
        assert "one" not in client._cache  # oldest insertion dropped
        assert "three" in client._cache
        client.close()

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_an_evicted_entry_is_refetched_rather_than_lost(self, post: MagicMock) -> None:
        client = EmbeddingClient(timeout=0.1, cache_max=2)
        post.return_value = embedding(1.0, 0.0)
        for text in ("one", "two", "three"):
            client.embed(text)

        before = post.call_count
        assert client.embed("one") == [1.0, 0.0]
        assert post.call_count == before + 1
        client.close()

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_a_malformed_response_body_is_oracle_unavailable(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        # Ollama answering 200 with something that is not the expected shape must be a
        # fail-open, not an unhandled TypeError escaping onto the request path.
        post.return_value = MagicMock(
            status_code=200, json=MagicMock(side_effect=ValueError("not json"))
        )
        with pytest.raises(OracleUnavailable, match="bad response"):
            client.embed("hello")


class TestWarmUp:
    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_warm_reports_success(self, post: MagicMock, client: EmbeddingClient) -> None:
        post.return_value = embedding(1.0, 0.0)
        assert client.warm() is True

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_warm_asks_the_model_to_stay_resident(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        # Ollama evicts an idle model after ~5 minutes, so a one-shot warm-up would
        # leave the 14 s cold path reachable during normal operation (spec 06 §4.1).
        post.return_value = embedding(1.0, 0.0)
        client.warm()
        _, kwargs = post.call_args
        assert kwargs["json"]["keep_alive"] == client.keep_alive

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_warm_allows_far_longer_than_the_hot_path(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        # 14,244 ms measured cold vs a 2 s hot-path timeout: warming with the hot-path
        # budget would time out every time and warm nothing.
        post.return_value = embedding(1.0, 0.0)
        client.warm()
        _, kwargs = post.call_args
        assert kwargs["timeout"] == client.warm_timeout
        assert client.warm_timeout > client.timeout

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_warm_never_raises_when_ollama_is_down(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        # Startup must not fail because an advisory heuristic is unavailable.
        post.side_effect = httpx.RequestError("connection refused")
        assert client.warm() is False


class TestFeatureExtractor:
    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_all_three_features_are_computed(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        post.side_effect = [
            embedding(1.0, 0.0),  # task
            embedding(1.0, 0.0),  # template
            embedding(0.0, 1.0),  # rendered
        ]
        features = FeatureExtractor(client).extract(a_context())
        assert features.f1 == Decimal("1.0000")
        assert features.f2 == Decimal("0.0000")
        assert features.f5 == Decimal("1.0000")

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_f1_and_f2_differ_for_the_same_request(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        # Spec 06 §5.1: they embed different strings, so they are two features.
        post.side_effect = [
            embedding(1.0, 0.0),
            embedding(0.7071, 0.7071),
            embedding(0.0, 1.0),
        ]
        features = FeatureExtractor(client).extract(a_context())
        assert features.f1 != features.f2

    def test_f5_is_computed_without_any_embedding_model(self) -> None:
        # f5 is the feature that survives Ollama being down, and the only one that can
        # see an amount attack.
        features = FeatureExtractor(None).extract(a_context())
        assert features.f5 == Decimal("1.0000")
        assert features.f1 is None
        assert features.f2 is None

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_an_unavailable_model_leaves_f1_and_f2_absent_not_zero(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        # Absent means "not computed". Zero would be a false observation of total
        # divergence, and would poison the deferred dataset (spec 06 §5.3).
        post.side_effect = httpx.RequestError("down")
        features = FeatureExtractor(client).extract(a_context())
        assert features.f1 is None
        assert features.f2 is None
        assert features.f5 == Decimal("1.0000")

    def test_a_missing_task_intent_yields_no_features_at_all(self) -> None:
        features = FeatureExtractor(None).extract(a_context(task_intent_text=None))
        assert features == DriftFeatures()

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_extraction_never_raises(self, post: MagicMock, client: EmbeddingClient) -> None:
        # Spec 06 §5.3: features are observational, so a failure degrades to absent
        # features rather than propagating into the decision.
        post.side_effect = RuntimeError("something unforeseen")
        assert FeatureExtractor(client).extract(a_context()) is not None

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_the_task_embedding_is_reused_across_requests(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        # Three embeddings for the first request, then only the rendered action changes.
        post.side_effect = [embedding(1.0, 0.0)] * 6
        extractor = FeatureExtractor(client)
        extractor.extract(a_context())
        first = post.call_count
        extractor.extract(a_context())
        assert post.call_count == first  # identical request, fully cached

    @patch("agentiam_pep.drift.httpx.Client.post")
    def test_f5_catches_the_amount_attack_while_f2_does_not(
        self, post: MagicMock, client: EmbeddingClient
    ) -> None:
        """The measurement that justifies f5 existing (spec 06 §5.1)."""
        post.side_effect = [embedding(1.0, 0.0)] * 3
        inflated = FeatureExtractor(client).extract(a_context(args={"payment.amount": 95000000000}))
        assert inflated.f5 == Decimal("0.0000")


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
        cached.close()
        assert result.f5 == Decimal("1.0000")

        timings = sorted(benchmark.stats.stats.data)  # type: ignore[attr-defined]
        p99 = timings[int(len(timings) * 0.99)]
        # Generous against NFR-1's 1 ms, because this is off the decision's critical
        # path by design. It exists to catch an order-of-magnitude regression — an
        # accidental re-embed, or a cosine that stopped being cached.
        assert p99 < 0.002, f"p99 was {p99 * 1e6:.0f} us for cached-vector extraction"
