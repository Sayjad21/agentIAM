"""AgentIAM Control Plane Web Application.

Provides the Cedar Authoring UI (T-027).
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import cedarpy
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from agentiam_controlplane.auth import build_router as build_auth_router
from agentiam_controlplane.budgets_api import build_router as build_budgets_router
from agentiam_controlplane.db.escalations import list_by_state
from agentiam_controlplane.decisions_api import build_router as build_decisions_router
from agentiam_controlplane.escalations_api import build_router as build_escalations_router
from agentiam_controlplane.nl_compiler.compiler import compile_nl_to_policy
from agentiam_controlplane.revocations_api import build_router as build_revocations_router
from agentiam_controlplane.tree_api import build_router as build_tree_router
from agentiam_core.corpus import CORPUS, CORPUS_SOURCE, CORPUS_TOOLS
from agentiam_core.decision import PolicyVerdict
from agentiam_core.escalation import EscalationState
from agentiam_core.hashing import DECIMAL_PLACES
from agentiam_core.policy_testing import (
    PolicyTestCase,
    run_policy_tests,
    summarize,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from agentiam_controlplane.db.revocations import RevocationPublisher
    from agentiam_controlplane.settings import ControlPlaneSettings, OIDCSettings

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
            "elevated": case.elevated,
            "environment": case.environment,
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


def _refuse_activation(detail: str) -> HTMLResponse:
    """A refused activation — 409, and the current policy untouched.

    409 rather than 400 or 403: the request is well-formed and the caller is permitted;
    what fails is the *state transition*. ADR-030 sets this out, and spec 05 §5.5 fixes
    the same status for the PEP-side gate, so the two surfaces answer alike.
    """
    return HTMLResponse(content=f'<div class="alert danger">{detail}</div>', status_code=409)


def create_app(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    escalation_settings: ControlPlaneSettings | None = None,
    oidc_settings: OIDCSettings | None = None,
    revocation_publisher: RevocationPublisher | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Create the FastAPI application for the Control Plane.

    The escalation queue (`/v1/escalations`, `GET /escalations`) and the revocation API
    (`/v1/revocations`) are mounted only when both `session_factory` and `escalation_settings`
    are supplied — no database has ever been reachable from this app before T-037, and a
    console built for a demo without one should keep working exactly as before rather than
    fail to start. `escalation_settings` is shared between the two: T-038's revoke endpoint
    reuses its `approvers` set (ADR-041's stopgap) rather than inventing a second one.

    Whenever those two are supplied, `SessionMiddleware` is installed too (keyed by
    `escalation_settings.session_secret_key`) — `POST /v1/escalations/.../approve` and
    `.../deny` require a real session unconditionally as of T-043 (ADR-046), independent of
    whether login itself is wired up. `oidc_settings` is what wires login up: supplying it
    additionally mounts `/auth/login`, `/auth/callback`, `/auth/logout` against a real
    Keycloak realm. Without it, the escalation routes still demand a session — a test (or an
    operator) has to produce one some other way, e.g. a signed cookie built directly with the
    same secret — the same "wired means it does the thing, unwired means it visibly doesn't"
    shape as the PEP's `enforcing` flag.

    `revocation_publisher` is independently optional (spec 07 §5.2): a `None` publisher still
    lets `POST /v1/revocations` persist and `GET /v1/revocations` serve pulls — a deployment
    without Redis wired up is correct, only slower.
    """
    app = FastAPI(title="AgentIAM Control Plane")

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    if session_factory is not None and escalation_settings is not None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=escalation_settings.session_secret_key,
            same_site="lax",
        )
        app.include_router(
            build_escalations_router(
                session_factory=session_factory, settings=escalation_settings, now=now
            )
        )
        app.include_router(
            build_revocations_router(
                session_factory=session_factory,
                settings=escalation_settings,
                publisher=revocation_publisher,
                now=now,
            )
        )
        if oidc_settings is not None:
            app.include_router(build_auth_router(settings=oidc_settings))

    app.include_router(build_tree_router(session_factory=session_factory, now=now))

    # T-046. Mounted unconditionally, like the tree: both routes answer 503 without a
    # database, which is visibly "not wired" rather than a boot failure.
    app.include_router(build_decisions_router(session_factory=session_factory))

    # T-047, same shape: 503 without a database rather than a boot failure.
    app.include_router(build_budgets_router(session_factory=session_factory, now=now))

    @app.get("/budgets", response_class=HTMLResponse)
    async def budgets_console(request: Request) -> HTMLResponse:
        """The budget and lease dashboard — T-047's console surface."""
        return templates.TemplateResponse(
            request=request,
            name="budgets.html",
            context={"has_database": session_factory is not None},
        )

    @app.get("/decisions", response_class=HTMLResponse)
    async def decisions_console(
        request: Request,
        outcome: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        scope: str | None = Query(default=None),
    ) -> HTMLResponse:
        """The live decision stream — T-046's console surface.

        The filters are read here only to seed the form and the initial `EventSource` URL;
        the filtering itself happens in SQL (`db/decisions.py`), because the audit chain
        grows without bound.
        """
        return templates.TemplateResponse(
            request=request,
            name="decisions.html",
            context={
                "outcome": outcome or "",
                "agent_id": agent_id or "",
                "scope": scope or "",
                "streaming": session_factory is not None,
            },
        )

    @app.get("/identity-tree", response_class=HTMLResponse)
    async def identity_tree(
        request: Request, task_id: str | None = Query(default=None)
    ) -> HTMLResponse:
        """The agent delegation tree visualizer — T-045's console acceptance criterion."""
        return templates.TemplateResponse(
            request=request,
            name="identity_tree.html",
            context={"task_id": task_id},
        )

    @app.get("/escalations", response_class=HTMLResponse)
    async def escalation_queue(
        request: Request, state: str = Query(default="pending")
    ) -> HTMLResponse:
        """The pending-approval queue — T-037's console acceptance criterion.

        Approve/deny still post straight to the JSON API; T-050 owns the richer inline
        screen. Viewing the queue stays open to anyone who can reach the console (T-037's
        original scope) — only the approve/deny *actions* require a session (T-043).
        """
        pending: list[dict[str, object]] = []
        error: str | None = None
        principal_id: str | None = None
        display_name: str | None = None
        if session_factory is None:
            error = "the escalation queue has no database configured"
        else:
            principal_id = request.session.get("principal_id")
            display_name = request.session.get("display_name")
            try:
                parsed_state = EscalationState(state)
            except ValueError:
                parsed_state = EscalationState.PENDING
            instant = now()
            async with session_factory() as session:
                found = await list_by_state(session, state=parsed_state, now=instant)
            pending = [
                {
                    "id": str(e.id),
                    "agent_id": e.agent_id,
                    "principal_id": e.principal_id,
                    "requested_scopes": sorted(e.requested_scopes),
                    "requested_amount": str(e.requested_amount),
                    "reason": e.reason,
                    "expires_at": e.expires_at.isoformat(),
                }
                for e in found
            ]
        return templates.TemplateResponse(
            request=request,
            name="escalations.html",
            context={
                "pending": pending,
                "error": error,
                "state": state,
                "principal_id": principal_id,
                "display_name": display_name,
                "login_wired": oidc_settings is not None,
            },
        )

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

        # 1. Evaluate the auto-generated tests — through the *same* evaluator the corpus
        #    and the activation gate use. They previously ran against an empty entity
        #    list, so they could only turn on guessed ids rather than on the policy being
        #    right (STATUS gap 19).
        auto_test_results = []
        auto_tests_passed = True

        for t in output.tests:
            try:
                verdict = evaluate_case(candidate_engine, t.as_policy_test_case())
                passed = verdict.allowed == t.expected
                if not passed:
                    auto_tests_passed = False
                auto_test_results.append({"test": t, "allowed": verdict.allowed, "passed": passed})
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
        for test_res in candidate_results:
            current_verdict = eval_current(test_res.case)
            if current_verdict is None:
                changed = True
            else:
                changed = current_verdict.allowed != test_res.actual

            diffs.append(
                {
                    "case": test_res.case,
                    "passed": test_res.passed,
                    "candidate_allowed": test_res.actual,
                    "current_allowed": current_verdict.allowed if current_verdict else None,
                    "changed": changed,
                }
            )

        corpus_passed = len(summary.failures) == 0

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
    async def activate_policy(request: Request, source: str = Form("")) -> HTMLResponse:
        """Commit the new policy bundle — but only if it earns it.

        `PLAN.md` §907 and T-030's acceptance criterion both say a policy is *never*
        activatable while a test fails, and ADR-030 fixes the status as 409. This endpoint
        previously assigned the source and returned 200 unconditionally: `can_activate` was
        computed for the template, so the gate lived in the UI and any direct POST walked
        straight past it — unparseable Cedar included.

        The gate here mirrors `agentiam_pep.activation.activate_bundle`: parse, then the
        full corpus, and the previous policy keeps serving on refusal (spec 05 §5.5). It
        cannot *call* that function yet, because that path also verifies an Ed25519
        signature and a monotonic serial, and the console has no bundle signing — the store
        is still a stub. Those two gates arrive with real bundle publication; the corpus
        gate is the one that has something to check today, and it is the one that was
        missing.
        """
        try:
            candidate_engine = cedarpy.PolicySet.from_str(source)
        except Exception as exc:
            return _refuse_activation(f"Generated Cedar does not parse: {exc}")

        def eval_candidate(case: PolicyTestCase) -> PolicyVerdict:
            return evaluate_case(candidate_engine, case)

        summary = summarize(run_policy_tests(CORPUS, eval_candidate))
        if not summary.all_passed:
            failed = ", ".join(r.case.name for r in summary.failures[:5])
            more = "" if summary.failed <= 5 else f" (and {summary.failed - 5} more)"
            return _refuse_activation(
                f"Refused: {summary.failed} of {summary.total} corpus tests failed "
                f"&mdash; {failed}{more}. The previous policy is still serving."
            )

        store.current_source = source
        return HTMLResponse(
            content=(
                f'<div class="alert success">Policy Activated Successfully! '
                f"{summary.passed}/{summary.total} corpus tests passed.</div>"
            ),
            status_code=200,
        )

    return app


app = create_app()
