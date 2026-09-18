"""Passwordless sign-in for a demo deployment — the `auth.py` stand-in.

`auth.py` is the real thing: OIDC against a Keycloak realm, authlib owning state, nonce and
PKCE. It needs an issuer, a client secret, and a redirect URI reachable from the browser,
which `docker-compose.demo.yml` deliberately does not provide.

That left the escalation queue unable to finish its own workflow. Approve and deny have
required `require_session_principal` since ADR-046 — the acting approver comes from the
session, never from the request body, because a body-supplied approver name was never
authenticated. Nothing but OIDC ever wrote a principal *into* that session, so with OIDC
unwired the queue rendered, validated its narrowing live, and then answered every approval
with 401.

This module writes the same one key OIDC writes — `session["principal_id"]` — after asking
the operator which of a small, configured set of names they are. Everything downstream is
unchanged and unaware: `escalations_api` still checks that name against
`ControlPlaneSettings.approvers` and still returns 403 when it is not there, which is why
the default persona list includes someone who is not.

**It authenticates nobody, and it is built to stay visibly that way.** Four gates, none of
them load bearing alone:

* it is mounted only when `AGENTIAM_CONTROLPLANE_DEMO_LOGIN` is explicitly truthy;
* `create_app` refuses to mount it when `OIDCSettings` is present — a real issuer always wins,
  so turning on real login cannot leave a passwordless door open beside it;
* the sign-in page states that no password is checked, and every page's top bar shows the
  session is a demo one;
* `/readyz` reports `demo_login` separately from `auth`, so a probe can tell a console with
  real login from one with this.

`POST` for the sign-in itself, not `GET`: a link that changes who you are would be
prefetchable by the browser and followable from another origin. The form is same-origin and
carries the target as a field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

    from agentiam_controlplane.settings import DemoLoginSettings

__all__ = ["build_router"]

_DEFAULT_DESTINATION = "/escalations"


def _safe_destination(value: str) -> str:
    """A same-origin path, or the default.

    Anything not starting with a single `/` is rejected, which covers an absolute URL and
    also `//evil.example`, the protocol-relative form a naive `startswith("/")` lets through
    — the browser reads that as a host, so a sign-in would end on someone else's site.
    """
    if value.startswith("/") and not value.startswith("//"):
        return value
    return _DEFAULT_DESTINATION


def build_router(
    *,
    settings: DemoLoginSettings,
    approvers: frozenset[str],
    templates: Jinja2Templates,
) -> APIRouter:
    """Build `/auth/login`, `/auth/signin`, `/auth/logout` over a fixed persona list.

    `approvers` is passed in only so the picker can *label* who may approve. It is not
    enforced here, and deliberately so: signing in as a non-approver has to be possible for
    the 403 from `escalations_api` to be demonstrable, and that refusal must come from the
    same code path a real session hits rather than from this screen declining to log you in.
    """
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.get("/login", response_class=HTMLResponse)
    async def login(
        request: Request,
        next_url: str = Query(default=_DEFAULT_DESTINATION, alias="next"),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "active": "",
                "personas": [
                    {
                        "principal_id": p.principal_id,
                        "display_name": p.display_name,
                        "title": p.title,
                        "is_approver": p.principal_id in approvers,
                    }
                    for p in settings.personas
                ],
                "next_url": _safe_destination(next_url),
                "current": request.session.get("principal_id"),
            },
        )

    @router.post("/signin")
    async def signin(
        request: Request,
        principal_id: str = Form(...),
        next_url: str = Form(default=_DEFAULT_DESTINATION, alias="next"),
    ) -> RedirectResponse:
        """Adopt one of the configured personas.

        The posted id is matched against the configured list rather than trusted, so the
        session can only ever hold a name this deployment published. Without that check the
        form would be a way to become any principal at all by editing one field — which is
        the difference between a demo sign-in and a privilege-escalation endpoint.
        """
        chosen = next(
            (p for p in settings.personas if p.principal_id == principal_id),
            None,
        )
        if chosen is None:
            return RedirectResponse(url="/auth/login?error=unknown", status_code=303)

        # Mirrors `auth.py`'s callback exactly: the same two keys, in the same format, so
        # every reader of the session — the escalation routes, the top bar, the revocation
        # API — cannot tell which of the two login modes produced it and needs no branch.
        request.session["principal_id"] = chosen.principal_id
        request.session["display_name"] = chosen.display_name
        request.session["demo_login"] = True
        return RedirectResponse(url=_safe_destination(next_url), status_code=303)

    @router.get("/logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(url="/auth/login", status_code=303)

    return router
