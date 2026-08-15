"""Tests for the Control Plane Cedar Authoring UI (T-027)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentiam_controlplane.app import app, store
from agentiam_core.corpus import CORPUS_SOURCE

client = TestClient(app)


def test_get_policy_editor_renders() -> None:
    """The /policy route should return HTML with the editor."""
    response = client.get("/policy")
    assert response.status_code == 200
    assert "Cedar Policy Authoring" in response.text
    assert "Action::&#34;invoice:read&#34;" in response.text


def test_test_policy_endpoint_evaluates_corpus() -> None:
    """The /policy/test route should evaluate the corpus and return the results."""
    # Given the standard passing source
    response = client.post("/policy/test", data={"source": CORPUS_SOURCE})
    assert response.status_code == 200

    # All tests should pass
    assert "51 / 51" in response.text or "All tests passed" in response.text
    assert 'class="alert danger"' not in response.text
    assert "Activate Policy" in response.text


def test_test_policy_endpoint_shows_diff_on_changes() -> None:
    """Modifying a policy should highlight the diffs in the table."""
    # A modified source that breaks beat 4 (payment limit)
    modified_source = CORPUS_SOURCE.replace('decimal("500000.0")', 'decimal("1000.0")')

    response = client.post("/policy/test", data={"source": modified_source})
    assert response.status_code == 200

    # Some tests will fail now
    assert "Failures" in response.text
    assert "row-fail" in response.text
    assert "Activation disabled" in response.text


def test_test_policy_endpoint_handles_parse_errors() -> None:
    """Invalid Cedar source should return a parse error gracefully."""
    response = client.post("/policy/test", data={"source": "this is not cedar"})
    assert response.status_code == 200

    assert "Policy Bundle Error" in response.text
    assert "unexpected token `is`" in response.text or "parse error" in response.text.lower()


def test_activate_policy_endpoint_updates_store() -> None:
    """The /policy/activate endpoint should accept the new policy."""
    old_source = store.current_source
    new_source = old_source + "\n// A new comment"

    response = client.post("/policy/activate", data={"source": new_source})
    assert response.status_code == 200
    assert "Policy Activated Successfully" in response.text

    assert store.current_source == new_source

    # Restore the store
    store.current_source = old_source
