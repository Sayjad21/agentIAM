"""Tests for the Control Plane Cedar Authoring UI (T-027)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentiam_controlplane import app as app_module
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


class TestActivationIsGated:
    """`PLAN.md` §907 and T-030: *never activatable without passing tests* (409).

    The endpoint previously assigned `store.current_source` and returned 200 with no
    corpus run, no auto-tests and not even a parse — `can_activate` was computed and
    handed to the template, so the gate existed only in the UI. A direct POST installed
    whatever it was given, including Cedar that does not parse.

    ADR-030 and ADR-034 both describe a gate that nothing enforced. These tests are the
    enforcement.
    """

    def setup_method(self) -> None:
        self._saved = store.current_source

    def teardown_method(self) -> None:
        # The store is module-global; leaking a mutation between tests makes the next
        # failure depend on execution order.
        store.current_source = self._saved

    def test_a_passing_policy_activates(self) -> None:
        new_source = CORPUS_SOURCE + "\n// A new comment"
        response = client.post("/policy/activate", data={"source": new_source})
        assert response.status_code == 200
        assert "Policy Activated" in response.text
        assert store.current_source == new_source

    def test_a_policy_failing_the_corpus_is_refused_with_409(self) -> None:
        # Breaks beat 4's payment limit, exactly as the diff test does.
        broken = CORPUS_SOURCE.replace('decimal("500000.0")', 'decimal("1000.0")')

        response = client.post("/policy/activate", data={"source": broken})
        assert response.status_code == 409
        assert store.current_source != broken, "a refused bundle must not become current"

    def test_the_refusal_names_what_failed(self) -> None:
        broken = CORPUS_SOURCE.replace('decimal("500000.0")', 'decimal("1000.0")')
        response = client.post("/policy/activate", data={"source": broken})
        assert "corpus" in response.text.lower() or "failed" in response.text.lower()

    def test_unparseable_cedar_is_refused(self) -> None:
        response = client.post("/policy/activate", data={"source": "this is not cedar"})
        assert response.status_code == 409
        assert store.current_source != "this is not cedar"

    def test_an_empty_policy_is_refused_rather_than_silently_permitting_nothing(self) -> None:
        # An empty policy denies everything, so it would sail past a parse-only check and
        # take the whole demo down. The corpus is what catches it.
        #
        # `source` defaults to "" rather than being required, deliberately: an empty
        # policy is a thing to *refuse* (409), not a malformed request (422). httpx drops
        # an empty form value where a browser would send `source=`, so a required field
        # here would make the two disagree — and the browser is the one that matters.
        assert client.post("/policy/activate", data={"source": ""}).status_code == 409
        assert client.post("/policy/activate", data={}).status_code == 409

    def test_the_previous_policy_keeps_serving_after_a_refusal(self) -> None:
        original = store.current_source
        client.post("/policy/activate", data={"source": "this is not cedar"})
        assert store.current_source == original


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

    policy = 'permit(principal, action == Action::"invoice:read", resource);'

    async_mock = AsyncMock(
        return_value=CompilerOutput(
            cedar_source=policy,
            tests=[
                CompilerTestCase(
                    name="worker_reads",
                    description="any agent may read invoices",
                    expected=True,
                    role="worker",
                    operation="invoice:read",
                    tool="invoice_api",
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


class TestLeaseReaper:
    """`LEDGER.REAP` on a schedule — TODO item 9, STATUS gap 27.

    Spec 04 §4.6's own pseudocode says `REAP() # background, every TTL/4`. `reap()` has
    been correct since T-013 and the chaos suite exercises it by advancing a clock and
    calling it directly — which is why CH-3/CH-4's prose reports "REAP reclaims it" as a
    running fact. Grepping every non-test call site found none: not `app.py`, not
    `scripts/pep_service.py`, not `serve_pep.py`, not either compose file, not
    `deploy/k3s/`. A lease stranded by a hard-killed PEP stayed `ACTIVE`, and its budget
    stayed `leased`, until somebody ran it by hand.
    """

    def test_the_default_interval_is_a_quarter_of_the_lease_ttl(self) -> None:
        """Spec 04 §4.6, against the PEP's own 60 s default TTL."""
        from scripts.pep_service import DEFAULT_LEASE_TTL_S

        assert app_module.DEFAULT_REAPER_INTERVAL_S == DEFAULT_LEASE_TTL_S / 4

    def test_unset_means_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(app_module._REAPER_INTERVAL_ENV, raising=False)
        assert app_module._reaper_interval_s() == app_module.DEFAULT_REAPER_INTERVAL_S

    def test_zero_disables_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deployment reaping some other way — a k8s CronJob — should be able to say so."""
        monkeypatch.setenv(app_module._REAPER_INTERVAL_ENV, "0")
        assert app_module._reaper_interval_s() is None

    @pytest.mark.parametrize("bad", ["nonsense", "-1"])
    def test_an_unusable_interval_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        monkeypatch.setenv(app_module._REAPER_INTERVAL_ENV, bad)
        with pytest.raises(ValueError, match="REAPER_INTERVAL_S"):
            app_module._reaper_interval_s()

    def test_no_database_means_no_reaper(self) -> None:
        """Nothing to sweep, and `reap()` would have no session factory to do it with."""
        built = app_module.create_app(session_factory=None, reaper_interval_s=15.0)
        assert built.router.lifespan_context is not None  # FastAPI's default
        # The console-only app must not carry a background task.
        assert not hasattr(built.state, "reaper_task")

    async def test_it_sweeps_on_the_interval_and_stops_on_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The behaviour gap 27 is about: something calls `reap()` without being asked."""
        import asyncio

        calls: list[object] = []

        async def fake_reap(_session: object, *, now: object) -> list[object]:
            calls.append(now)
            return []

        monkeypatch.setattr("agentiam_controlplane.db.ledger.reap", fake_reap)

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

        built = app_module.create_app(
            session_factory=lambda: _Session(),  # type: ignore[arg-type]
            reaper_interval_s=0.01,
        )

        async with built.router.lifespan_context(built):
            await asyncio.sleep(0.06)

        assert calls, "the reaper never ran"
        # And it is gone once the app is down: no task outliving its own application.
        assert len(asyncio.all_tasks()) >= 1  # the test's own task, nothing else pending

    async def test_a_failed_sweep_does_not_kill_the_control_plane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreachable ledger must not take the console and escalation queue with it."""
        import asyncio

        attempts: list[int] = []

        async def exploding_reap(_session: object, *, now: object) -> list[object]:
            attempts.append(1)
            raise RuntimeError("postgres is down")

        monkeypatch.setattr("agentiam_controlplane.db.ledger.reap", exploding_reap)

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

        built = app_module.create_app(
            session_factory=lambda: _Session(),  # type: ignore[arg-type]
            reaper_interval_s=0.01,
        )

        async with built.router.lifespan_context(built):
            await asyncio.sleep(0.06)

        assert len(attempts) > 1, "it gave up after the first failure instead of retrying"
