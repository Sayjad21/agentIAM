"""The control plane's deployment surface — T-056 Part 1.

Two things a deployable control plane needs that nothing before T-056 provided.

**Health endpoints.** `docker compose up --wait` gates on a container healthcheck and
Kubernetes gates on liveness/readiness probes. The PEP has had `/healthz` and `/readyz`
since T-018; the control plane had neither, so neither orchestrator had anything to ask.

**An env-wired factory.** `agentiam_controlplane.app.app` — the module-level instance
T-027's console tests import — is `create_app()` with *no arguments*: no `session_factory`,
so no escalations router, no revocations router, no `SessionMiddleware`, no auth. That is
exactly right for the Cedar authoring UI those tests drive, and a trap for anyone who
points `uvicorn agentiam_controlplane.app:app` at a deployment and gets a silently
crippled control plane. `create_app_from_env()` is the deployment entry point; the
module-level `app` is left alone precisely because it is not dead code.

The readiness contract follows the PEP's (`app.py` `/readyz`): **report what is actually
verified, and do not dial a dependency.** The control plane's routes already answer 503
without a database rather than failing to boot (T-046/T-047/T-048's "visibly not wired"
shape), so readiness reflects wiring, not reachability. A probe that fails on a Postgres
blip would pull every replica at once and remove the console an operator would use to
diagnose it — turning a degraded state into a total outage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from agentiam_controlplane.app import create_app, create_app_from_env

if TYPE_CHECKING:
    pass

_VALID_KEY_HEX = "8aba07e36c371b19ebd16f9d7f63ed4a87ac254ae752fa908e64bb2e807e8241"


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", _VALID_KEY_HEX)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "kc:manager,kc:cfo")
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_SESSION_SECRET_KEY", "test-session-secret")


def _clear_optional_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AGENTIAM_CONTROLPLANE_DATABASE_URL",
        "DATABASE_URL",
        "AGENTIAM_CONTROLPLANE_REDIS_URL",
        "AGENTIAM_CONTROLPLANE_OIDC_ISSUER",
        "AGENTIAM_CONTROLPLANE_OIDC_CLIENT_ID",
        "AGENTIAM_CONTROLPLANE_OIDC_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- health


class TestHealthEndpoints:
    def test_healthz_is_liveness_and_checks_nothing_external(self) -> None:
        # Built with no database on purpose: liveness must not depend on one, or a
        # Postgres outage becomes a restart loop that cannot converge.
        client = TestClient(create_app())
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readyz_answers_without_a_database(self) -> None:
        client = TestClient(create_app())
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_readyz_reports_the_unwired_app_as_unwired(self) -> None:
        client = TestClient(create_app())
        checks = client.get("/readyz").json()["checks"]
        assert checks["database"] is False
        assert checks["escalations"] is False
        assert checks["auth"] is False
        assert checks["revocation_publisher"] is False

    def test_readyz_reports_a_wired_app_as_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The guard that makes the previous test meaningful: if `checks` were hardcoded
        # to False it would pass, and if hardcoded to True the previous one would fail.
        _set_required_env(monkeypatch)
        from agentiam_controlplane.settings import ControlPlaneSettings

        app = create_app(
            session_factory=object(),  # type: ignore[arg-type]
            escalation_settings=ControlPlaneSettings.from_env(),
        )
        checks = TestClient(app).get("/readyz").json()["checks"]
        assert checks["database"] is True
        assert checks["escalations"] is True

    def test_healthz_does_not_require_a_trailing_slash_redirect(self) -> None:
        # Probes are configured with an exact path; a 307 would read as unhealthy to
        # some probe implementations rather than following it.
        client = TestClient(create_app())
        assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- env factory


class TestCreateAppFromEnv:
    def test_it_wires_the_database_when_a_url_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        _clear_optional_env(monkeypatch)
        # A URL that parses and creates an engine lazily. `create_async_engine` does not
        # connect, so no server is needed for this assertion.
        monkeypatch.setenv(
            "AGENTIAM_CONTROLPLANE_DATABASE_URL",
            "postgresql+asyncpg://agentiam:agentiam@localhost:5432/agentiam",
        )

        checks = TestClient(create_app_from_env()).get("/readyz").json()["checks"]
        assert checks["database"] is True
        assert checks["escalations"] is True

    def test_without_a_database_url_it_still_boots_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deliberately not a startup failure: the console (policy authoring, T-027) is
        # useful without a database, and a deployment that forgot the URL should be
        # diagnosable through the very endpoint that reports the problem.
        _set_required_env(monkeypatch)
        _clear_optional_env(monkeypatch)

        checks = TestClient(create_app_from_env()).get("/readyz").json()["checks"]
        assert checks["database"] is False
        assert checks["escalations"] is False

    def test_it_reads_plain_database_url_as_a_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `DATABASE_URL` is what alembic.ini and `.env.example` already use, so a
        # deployment that sets only that must not silently come up database-less.
        _set_required_env(monkeypatch)
        _clear_optional_env(monkeypatch)
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+asyncpg://agentiam:agentiam@localhost:5432/agentiam"
        )

        checks = TestClient(create_app_from_env()).get("/readyz").json()["checks"]
        assert checks["database"] is True

    def test_a_missing_required_setting_fails_loudly_at_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail closed and fail early: a control plane that boots without a signing key
        # would accept escalation approvals it cannot mint a token for, and would only
        # discover that when an approver clicked the button.
        _clear_optional_env(monkeypatch)
        for name in (
            "AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY",
            "AGENTIAM_CONTROLPLANE_APPROVERS",
            "AGENTIAM_CONTROLPLANE_SESSION_SECRET_KEY",
        ):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(ValueError, match="AGENTIAM_CONTROLPLANE_"):
            create_app_from_env()

    def test_oidc_is_optional_and_absent_means_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        _clear_optional_env(monkeypatch)

        checks = TestClient(create_app_from_env()).get("/readyz").json()["checks"]
        assert checks["auth"] is False

    def test_oidc_wires_when_fully_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_required_env(monkeypatch)
        _clear_optional_env(monkeypatch)
        monkeypatch.setenv(
            "AGENTIAM_CONTROLPLANE_DATABASE_URL",
            "postgresql+asyncpg://agentiam:agentiam@localhost:5432/agentiam",
        )
        monkeypatch.setenv(
            "AGENTIAM_CONTROLPLANE_OIDC_ISSUER", "http://localhost:8080/realms/agentiam"
        )
        monkeypatch.setenv("AGENTIAM_CONTROLPLANE_OIDC_CLIENT_ID", "agentiam-console")
        monkeypatch.setenv("AGENTIAM_CONTROLPLANE_OIDC_CLIENT_SECRET", "dev-secret")

        checks = TestClient(create_app_from_env()).get("/readyz").json()["checks"]
        assert checks["auth"] is True

    def test_a_partially_configured_oidc_is_refused_rather_than_half_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Half-configured OIDC is the dangerous middle: login routes that exist and
        # cannot complete a flow. Refuse at boot instead.
        _set_required_env(monkeypatch)
        _clear_optional_env(monkeypatch)
        monkeypatch.setenv(
            "AGENTIAM_CONTROLPLANE_OIDC_ISSUER", "http://localhost:8080/realms/agentiam"
        )

        with pytest.raises(ValueError, match="AGENTIAM_CONTROLPLANE_OIDC_"):
            create_app_from_env()


# --------------------------------------------------------------------------- the trap


class TestTheModuleLevelAppIsConsoleOnly:
    """`app = create_app()` is imported by T-027's console tests, so it is not dead code.

    It is also not a deployment entry point, and this pins the distinction so a future
    reader does not "fix" one by breaking the other.
    """

    def test_the_module_level_app_is_database_less_by_construction(self) -> None:
        from agentiam_controlplane.app import app

        checks = TestClient(app).get("/readyz").json()["checks"]
        assert checks["database"] is False

    def test_create_app_from_env_is_a_different_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        _clear_optional_env(monkeypatch)
        from agentiam_controlplane.app import app

        assert create_app_from_env() is not app
