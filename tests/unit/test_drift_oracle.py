"""The v0 drift score — spec 06 §2, T-032.

The scoring rules, against a fake model. How the score is fetched without blocking is
`test_drift_async.py`'s subject; this module is about the numbers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest

from agentiam_core.models import RequestContext
from agentiam_pep.drift import EmbeddingClient, RuleBasedDriftOracle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


#: Builds an oracle over a fake model; the fixture closes every client it built.
OracleFactory = Callable[[httpx.MockTransport], RuleBasedDriftOracle]


def a_context(task: str | None, action: str | None) -> RequestContext:
    return RequestContext.model_construct(task_intent_text=task, action_intent_text=action)


def model_answering(vectors: dict[str, list[float]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"embedding": vectors[json.loads(request.content)["prompt"]]}
        )

    return httpx.MockTransport(handle)


def model_failing(make: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(make)


@pytest.fixture
async def oracle_for() -> AsyncIterator[OracleFactory]:
    clients: list[EmbeddingClient] = []

    def build(transport: httpx.MockTransport) -> RuleBasedDriftOracle:
        client = EmbeddingClient(transport=transport)
        clients.append(client)
        return RuleBasedDriftOracle(embeddings=client, assess_timeout_s=1.0)

    yield build
    for client in clients:
        await client.aclose()


async def test_score_is_zero_for_exact_match(oracle_for: OracleFactory) -> None:
    oracle = oracle_for(model_answering({}))
    text = "Transfer 500 units to vendor A"
    assert await oracle.assess(a_context(text, text)) == Decimal("0.0")


async def test_score_is_low_for_subset_match(oracle_for: OracleFactory) -> None:
    oracle = oracle_for(model_answering({}))
    context = a_context(
        "Transfer 500 units to vendor A for office supplies", "Transfer 500 units to vendor A"
    )
    assert await oracle.assess(context) == Decimal("0.1")


async def test_score_is_high_for_orthogonal_intent(oracle_for: OracleFactory) -> None:
    task, action = "Transfer 500 units to vendor A", "Delete production database"
    oracle = oracle_for(model_answering({task: [1.0, 0.0, 0.0], action: [0.0, 1.0, 0.0]}))
    score = await oracle.assess(a_context(task, action))
    assert score is not None
    assert score >= Decimal("0.7")


async def test_score_scales_linearly_with_similarity(oracle_for: OracleFactory) -> None:
    task, action = "Transfer 500 units to vendor A", "Transfer 250 units"
    oracle = oracle_for(model_answering({task: [1.0, 0.0], action: [0.65, 0.759934]}))
    score = await oracle.assess(a_context(task, action))
    assert score is not None
    assert Decimal("0.49") <= score <= Decimal("0.51")


async def test_fails_open_if_oracle_is_down(oracle_for: OracleFactory) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    oracle = oracle_for(model_failing(refuse))
    context = a_context("Transfer 500 units to vendor A", "Delete production database")
    assert await oracle.assess(context) is None
    assert oracle.outcomes["error"] == 1


async def test_fails_open_if_oracle_returns_error(oracle_for: OracleFactory) -> None:
    oracle = oracle_for(model_failing(lambda _request: httpx.Response(500)))
    context = a_context("Transfer 500 units to vendor A", "Delete production database")
    assert await oracle.assess(context) is None
    assert oracle.outcomes["error"] == 1


async def test_fails_open_if_missing_intent_text(oracle_for: OracleFactory) -> None:
    oracle = oracle_for(model_answering({}))
    assert await oracle.assess(a_context(None, "Transfer 500 units to vendor A")) is None
    assert await oracle.assess(a_context("Transfer 500 units to vendor A", None)) is None
