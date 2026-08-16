"""OIDC login for the console, against Keycloak — T-043, ADR-046.

`/auth/login` redirects the browser to the realm's authorization endpoint; authlib owns
state, nonce and PKCE (Rule 1 forbids hand-rolling any of that). `/auth/callback` exchanges
the returned code for tokens and verifies the ID token's signature against the realm's JWKS
— again authlib, not this module. Only two things are pulled out of the verified token and
kept: `principal_id` (`kc:<sub>`, the format already used everywhere else in the codebase —
spec 01 line 73, ADR-041's `"kc:manager"` test literal) and a display name for the console
UI. Nothing else from the token is stored (Rule 10: no PII beyond what the UI needs to show
who is signed in).

`require_session_principal` is the dependency that replaces ADR-041 point 2's stopgap:
`escalations_api`'s approve/deny routes no longer trust a request-body `approver` field —
they trust whatever this session says, or refuse with 401.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

if TYPE_CHECKING:
    from agentiam_controlplane.settings import OIDCSettings

__all__ = ["build_router", "require_session_principal"]

_SCOPE = "openid profile email"
_CLIENT_NAME = "keycloak"
_DEFAULT_DESTINATION = "/escalations"


def build_router(*, settings: OIDCSettings) -> APIRouter:
    """Build `/auth/login`, `/auth/callback`, `/auth/logout`, bound to `settings`.

    Requires `SessionMiddleware` already installed on the app: `create_app` only mounts
    this router when it has also installed that middleware, so `request.session` below is
    always backed by a real signed cookie, never Starlette's "no middleware" `AssertionError`.
    """
    oauth = OAuth()
    oauth.register(
        name=_CLIENT_NAME,
        server_metadata_url=f"{settings.issuer.rstrip('/')}/.well-known/openid-configuration",
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        client_kwargs={"scope": _SCOPE},
    )
    # `types-authlib`'s stub for `create_client` has no return-type annotation (measured
    # against `types-authlib` 1.7.2.20260814) — the runtime value is a `StarletteOAuth2App`.
    client = oauth.create_client(_CLIENT_NAME)  # type: ignore[no-untyped-call]
    assert client is not None  # noqa: S101 — registered two lines above; documents the invariant

    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.get("/login")
    async def login(
        request: Request,
        next_url: str = Query(default=_DEFAULT_DESTINATION, alias="next"),
    ) -> RedirectResponse:
        request.session["post_login_redirect"] = (
            next_url if next_url.startswith("/") else _DEFAULT_DESTINATION
        )
        redirect_uri = str(request.url_for("auth_callback"))
        result: RedirectResponse = await client.authorize_redirect(request, redirect_uri)
        return result

    @router.get("/callback", name="auth_callback")
    async def callback(request: Request) -> RedirectResponse:
        try:
            token = await client.authorize_access_token(request)
        except Exception as exc:  # authlib's OAuthError subclasses, or a state/nonce mismatch
            raise HTTPException(status_code=401, detail=f"login failed: {exc}") from exc

        userinfo = token.get("userinfo")
        sub = userinfo.get("sub") if userinfo else None
        if not sub:
            raise HTTPException(status_code=401, detail="ID token carried no subject claim")

        request.session["principal_id"] = f"kc:{sub}"
        request.session["display_name"] = (
            userinfo.get("preferred_username") or userinfo.get("name") or sub
        )
        destination = request.session.pop("post_login_redirect", _DEFAULT_DESTINATION)
        return RedirectResponse(url=destination, status_code=303)

    @router.get("/logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        metadata = await client.load_server_metadata()
        end_session_endpoint = metadata.get("end_session_endpoint")
        if end_session_endpoint:
            return RedirectResponse(url=end_session_endpoint, status_code=303)
        return RedirectResponse(url=_DEFAULT_DESTINATION, status_code=303)

    return router


async def require_session_principal(request: Request) -> str:
    """The signed-in principal's `kc:<sub>` id.

    Raises:
        HTTPException: 401, no session (or no `principal_id` in it) exists.
    """
    principal_id = request.session.get("principal_id")
    if not principal_id:
        raise HTTPException(status_code=401, detail="login required")
    return str(principal_id)
