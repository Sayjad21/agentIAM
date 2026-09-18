"""Passwordless demo sign-in — `demo_auth.py` and `DemoLoginSettings`.

The escalation queue has required a real session since ADR-046: approve and deny take the
acting approver from `request.session` and never from the request body. Only OIDC ever wrote
a principal into that session, and the demo stack leaves OIDC unwired — so the queue rendered,
validated its narrowing live, and answered every approval with 401.

These tests cover the stand-in that fills that gap, and they spend more effort on its *gates*
than on its happy path, because the happy path is three lines and the gates are the reason it
is safe to have at all: it authenticates nobody.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentiam_controlplane.settings import DemoLoginSettings, Persona

ENV = "AGENTIAM_CONTROLPLANE_DEMO_LOGIN"
PERSONAS_ENV = "AGENTIAM_CONTROLPLANE_DEMO_PERSONAS"


@pytest.fixture
def clean_env() -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in (ENV, PERSONAS_ENV)}
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestItIsOffUnlessAskedFor:
    """Off unless explicitly asked for.

    Defaulting this on would be the drift the gate exists to prevent, so absence and every
    spelling of "no" must all mean off.
    """

    @pytest.mark.usefixtures("clean_env")
    def test_absent_means_no_demo_login(self) -> None:
        assert DemoLoginSettings.from_env() is None

    @pytest.mark.usefixtures("clean_env")
    @pytest.mark.parametrize("value", ["", " ", "0", "false", "False", "no", "off", "maybe"])
    def test_falsey_spellings_mean_no_demo_login(self, value: str) -> None:
        os.environ[ENV] = value
        assert DemoLoginSettings.from_env() is None

    @pytest.mark.usefixtures("clean_env")
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_spellings_switch_it_on(self, value: str) -> None:
        os.environ[ENV] = value
        settings = DemoLoginSettings.from_env()
        assert settings is not None
        assert settings.personas


class TestPersonaParsing:
    @pytest.mark.usefixtures("clean_env")
    def test_defaults_include_a_non_approver(self) -> None:
        """A picker offering only approvers would prove nothing about who may approve.

        The 403 an unauthorized name gets is the allowlist doing its job, and it comes from
        `escalations_api`, not from this screen — which is only demonstrable if signing in
        as such a name is possible.
        """
        os.environ[ENV] = "true"
        settings = DemoLoginSettings.from_env()
        assert settings is not None
        approvers = {
            "kc:11111111-1111-1111-1111-111111111111",
            "kc:22222222-2222-2222-2222-222222222222",
        }
        ids = {p.principal_id for p in settings.personas}
        assert ids & approvers == approvers, "both compose-default approvers are offered"
        assert ids - approvers, "and at least one name that is not an approver"

    @pytest.mark.usefixtures("clean_env")
    def test_name_and_title_are_parsed(self) -> None:
        os.environ[ENV] = "1"
        os.environ[PERSONAS_ENV] = "kc:a=Ayesha Karim|Controller,kc:b=Rafi"
        settings = DemoLoginSettings.from_env()
        assert settings is not None
        assert settings.personas == (
            Persona("kc:a", "Ayesha Karim", "Controller"),
            Persona("kc:b", "Rafi", ""),
        )

    @pytest.mark.usefixtures("clean_env")
    def test_a_bare_id_falls_back_to_itself_as_the_name(self) -> None:
        os.environ[ENV] = "1"
        os.environ[PERSONAS_ENV] = "kc:solo"
        settings = DemoLoginSettings.from_env()
        assert settings is not None
        assert settings.personas == (Persona("kc:solo", "kc:solo", ""),)

    @pytest.mark.usefixtures("clean_env")
    def test_an_entry_with_no_principal_is_rejected(self) -> None:
        os.environ[ENV] = "1"
        os.environ[PERSONAS_ENV] = "=Nobody|Ghost"
        with pytest.raises(ValueError, match="no principal id"):
            DemoLoginSettings.from_env()

    @pytest.mark.usefixtures("clean_env")
    def test_set_but_empty_of_names_is_rejected(self) -> None:
        os.environ[ENV] = "1"
        os.environ[PERSONAS_ENV] = " , , "
        with pytest.raises(ValueError, match="names nobody"):
            DemoLoginSettings.from_env()


class _EmptyResult:
    """Enough of a SQLAlchemy result for a page that renders an empty list."""

    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None

    def one_or_none(self) -> None:
        return None


class _EmptySession:
    async def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()

    async def __aenter__(self) -> _EmptySession:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _EmptySessionFactory:
    """A session factory whose every query comes back empty.

    The escalation routes have to be mounted for `SessionMiddleware` to be installed, and
    that needs a factory — but nothing here reads a row, so the pages under test should
    render their empty state rather than reach a database. A `MagicMock` cannot: its
    `execute` returns a coroutine that `.scalars()` is then called on, which fails inside
    the query rather than in the test.
    """

    def __call__(self) -> _EmptySession:
        return _EmptySession()


def _app(**over: Any) -> Any:
    """A console app with demo login wired, over an in-memory-ish session factory.

    The escalation routes need a `session_factory` to be mounted at all, and mounting them
    is what installs `SessionMiddleware` — which is what this router writes into. A stub
    factory is enough: no test here reaches the database.
    """
    from biscuit_auth import Algorithm, PrivateKey

    from agentiam_controlplane.app import create_app
    from agentiam_controlplane.settings import ControlPlaneSettings

    key = PrivateKey.from_bytes(bytes(range(32)), Algorithm.Ed25519)  # type: ignore[call-arg,attr-defined]
    kwargs: dict[str, Any] = {
        "session_factory": _EmptySessionFactory(),
        "escalation_settings": ControlPlaneSettings(
            root_private_key=key,
            approvers=frozenset({"kc:approver"}),
            session_secret_key="not-a-secret-test-fixture",  # noqa: S106
        ),
        "demo_login_settings": DemoLoginSettings(
            personas=(
                Persona("kc:approver", "Ada Approver", "CFO"),
                Persona("kc:outsider", "Otto Outsider", "Analyst"),
            )
        ),
    }
    kwargs.update(over)
    return create_app(**kwargs)


class TestTheRoutes:
    def test_the_picker_lists_every_persona_and_labels_who_may_approve(self) -> None:
        with TestClient(_app()) as client:
            resp = client.get("/auth/login")
        assert resp.status_code == 200
        assert "Ada Approver" in resp.text
        assert "Otto Outsider" in resp.text
        assert "may approve escalations" in resp.text
        assert "not on the approver allowlist" in resp.text

    def test_the_picker_says_no_password_is_checked(self) -> None:
        """Stated on the screen, not only in the code.

        A demo login that looks real is worse than none, because nothing downstream can
        tell the difference.
        """
        with TestClient(_app()) as client:
            resp = client.get("/auth/login")
        assert "no password is checked" in resp.text.lower()

    def test_signing_in_puts_the_principal_in_the_session(self) -> None:
        with TestClient(_app()) as client:
            resp = client.post(
                "/auth/signin",
                data={"principal_id": "kc:approver", "next": "/escalations"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/escalations"
            # The proof it took: the top bar renders the name on an unrelated page.
            assert "Ada Approver" in client.get("/budgets").text

    def test_an_unlisted_principal_cannot_be_adopted(self) -> None:
        """The posted id is matched against the configured list rather than trusted.

        Without this the form is a way to become any principal at all by editing one
        field, which is the difference between a demo sign-in and a privilege-escalation
        endpoint.
        """
        with TestClient(_app()) as client:
            resp = client.post(
                "/auth/signin",
                data={"principal_id": "kc:root", "next": "/escalations"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=unknown" in resp.headers["location"]
            assert "kc:root" not in client.get("/budgets").text

    @pytest.mark.parametrize("hostile", ["//evil.example", "https://evil.example", "evil"])
    def test_the_redirect_target_must_be_a_same_origin_path(self, hostile: str) -> None:
        """The redirect target must be a same-origin path.

        `//evil.example` is the one a naive `startswith("/")` lets through — the browser
        reads it as a host, so a sign-in would land on someone else's site.
        """
        with TestClient(_app()) as client:
            resp = client.post(
                "/auth/signin",
                data={"principal_id": "kc:approver", "next": hostile},
                follow_redirects=False,
            )
            assert resp.headers["location"] == "/escalations"

    def test_signing_out_clears_the_session(self) -> None:
        with TestClient(_app()) as client:
            client.post("/auth/signin", data={"principal_id": "kc:approver"})
            assert "Ada Approver" in client.get("/budgets").text
            client.get("/auth/logout")
            assert "Ada Approver" not in client.get("/budgets").text

    def test_a_signed_in_non_approver_is_warned_before_they_try(self) -> None:
        with TestClient(_app()) as client:
            client.post("/auth/signin", data={"principal_id": "kc:outsider"})
            page = client.get("/escalations").text
        assert "not on the approver allowlist" in page
        assert "403" in page


class TestItYieldsToRealLogin:
    def test_oidc_wins_and_the_passwordless_door_is_not_served(self) -> None:
        """A real issuer always wins.

        Switching real login on must not leave a credential-free route mounted beside it.
        """
        from agentiam_controlplane.settings import OIDCSettings

        app = _app(
            oidc_settings=OIDCSettings(
                issuer="http://keycloak.invalid/realms/agentiam",
                client_id="console",
                client_secret="not-a-secret-test-fixture",  # noqa: S106
            )
        )
        with TestClient(app) as client:
            # The persona picker is a POST-backed page; with OIDC mounted, `/auth/login` is
            # authlib's redirect route instead, and `/auth/signin` does not exist at all.
            resp = client.post("/auth/signin", data={"principal_id": "kc:approver"})
        assert resp.status_code == 404

    def test_readyz_reports_demo_login_apart_from_auth(self) -> None:
        """A probe that could not tell the two apart would read a demo console as secured."""
        with TestClient(_app()) as client:
            checks = client.get("/readyz").json()["checks"]
        assert checks["auth"] is False
        assert checks["demo_login"] is True

    def test_without_either_the_top_bar_says_login_is_not_configured(self) -> None:
        with TestClient(_app(demo_login_settings=None)) as client:
            page = client.get("/budgets").text
        assert "login not configured" in page
