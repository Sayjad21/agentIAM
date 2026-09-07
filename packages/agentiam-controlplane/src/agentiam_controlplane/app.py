"""AgentIAM Control Plane Web Application.

Provides the Cedar Authoring UI (T-027).
"""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import cedarpy
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from agentiam_controlplane.audit_api import build_router as build_audit_router
from agentiam_controlplane.auth import build_router as build_auth_router
from agentiam_controlplane.budgets_api import build_router as build_budgets_router
from agentiam_controlplane.db.escalations import list_by_state
from agentiam_controlplane.decisions_api import build_router as build_decisions_router
from agentiam_controlplane.escalations_api import build_router as build_escalations_router
from agentiam_controlplane.metrics_api import build_router as build_metrics_router
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
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

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


#: Spec 04 §4.6's own pseudocode: `REAP() # background, every TTL/4`. The PEP's default
#: lease TTL is 60 s (`scripts/pep_service.DEFAULT_LEASE_TTL_S`), so 15 s.
DEFAULT_REAPER_INTERVAL_S = 15.0

#: `AGENTIAM_CONTROLPLANE_REAPER_INTERVAL_S`. `0` disables the reaper — a legitimate
#: configuration for a deployment running `reap()` some other way (a k8s CronJob, say),
#: and one an operator should be able to choose without editing code.
_REAPER_INTERVAL_ENV = "AGENTIAM_CONTROLPLANE_REAPER_INTERVAL_S"

logger = logging.getLogger(__name__)


