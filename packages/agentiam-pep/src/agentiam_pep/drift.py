"""Rule-based drift detection using semantic embeddings (T-032)."""

import json
import logging
import math
from decimal import Decimal
from functools import lru_cache

import httpx

from agentiam_core.decision import DriftOracle, OracleUnavailable
from agentiam_core.models import RequestContext

logger = logging.getLogger(__name__)

# A score above this threshold in v0 means high divergence (drift escalation).
_DRIFT_THRESHOLD = Decimal("0.7")
# If cosine similarity is below this, the actions are semantically orthogonal.
_SIMILARITY_THRESHOLD = 0.3


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(a * b for a, b in zip(v1, v2, strict=True))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


class RuleBasedDriftOracle(DriftOracle):
    """A deterministic drift oracle using a local embedding model.

    Queries a local Ollama instance (default: nomic-embed-text) to compute semantic
    similarity between the approved task intent and the requested action intent.
    If they diverge significantly, returns a high drift score.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout: float = 2.0,
    ) -> None:
        """Initialize the local embedding client.

        The short 2.0s timeout ensures that a missing Ollama daemon fails open quickly,
        respecting NFR-1 for valid traffic and the "drift never blocks" invariant (spec 09).
        """
        self._base_url = base_url
        self._model = model
        self._timeout = timeout

    @lru_cache(maxsize=1024)  # noqa: B019
    def _compute_drift_score(self, task_intent: str, action_intent: str) -> Decimal:
        """Fetch embeddings and compute the drift score. Cached to stay within NFR-1."""
        if task_intent == action_intent:
            return Decimal("0.0")

        # Basic keyword heuristic: if the action intent text is a pure subset of the task,
        # it's highly likely to be aligned.
        task_words = set(task_intent.lower().split())
        action_words = set(action_intent.lower().split())
        if action_words and action_words.issubset(task_words):
            return Decimal("0.1")

        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout) as client:
                res1 = client.post(
                    "/api/embeddings", json={"model": self._model, "prompt": task_intent}
                )
                res1.raise_for_status()
                emb1 = res1.json().get("embedding")

                res2 = client.post(
                    "/api/embeddings", json={"model": self._model, "prompt": action_intent}
                )
                res2.raise_for_status()
                emb2 = res2.json().get("embedding")
        except httpx.RequestError as exc:
            logger.warning("Drift oracle unavailable (network error): %s", exc)
            raise OracleUnavailable("Network error") from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("Drift oracle unavailable (HTTP %s)", exc.response.status_code)
            raise OracleUnavailable("HTTP error") from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Drift oracle unavailable (bad response): %s", exc)
            raise OracleUnavailable("Bad response") from exc

        if not emb1 or not emb2:
            raise OracleUnavailable("Ollama returned empty embeddings")

        similarity = cosine_similarity(emb1, emb2)
        if similarity < _SIMILARITY_THRESHOLD:
            # Orthogonal intents -> High drift
            return _DRIFT_THRESHOLD + Decimal("0.1")

        # Linear scale between SIMILARITY_THRESHOLD and 1.0 mapping to drift 0.7 -> 0.0
        # similarity=1.0 -> drift=0.0
        # similarity=0.3 -> drift=0.7
        drift_val = 1.0 - ((similarity - _SIMILARITY_THRESHOLD) / (1.0 - _SIMILARITY_THRESHOLD))
        # Ensure it tops out at 0.7 so it doesn't cross threshold unless it's strictly < 0.3
        drift_val = min(0.7, max(0.0, drift_val))

        # Round to 3 decimal places to keep logs clean
        return Decimal(str(round(drift_val, 3)))

    def score_for(self, context: RequestContext) -> Decimal | None:
        """Compute the drift score for the current request.

        If either intent is missing, drift detection is impossible, so we fail open
        by returning None.
        """
        if not context.task_intent_text or not context.action_intent_text:
            return None

        return self._compute_drift_score(context.task_intent_text, context.action_intent_text)
