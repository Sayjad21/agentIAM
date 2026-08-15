import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentiam_core.decision import OracleUnavailable
from agentiam_core.models import RequestContext
from agentiam_pep.drift import RuleBasedDriftOracle


@pytest.fixture
def drift_oracle() -> RuleBasedDriftOracle:
    return RuleBasedDriftOracle(timeout=0.1)


def test_score_is_zero_for_exact_match(drift_oracle: RuleBasedDriftOracle) -> None:
    context = RequestContext.model_construct(
        task_intent_text="Transfer 500 units to vendor A",
        action_intent_text="Transfer 500 units to vendor A",
    )
    score = drift_oracle.score_for(context)
    assert score == Decimal("0.0")


def test_score_is_low_for_subset_match(drift_oracle: RuleBasedDriftOracle) -> None:
    context = RequestContext.model_construct(
        task_intent_text="Transfer 500 units to vendor A for office supplies",
        action_intent_text="Transfer 500 units to vendor A",
    )
    score = drift_oracle.score_for(context)
    assert score == Decimal("0.1")


@patch("agentiam_pep.drift.httpx.Client.post")
def test_score_is_high_for_orthogonal_intent(mock_post: MagicMock, drift_oracle: RuleBasedDriftOracle) -> None:
    context = RequestContext.model_construct(
        task_intent_text="Transfer 500 units to vendor A",
        action_intent_text="Delete production database",
    )
    
    mock_post.side_effect = [
        MagicMock(status_code=200, json=lambda: {"embedding": [1.0, 0.0, 0.0]}),
        MagicMock(status_code=200, json=lambda: {"embedding": [0.0, 1.0, 0.0]}),
    ]
    
    score = drift_oracle.score_for(context)
    assert score is not None
    assert score >= Decimal("0.7")


@patch("agentiam_pep.drift.httpx.Client.post")
def test_score_scales_linearly_with_similarity(mock_post: MagicMock, drift_oracle: RuleBasedDriftOracle) -> None:
    context = RequestContext.model_construct(
        task_intent_text="Transfer 500 units to vendor A",
        action_intent_text="Transfer 250 units",
    )
    
    mock_post.side_effect = [
        MagicMock(status_code=200, json=lambda: {"embedding": [1.0, 0.0]}),
        MagicMock(status_code=200, json=lambda: {"embedding": [0.65, 0.759934]}),
    ]
    
    score = drift_oracle.score_for(context)
    assert score is not None
    assert Decimal("0.49") <= score <= Decimal("0.51")


@patch("agentiam_pep.drift.httpx.Client.post")
def test_fails_open_if_oracle_is_down(mock_post: MagicMock, drift_oracle: RuleBasedDriftOracle) -> None:
    context = RequestContext.model_construct(
        task_intent_text="Transfer 500 units to vendor A",
        action_intent_text="Delete production database",
    )
    mock_post.side_effect = httpx.RequestError("Connection refused")
    
    with pytest.raises(OracleUnavailable, match="Network error"):
        drift_oracle.score_for(context)


@patch("agentiam_pep.drift.httpx.Client.post")
def test_fails_open_if_oracle_returns_error(mock_post: MagicMock, drift_oracle: RuleBasedDriftOracle) -> None:
    context = RequestContext.model_construct(
        task_intent_text="Transfer 500 units to vendor A",
        action_intent_text="Delete production database",
    )
    response = MagicMock(status_code=500)
    mock_post.side_effect = httpx.HTTPStatusError("Server error", request=MagicMock(), response=response)
    
    with pytest.raises(OracleUnavailable, match="HTTP error"):
        drift_oracle.score_for(context)


def test_fails_open_if_missing_intent_text(drift_oracle: RuleBasedDriftOracle) -> None:
    context1 = RequestContext.model_construct(
        task_intent_text=None,
        action_intent_text="Transfer 500 units to vendor A",
    )
    assert drift_oracle.score_for(context1) is None
    
    context2 = RequestContext.model_construct(
        task_intent_text="Transfer 500 units to vendor A",
        action_intent_text=None,
    )
    assert drift_oracle.score_for(context2) is None
