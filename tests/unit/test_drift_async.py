"""Drift scoring off the event loop, with a bounded wait — APICTA-07, STATUS gap 22.

`decide()` is pure and synchronous, and it used to reach the embedding model from step 6
through a synchronous `httpx.Client`: one uncached request against a hung Ollama blocked the
whole event loop for the client's full timeout, and every other request waited behind it
(CH-8 measured it). The fix keeps `decide()` pure and moves the network out of it:

* `RuleBasedDriftOracle.score_for()` answers only from what it already holds and raises
  `ScoreNotReady` — an `OracleUnavailable`, so `decide()` fails open — when the model is
  needed.
* `Pipeline.authorize()` notices that miss, awaits `assess()` for at most the configured
  bound, and decides again with the answer.
* A fetch that outlives the bound is not cancelled. It finishes in the background and fills
  the cache, so the next request is a hit rather than another timeout.

Every test here runs against an in-process fake model (`httpx.MockTransport`), so none of
them needs Ollama.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from agentiam_core.decision import OracleUnavailable
from agentiam_core.errors import ReasonCode
from agentiam_core.hashing import hash_object
from agentiam_core.models import DecisionRecord, RequestContext
from agentiam_pep.drift import (
    EmbeddingClient,
    FeatureExtractor,
    RuleBasedDriftOracle,
    ScoreNotReady,
)
from agentiam_pep.pipeline import Authorized, Refused
from tests.unit.test_pep_pipeline import (
    TASK_INTENT,
    a_mandate,
    a_pipeline,
    intent_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: The bound under test. Small, so a stall is unmistakable against it.
BOUND_S = 0.12

#: How long the fake model hangs when told to. Far beyond the bound, so a request that
#: waited for it would be obvious.
STALL_S = 5.0

TASK = "Pay invoice INV-2291 from vendor Rahman Textiles"
ALIGNED = "Settle the Rahman Textiles invoice"
ORTHOGONAL = "Delete the production database"


class FakeModel:
    """An embedding endpoint whose vectors, latency and failures a test controls.

    Texts are mapped to vectors by keyword so a test can say "these two agree" or "these two
    are orthogonal" without hand-building requests. `stall` makes every call hang until
    `release()` — the black hole CH-8 measured, minus the network.
    """

    def __init__(self) -> None:
        """Answer instantly, with no failures, until told otherwise."""
        self.calls: list[str] = []
        self.stall = False
        self.fail_status: int | None = None
        self._released = asyncio.Event()

    def release(self) -> None:
        """Let every stalled call finish."""
        self._released.set()

    def vector(self, text: str) -> list[float]:
        lowered = text.lower()
        if "database" in lowered:
            return [0.0, 1.0, 0.0]
        if "rahman" in lowered or "invoice" in lowered:
            return [1.0, 0.0, 0.0]
        return [0.6, 0.0, 0.8]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        import json

        text = json.loads(request.content)["prompt"]
        self.calls.append(text)
        if self.stall:
            try:
                await asyncio.wait_for(self._released.wait(), timeout=STALL_S)
            except TimeoutError:
                pass
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, json={"error": "boom"})
        return httpx.Response(200, json={"embedding": self.vector(text)})


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
async def oracle(model: FakeModel) -> AsyncIterator[RuleBasedDriftOracle]:
    client = EmbeddingClient(transport=httpx.MockTransport(model.handle))
    built = RuleBasedDriftOracle(embeddings=client, assess_timeout_s=BOUND_S)
    yield built
    model.release()
    await client.aclose()


def a_context(task: str | None = TASK, action: str | None = ORTHOGONAL) -> RequestContext:
    return RequestContext.model_construct(task_intent_text=task, action_intent_text=action)


class TestScoreForDoesNoIo:
    """What `decide()` calls. It must never touch the network, whatever the cache holds."""

    def test_a_miss_raises_score_not_ready_without_calling_the_model(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        with pytest.raises(ScoreNotReady):
            oracle.score_for(a_context())
        assert model.calls == []

    def test_score_not_ready_is_an_oracle_failure_so_decide_fails_open(self) -> None:
        # The safety property for any caller that hands this oracle straight to `decide()`
        # without assessing first: it does not block, and it does not deny.
        assert issubclass(ScoreNotReady, OracleUnavailable)

    def test_identical_intents_score_zero_without_the_model(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        assert oracle.score_for(a_context(action=TASK)) == Decimal("0.0")
        assert model.calls == []

    def test_a_subset_action_scores_low_without_the_model(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        assert oracle.score_for(a_context(action="Pay invoice INV-2291")) == Decimal("0.1")
        assert model.calls == []

    def test_missing_intent_text_is_not_assessed(self, oracle: RuleBasedDriftOracle) -> None:
        assert oracle.score_for(a_context(task=None)) is None
        assert oracle.score_for(a_context(action=None)) is None


class TestAssess:
    async def test_assess_fetches_and_then_score_for_answers_from_cache(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        score = await oracle.assess(a_context())
        assert score is not None and score > Decimal("0.7")
        fetched = len(model.calls)

        assert oracle.score_for(a_context()) == score
        assert len(model.calls) == fetched

    async def test_aligned_intents_score_low(self, oracle: RuleBasedDriftOracle) -> None:
        score = await oracle.assess(a_context(action=ALIGNED))
        assert score == Decimal("0.0000")

    async def test_a_stalled_model_costs_the_bound_and_no_more(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        model.stall = True
        started = time.perf_counter()
        score = await oracle.assess(a_context())
        elapsed = time.perf_counter() - started

        assert score is None, "a timed-out assessment is 'not assessed', never a guess"
        assert elapsed < BOUND_S + 0.1, f"assess took {elapsed * 1000:.0f} ms"
        assert oracle.outcomes["timeout"] == 1

    async def test_a_fetch_that_outlives_the_bound_still_fills_the_cache(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        # Cancelling the fetch on timeout would leave the cache empty, so every later
        # request would time out the same way for as long as the model stayed slow.
        model.stall = True
        assert await oracle.assess(a_context()) is None

        model.release()
        await oracle.embeddings.drain()
        calls_after_fill = len(model.calls)

        assert oracle.score_for(a_context()) is not None
        assert len(model.calls) == calls_after_fill

    async def test_concurrent_misses_share_one_fetch_per_text(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        await asyncio.gather(*(oracle.assess(a_context()) for _ in range(10)))
        assert sorted(model.calls) == sorted([TASK, ORTHOGONAL])

    async def test_a_model_error_is_not_assessed_and_is_not_cached(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        model.fail_status = 500
        assert await oracle.assess(a_context()) is None
        assert oracle.outcomes["error"] == 1

        model.fail_status = None
        assert await oracle.assess(a_context()) is not None
        assert oracle.outcomes["fetched"] == 1


class TestEmbeddingClient:
    async def test_warm_reports_success(self, model: FakeModel) -> None:
        client = EmbeddingClient(transport=httpx.MockTransport(model.handle))
        assert await client.warm() is True
        await client.aclose()

    async def test_warm_reports_failure_without_raising(self, model: FakeModel) -> None:
        model.fail_status = 503
        client = EmbeddingClient(transport=httpx.MockTransport(model.handle))
        assert await client.warm() is False
        await client.aclose()

    async def test_the_cache_is_bounded(self, model: FakeModel) -> None:
        client = EmbeddingClient(transport=httpx.MockTransport(model.handle), cache_max=2)
        for text in ("one", "two", "three"):
            await client.embed(text)
        assert client.cached("one") is None
        assert client.cached("three") is not None
        await client.aclose()

    async def test_aclose_cancels_stalled_fetches(self, model: FakeModel) -> None:
        model.stall = True
        client = EmbeddingClient(transport=httpx.MockTransport(model.handle))
        client.prefetch("hangs")
        await asyncio.sleep(0)
        await asyncio.wait_for(client.aclose(), timeout=1.0)


class TestFeatureExtractorNeverWaits:
    """Features go on the record, which is written synchronously. They read the cache only."""

    async def test_a_miss_leaves_f1_and_f2_absent_and_prefetches(self, model: FakeModel) -> None:
        client = EmbeddingClient(transport=httpx.MockTransport(model.handle))
        extractor = FeatureExtractor(client)
        context = RequestContext.model_construct(
            operation="payment:initiate",
            tool="payment_api",
            args={"payment.amount": 450000000},
            task_intent_text=TASK,
            action_intent_text=ALIGNED,
        )

        first = extractor.extract(context)
        assert first.f1 is None and first.f2 is None
        assert first.f5 is not None, "f5 needs no model and is always computed"

        await client.drain()
        second = extractor.extract(context)
        assert second.f1 is not None and second.f2 is not None
        await client.aclose()


class TestPipeline:
    async def test_a_hung_model_does_not_stall_concurrent_requests(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        """The CH-8 finding, inverted: twenty requests at once against a black hole."""
        model.stall = True
        pipeline, _, _ = await a_pipeline(drift=oracle)

        async def one(index: int) -> float:
            started = time.perf_counter()
            result = await pipeline.authorize(
                method="GET",
                path="/invoices/inv_001",
                headers=intent_headers(action=f"wire remittance batch {index} abroad"),
            )
            assert isinstance(result, Authorized), result
            return time.perf_counter() - started

        started = time.perf_counter()
        latencies = await asyncio.gather(*(one(i) for i in range(20)))
        wall = time.perf_counter() - started

        assert max(latencies) < BOUND_S + 0.15, f"slowest request {max(latencies) * 1000:.0f} ms"
        assert wall < 2 * (BOUND_S + 0.15), f"20 concurrent requests took {wall * 1000:.0f} ms"

    async def test_a_miss_is_scored_on_the_same_request_when_the_model_answers(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        pipeline, sink, _ = await a_pipeline(drift=oracle)
        emitter = pipeline._emitter
        await emitter.start()
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=intent_headers(action=ALIGNED)
        )
        assert isinstance(result, Authorized)
        await emitter.flush()
        record = sink.records[-1]
        assert isinstance(record, DecisionRecord)
        assert record.drift_score == Decimal("0.0000"), (
            "the first request on a cold cache must carry a real score, not None — "
            "otherwise drift only ever applies from the second request on"
        )

    async def test_a_high_score_escalates_on_the_first_request(
        self, oracle: RuleBasedDriftOracle
    ) -> None:
        pipeline, _, _ = await a_pipeline(drift=oracle)
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=intent_headers(action=ORTHOGONAL)
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.DRIFT_ESCALATION

    async def test_a_request_refused_before_step_6_never_reaches_the_model(
        self, oracle: RuleBasedDriftOracle, model: FakeModel
    ) -> None:
        # Scoring before `decide()` would let any token holder make the PEP embed text by
        # sending requests that are refused anyway. Drift is consulted only when steps 3-5
        # have allowed, as spec 09 orders it.
        pipeline, _, _ = await a_pipeline(drift=oracle)
        reader_only = a_mandate(
            intent_hash=hash_object(TASK_INTENT), scopes=frozenset({"invoice:read"})
        )
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=intent_headers(reader_only, action=ORTHOGONAL),
            body=b'{"amount": "10.0000", "recipient": {"account_id": "acct_1"}}',
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.SCOPE_NOT_GRANTED
        assert model.calls == []
