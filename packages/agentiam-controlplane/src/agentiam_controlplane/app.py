"""AgentIAM Control Plane Web Application.

Provides the Cedar Authoring UI (T-027).
"""

from __future__ import annotations

import pathlib
from decimal import Decimal

import cedarpy
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agentiam_controlplane.nl_compiler.compiler import compile_nl_to_policy
from agentiam_core.corpus import CORPUS, CORPUS_SOURCE, CORPUS_TOOLS
from agentiam_core.decision import PolicyVerdict
from agentiam_core.hashing import DECIMAL_PLACES
from agentiam_core.policy_testing import (
    PolicyTestCase,
    run_policy_tests,
    summarize,
)

# Set up paths for templates and static assets
BASE_DIR = pathlib.Path(__file__).parent / "console"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# A mock for the "current" bundle in the database.
class DummyBundleStore:
    """Mock for the 'current' bundle in the database."""

    def __init__(self) -> None:
        """Initialize the store with the default corpus source."""
        self.current_source = CORPUS_SOURCE


store = DummyBundleStore()

_QUANTUM = Decimal(1).scaleb(-DECIMAL_PLACES)


def _as_cedar_decimal(value: Decimal) -> dict[str, dict[str, str]]:
    """Render money as Cedar's decimal extension value, at exactly four places."""
    return {"__extn": {"fn": "decimal", "arg": f"{value.quantize(_QUANTUM):f}"}}


def evaluate_case(engine: cedarpy.PolicySet, case: PolicyTestCase) -> PolicyVerdict:
    """Evaluate one case using Cedar."""
    tool = CORPUS_TOOLS.get(case.tool or "")
    if tool is None:
        tool_facts = {"tool_id": "", "server": "", "sensitivity": "low", "is_external": False}
    else:
        tool_facts = {
            "tool_id": str(tool.get("tool_id", "")),
            "server": str(tool.get("server", "")),
            "sensitivity": str(tool.get("sensitivity", "low")),
            "is_external": bool(tool.get("is_external", False)),
        }

    entities = [
        {
            "uid": {"type": "Agent", "id": "agent-1"},
            "attrs": {
                "role": case.role,
                "depth": case.depth,
                "task_id": "00000000-0000-0000-0000-000000000000",
                "principal_id": "test-principal",
            },
            "parents": [],
        },
        {
            "uid": {"type": "Tool", "id": case.tool or ""},
            "attrs": tool_facts,
            "parents": [],
        },
    ]

    request = {
        "principal": 'Agent::"agent-1"',
        "action": f'Action::"{case.operation}"',
        "resource": f'Tool::"{case.tool or ""}"',
        "context": {
            "amount": _as_cedar_decimal(case.amount),
            "arg_digest": "",
            "elevated": False,
            "environment": "production",
        },
    }

    response = cedarpy.is_authorized(request, engine, entities)
    allowed = response.decision is cedarpy.Decision.Allow
    diagnostics = getattr(response, "diagnostics", None)
    reasons = list(getattr(diagnostics, "reasons", None) or [])

    return PolicyVerdict(
        allowed=allowed,
        statement=reasons[0] if reasons else None,
    )


