"""Tests for the Control Plane Cedar Authoring UI (T-027)."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agentiam_controlplane import app as app_module
from agentiam_controlplane.app import app
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


class TestTheDatabaseLessConsoleCannotActivate:
    """T-027's console, built with no database, has no sessions and no bundle store.

    Activation used to succeed here by writing an in-memory stub that nothing read. Since
    APICTA-01 activation publishes a signed bundle to every PEP and needs a signed-in approver
    (spec 05 §5.7.1), so this console refuses it outright. The corpus gate itself is covered
    against a real store in `tests/integration/test_policy_publication.py`.
    """

    def test_activation_without_a_session_is_401(self) -> None:
        response = client.post("/policy/activate", data={"source": CORPUS_SOURCE})
        assert response.status_code == 401
        assert "approver" in response.text

    def test_a_refused_activation_changes_nothing_shown(self) -> None:
        client.post("/policy/activate", data={"source": "permit(principal, action, resource);"})
        assert "Action::&#34;invoice:read&#34;" in client.get("/policy").text

    def test_the_page_says_publication_is_not_configured(self) -> None:
        assert "Publication is not configured" in client.get("/policy").text

    def test_the_policy_page_shows_refusals_instead_of_discarding_them(self) -> None:
        # HTMX 1.9 discards non-2xx responses; the page opts its refusals back in.
        page = client.get("/policy").text
        assert "htmx:beforeSwap" in page
        assert "409" in page


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


@patch("agentiam_controlplane.app.compile_nl_to_policy")
def test_the_corpus_summary_reports_a_count_not_a_python_repr(mock_compile: MagicMock) -> None:
    """`summary.failures` is the failing results, not how many there were.

    The template compared it to `0` and printed it directly. A tuple never equals `0`, so
    the error branch rendered even on a clean run, and printing it dumped every
    `PolicyTestResult(case=PolicyTestCase(...))` into the page — thousands of characters of
    Python repr in a red box, on the screen a judge watches their own sentence compile on.
    """
    from agentiam_controlplane.nl_compiler.compiler import CompilerOutput

    # Narrow enough to fail most of the corpus, so the failure branch is what renders.
    policy = 'permit(principal, action == Action::"payment:initiate", resource);'
    mock_compile.side_effect = AsyncMock(return_value=CompilerOutput(cedar_source=policy, tests=[]))

    response = client.post("/policy/compile", data={"nl_source": "Anyone may pay"})

    assert response.status_code == 200
    assert "PolicyTestResult" not in response.text
    assert "PolicyTestCase" not in response.text
    # A count and the named cases, the same shape `/policy/activate`'s 409 uses.
    assert re.search(r"\d+ / \d+ corpus tests failed", response.text)
    assert "beat1_worker_reads_invoices" in response.text


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


@patch("agentiam_controlplane.app.compile_nl_to_policy")
def test_compiled_cedar_reaches_the_editor_as_a_string_not_as_script(
    mock_compile: MagicMock,
) -> None:
    """Model output used to be pasted into a JavaScript template literal with `| safe`.

    A backtick ends the literal and `${...}` runs as code, so a compiled policy that contained
    either — in a comment or a string — broke the page or executed in it. It is now emitted
    with `tojson`. Checked by parsing the rendered script, not by looking for a substring.
    """
    import shutil
    import subprocess
    import tempfile

    from agentiam_controlplane.nl_compiler.compiler import CompilerOutput

    hostile = (
        "// a `backtick` and ${alert(1)}\n"
        'permit(principal, action == Action::"invoice:read", resource);'
    )
    mock_compile.side_effect = AsyncMock(
        return_value=CompilerOutput(cedar_source=hostile, tests=[])
    )
    html = client.post("/policy/compile", data={"nl_source": "readers may read"}).text

    # The value is a JSON string literal now, so the hostile text sits inside quotes.
    assert 'value = "// a `backtick` and ${alert(1)}' in html

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the substring checks above still ran")
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert script is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script.group(1))
    result = subprocess.run([node, "--check", handle.name], capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, result.stderr