def _reaper_interval_s() -> float | None:
    """How often to sweep expired leases, from the environment. `None` disables."""
    import os

    raw = os.environ.get(_REAPER_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_REAPER_INTERVAL_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{_REAPER_INTERVAL_ENV} is not a number: {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{_REAPER_INTERVAL_ENV} must not be negative, got {raw!r}")
    return value or None


def _reaper_lifespan(
    session_factory: async_sessionmaker[AsyncSession],
    interval_s: float,
    now: Callable[[], datetime],
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """A lifespan that runs `LEDGER.REAP` on a loop — spec 04 §4.6, STATUS gap 27.

    A lease stranded by a hard-killed PEP stays `ACTIVE` and its budget stays `leased`
    until something retires it. `reap()` has existed and been correct since T-013, and
    CH-3/CH-4 exercise it by advancing a clock and calling it directly — which is why the
    chaos suite's prose reports "REAP reclaims it" as a running fact. Nothing scheduled it:
    not this module, not `scripts/pep_service.py`, not `serve_pep.py`, not either compose
    file, not `deploy/k3s/`.

    A lifespan rather than `@app.on_event`, which FastAPI 0.141 deprecates and
    `tests/unit/test_pep_service.py` already asserts the PEP does not use.

    Failures are logged and the loop continues. A sweep that cannot reach Postgres is the
    same outage the request path already fails closed on, and killing the control plane
    over it would take the console and the escalation queue down with it.
    """
    import asyncio
    import contextlib

    from agentiam_controlplane.db.ledger import reap

    async def _sweep_forever() -> None:
        while True:
            # Sleep first: at startup every lease is either fresh or already stranded, and
            # sweeping in the first milliseconds of boot races the migrations a compose
            # stack may still be applying.
            await asyncio.sleep(interval_s)
            try:
                async with session_factory() as session:
                    reclaimed = await reap(session, now=now())
            except Exception:
                logger.exception("lease reaper sweep failed; retrying in %.1fs", interval_s)
                continue
            if reclaimed:
                # Only when it did something. A line every 15 seconds reading "reclaimed 0"
                # is how a log stops being read.
                logger.info("lease reaper reclaimed %d expired lease(s)", len(reclaimed))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_sweep_forever())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return lifespan


def create_app(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    escalation_settings: ControlPlaneSettings | None = None,
    oidc_settings: OIDCSettings | None = None,
    revocation_publisher: RevocationPublisher | None = None,
    reaper_interval_s: float | None = None,
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

    `reaper_interval_s` schedules `LEDGER.REAP` (spec 04 §4.6). **`None` — the default —
    means no reaper**, which is deliberate: this constructor is what the test suite drives,
    and a background task retiring expired leases underneath a ledger test would make it
    flaky in a way that looks like a ledger bug. `create_app_from_env()` turns it on, the
    same split `pep_service.py` uses for everything a deployment needs and a test does not.
    """
    lifespan = (
        _reaper_lifespan(session_factory, reaper_interval_s, now)
        if session_factory is not None and reaper_interval_s is not None and reaper_interval_s > 0
        else None
    )
    app = FastAPI(title="AgentIAM Control Plane", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness. Deliberately checks nothing external — T-056.

        Same rule as the PEP's (`agentiam_pep.app`): a liveness probe that depends on
        Postgres turns one database outage into a restart loop that cannot converge,
        because restarting this process does nothing to fix the database.
        """
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness, reporting only what is actually wired — T-056.

        This reflects *wiring*, not *reachability*, and that is deliberate. Every
        database-backed route here already answers 503 without one rather than failing to
        boot (T-046/T-047/T-048's "visibly not wired" shape), so the process can genuinely
        serve without Postgres. A probe that dialled the database would pull every replica
        out of rotation on a blip — including the console an operator would use to
        diagnose it — converting a degraded state into a total outage.
        """
        return JSONResponse(
            {
                "status": "ready",
                "checks": {
                    "database": session_factory is not None,
                    "escalations": session_factory is not None and escalation_settings is not None,
                    "auth": oidc_settings is not None,
                    "revocation_publisher": revocation_publisher is not None,
                },
            }
        )

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

    # T-048, same shape: 503 without a database rather than a boot failure.
    app.include_router(build_audit_router(session_factory=session_factory))

    # T-049: Prometheus scrapes this for the Decisions and Budgets Grafana dashboards.
    # Same shape again: 503 without a database rather than a boot failure or a 500 a
    # scrape target would report as the target itself being down.
    app.include_router(build_metrics_router(session_factory=session_factory, now=now))

    @app.get("/", response_class=HTMLResponse)
    async def overview_console(request: Request) -> HTMLResponse:
        """The console landing page.

        There was no `/` at all before this: every console page was reachable only by
        typing its path, and none of them linked to any other. This is the entry point
        the nav in `base.html` points home to, and DEMO.md beat 0 ("console open") now
        has something to be open *on*.

        It reports rather than computes — `/readyz` for subsystem state and
        `/v1/budgets/dashboard` for the ledger — so it cannot disagree with the pages it
        links to.
        """
        return templates.TemplateResponse(
            request=request,
            name="overview.html",
            context={"active": "overview", "has_database": session_factory is not None},
        )

    @app.get("/budgets", response_class=HTMLResponse)
    async def budgets_console(request: Request) -> HTMLResponse:
        """The budget and lease dashboard — T-047's console surface."""
        return templates.TemplateResponse(
            request=request,
            name="budgets.html",
            context={"active": "budgets", "has_database": session_factory is not None},
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
                "active": "decisions",
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
            context={"active": "identity-tree", "task_id": task_id},
        )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_console(request: Request) -> HTMLResponse:
        """The audit explorer + custody view — T-048's console surface.

        Search, custody, and verification all go through `/v1/audit/*` from the browser;
        this route only serves the shell, the same split `decisions_console` and
        `budgets_console` use.
        """
        return templates.TemplateResponse(
            request=request,
            name="audit.html",
            context={"active": "audit", "has_database": session_factory is not None},
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
                "active": "escalations",
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
                "active": "policy",
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


def create_app_from_env() -> FastAPI:
    """Build the control plane from environment variables — the deployment entry point.

    T-056. A container runs this as a uvicorn factory:

        uvicorn agentiam_controlplane.app:create_app_from_env --factory --host 0.0.0.0

    **This is not `app` below, and the difference matters.** `app` is `create_app()` with
    no arguments — no database, so no escalation router, no revocation router, no session
    middleware and no login. That is correct for the Cedar authoring console (T-027) and
    is what `tests/unit/test_controlplane_ui.py` drives, but pointing a deployment at it
    yields a control plane that silently cannot approve an escalation or revoke a token.

    What is required versus optional follows the shape the rest of the package already
    uses. `ControlPlaneSettings.from_env()` is **required** and raises if the signing key,
    the approver allowlist or the session secret is missing — a control plane that booted
    without them would accept an approval it could not mint a token for, and only find out
    when a human clicked the button. The database URL is **optional**: without it the app
    still boots and `/readyz` reports `database: false`, because the console is useful
    without one and a misconfigured deployment should be diagnosable through the endpoint
    that reports the problem rather than through a crash loop. OIDC is optional as a
    *whole* — but `OIDCSettings.from_env()` raises on a partial configuration, so the
    dangerous middle state (login routes that exist and cannot complete a flow) is refused
    at boot rather than discovered at the redirect.

    Raises:
        ValueError: A required `AGENTIAM_CONTROLPLANE_*` variable is missing or malformed,
            or OIDC is partially configured.
    """
    import os

    from agentiam_controlplane.db.base import make_engine, make_session_factory
    from agentiam_controlplane.settings import ControlPlaneSettings, OIDCSettings

    settings = ControlPlaneSettings.from_env()

    # `DATABASE_URL` is the fallback because that is the name `alembic.ini` and
    # `.env.example` already use; a deployment that sets only that must not come up
    # database-less without saying so.
    database_url = os.environ.get("AGENTIAM_CONTROLPLANE_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    session_factory = make_session_factory(make_engine(database_url)) if database_url else None

    # Absent entirely means "login not wired"; partially present raises inside `from_env`.
    oidc_settings = (
        OIDCSettings.from_env()
        if any(
            os.environ.get(f"AGENTIAM_CONTROLPLANE_OIDC_{name}")
            for name in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET")
        )
        else None
    )

    redis_url = os.environ.get("AGENTIAM_CONTROLPLANE_REDIS_URL")
    revocation_publisher = None
    if redis_url:
        from redis.asyncio import Redis

        from agentiam_controlplane.db.revocation_publisher import RedisRevocationPublisher

        revocation_publisher = RedisRevocationPublisher(Redis.from_url(redis_url))

    return create_app(
        session_factory=session_factory,
        escalation_settings=settings,
        oidc_settings=oidc_settings,
        revocation_publisher=revocation_publisher,
        # Spec 04 §4.6 prescribes `REAP() # background, every TTL/4`, and until now
        # nothing anywhere called it outside a test: not this module, not
        # `scripts/pep_service.py`, not `serve_pep.py`, not either compose file, not
        # `deploy/k3s/` (STATUS gap 27, found by grepping every non-test call site). A
        # lease stranded by a crash or a SIGKILL therefore stayed `leased` and
        # unavailable — not lost, since `committed` is untouched, but never reclaimed.
        #
        # It belongs here rather than in the PEP for two reasons: the ledger is this
        # service's, and a PEP that died is exactly the one that cannot reap its own lease.
        reaper_interval_s=_reaper_interval_s(),
    )


#: The console-only app: no database, no auth, no escalations. Imported by T-027's UI
#: tests. **Not a deployment entry point** — use `create_app_from_env()` for that.
app = create_app()