def create_app() -> FastAPI:
    """Create the FastAPI application for the Control Plane."""
    app = FastAPI(title="AgentIAM Control Plane")

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/policy", response_class=HTMLResponse)
    async def get_policy_editor(request: Request) -> HTMLResponse:
        """Render the main Cedar authoring UI."""
        return templates.TemplateResponse(
            request=request,
            name="authoring.html",
            context={
                "source": store.current_source,
            },
        )

    @app.post("/policy/test", response_class=HTMLResponse)
    async def test_policy(request: Request, source: str = Form(...)) -> HTMLResponse:
        """Evaluate the provided source against the test corpus and render the diff view."""
        try:
            candidate_engine = cedarpy.PolicySet.from_str(source)
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="authoring_results.html",
                context={
                    "error": str(exc),
                    "summary": None,
                    "diffs": None,
                },
            )

        try:
            current_engine = cedarpy.PolicySet.from_str(store.current_source)
        except Exception:
            current_engine = None

        def eval_candidate(case: PolicyTestCase) -> PolicyVerdict:
            return evaluate_case(candidate_engine, case)

        def eval_current(case: PolicyTestCase) -> PolicyVerdict | None:
            if current_engine is None:
                return None
            return evaluate_case(current_engine, case)

        candidate_results = run_policy_tests(CORPUS, eval_candidate)
        summary = summarize(candidate_results)

        # Calculate diffs against current bundle
        diffs = []
        for res in candidate_results:
            current_verdict = eval_current(res.case)
            if current_verdict is None:
                changed = True
            else:
                changed = current_verdict.allowed != res.actual

            diffs.append(
                {
                    "case": res.case,
                    "passed": res.passed,
                    "candidate_allowed": res.actual,
                    "current_allowed": current_verdict.allowed if current_verdict else None,
                    "changed": changed,
                }
            )

        return templates.TemplateResponse(
            request=request,
            name="authoring_results.html",
            context={
                "error": None,
                "summary": summary,
                "diffs": diffs,
            },
        )

    @app.post("/policy/compile", response_class=HTMLResponse)
    async def compile_policy(request: Request, nl_source: str = Form(...)) -> HTMLResponse:
        """Compile a natural language statement into Cedar policy and evaluate."""
        try:
            output = await compile_nl_to_policy(nl_source)
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="nl_results.html",
                context={"error": str(exc)},
            )

        if output.clarifying_question:
            return templates.TemplateResponse(
                request=request,
                name="nl_results.html",
                context={"clarifying_question": output.clarifying_question},
            )

        # We have a valid generated policy, let's test it!
        cedar_source = output.cedar_source or ""

        try:
            candidate_engine = cedarpy.PolicySet.from_str(cedar_source)
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="nl_results.html",
                context={"error": f"Generated Cedar is invalid: {exc}"},
            )

        # 1. Evaluate auto-generated tests
        auto_test_results = []
        auto_tests_passed = True

        for t in output.tests:
            req = {
                "principal": f'User::"{t.principal_id}"',  # Simplification for generated tests
                "action": f'Action::"{t.action}"',
                "resource": f'{t.resource_type}::"{t.resource_id}"',
                "context": {},
            }
            try:
                res = cedarpy.is_authorized(req, candidate_engine, [])
                allowed = res.decision is cedarpy.Decision.Allow
                passed = allowed == t.expected
                if not passed:
                    auto_tests_passed = False
                auto_test_results.append({"test": t, "allowed": allowed, "passed": passed})
            except Exception as exc:
                auto_tests_passed = False
                auto_test_results.append({"test": t, "error": str(exc), "passed": False})

        # 2. Evaluate master corpus
        try:
            current_engine = cedarpy.PolicySet.from_str(store.current_source)
        except Exception:
            current_engine = None

        def eval_candidate(case: PolicyTestCase) -> PolicyVerdict:
            return evaluate_case(candidate_engine, case)

        def eval_current(case: PolicyTestCase) -> PolicyVerdict | None:
            if current_engine is None:
                return None
            return evaluate_case(current_engine, case)

        candidate_results = run_policy_tests(CORPUS, eval_candidate)
        summary = summarize(candidate_results)

        diffs = []
        for res in candidate_results:
            current_verdict = eval_current(res.case)
            if current_verdict is None:
                changed = True
            else:
                changed = current_verdict.allowed != res.actual

            diffs.append(
                {
                    "case": res.case,
                    "passed": res.passed,
                    "candidate_allowed": res.actual,
                    "current_allowed": current_verdict.allowed if current_verdict else None,
                    "changed": changed,
                }
            )

        corpus_passed = summary.failures == 0

        return templates.TemplateResponse(
            request=request,
            name="nl_results.html",
            context={
                "error": None,
                "cedar_source": cedar_source,
                "auto_test_results": auto_test_results,
                "auto_tests_passed": auto_tests_passed,
                "summary": summary,
                "diffs": diffs,
                "corpus_passed": corpus_passed,
                "can_activate": auto_tests_passed and corpus_passed,
            },
        )

    @app.post("/policy/activate", response_class=HTMLResponse)
    async def activate_policy(request: Request, source: str = Form(...)) -> HTMLResponse:
        """Sign and commit the new policy bundle."""
        store.current_source = source
        return HTMLResponse(
            content='<div class="alert success">Policy Activated Successfully!</div>',
            status_code=200,
        )

    return app


app = create_app()
