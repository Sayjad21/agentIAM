"""Tests for the Control Plane Cedar Authoring UI (T-027)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
    assert 'div class="alert success">Policy Activated' in response.text


@patch("agentiam_controlplane.app.compile_nl_to_policy")
def test_compile_ambiguous_policy(mock_compile: MagicMock) -> None:
    """Test that an ambiguous natural language prompt surfaces a clarifying question."""
    from agentiam_controlplane.nl_compiler.compiler import CompilerOutput

    # Setup the async mock
    async_mock = AsyncMock(
        return_value=CompilerOutput(
            clarifying_question="Who is 'someone'?",
            cedar_source=None,
            tests=[],
        )
    )
    mock_compile.side_effect = async_mock

    response = client.post("/policy/compile", data={"nl_source": "someone can do something"})
    assert response.status_code == 200
    assert 'class="alert warning"' in response.text
    assert "Who is &#39;someone&#39;?" in response.text or "Who is 'someone'?" in response.text


@patch("agentiam_controlplane.app.compile_nl_to_policy")
def test_compile_valid_policy(mock_compile: MagicMock) -> None:
    """Test that a valid NL prompt generates Cedar and evaluates both auto-tests and corpus."""
    from agentiam_controlplane.nl_compiler.compiler import CompilerOutput, CompilerTestCase

    policy = 'permit(principal == Agent::"agent-1", action, resource);'

    async_mock = AsyncMock(
        return_value=CompilerOutput(
            cedar_source=policy,
            tests=[
                CompilerTestCase(
                    name="test1",
                    description="test",
                    expected=True,
                    principal_id="admin",
                    action="do",
                    resource_type="System",
                    resource_id="1",
                )
            ],
        )
    )
    mock_compile.side_effect = async_mock

    response = client.post("/policy/compile", data={"nl_source": "Admins can do anything"})
    assert response.status_code == 200
    assert "Success:</strong> Policy compiled successfully." in response.text
    assert "Auto-Generated Tests" in response.text
    assert "Corpus Evaluation" in response.text
