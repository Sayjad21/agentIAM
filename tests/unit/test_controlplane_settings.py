"""`ControlPlaneSettings.from_env` and `OIDCSettings.from_env` — T-037, T-043.

The root private key is a stopgap (no issuance service exists yet); the approver allowlist
is not — T-043 replaced *how* the acting approver is identified (a real OIDC session instead
of a request-body field, ADR-046) but the allowlist itself is unchanged in shape.
"""

from __future__ import annotations

import pytest

from agentiam_controlplane.settings import ControlPlaneSettings, OIDCSettings

_VALID_KEY_HEX = "8aba07e36c371b19ebd16f9d7f63ed4a87ac254ae752fa908e64bb2e807e8241"


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", _VALID_KEY_HEX)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "kc:manager, kc:cfo")
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_SESSION_SECRET_KEY", "test-session-secret")


def test_builds_from_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)

    settings = ControlPlaneSettings.from_env()

    assert settings.approvers == frozenset({"kc:manager", "kc:cfo"})
    assert settings.elevation_max_depth == 1
    assert settings.session_secret_key == "test-session-secret"  # noqa: S105


def test_missing_root_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", raising=False)
    with pytest.raises(ValueError, match="ROOT_PRIVATE_KEY"):
        ControlPlaneSettings.from_env()


def test_non_hex_root_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", "not-hex")
    with pytest.raises(ValueError, match="hex"):
        ControlPlaneSettings.from_env()


def test_wrong_length_root_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", "aa")
    with pytest.raises(ValueError, match="32 bytes"):
        ControlPlaneSettings.from_env()


def test_missing_approvers_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("AGENTIAM_CONTROLPLANE_APPROVERS", raising=False)
    with pytest.raises(ValueError, match="APPROVERS"):
        ControlPlaneSettings.from_env()


def test_blank_approvers_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "  , ,")
    with pytest.raises(ValueError, match="APPROVERS"):
        ControlPlaneSettings.from_env()


def test_missing_session_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("AGENTIAM_CONTROLPLANE_SESSION_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="SESSION_SECRET_KEY"):
        ControlPlaneSettings.from_env()


_OIDC_ENV = {
    "AGENTIAM_CONTROLPLANE_OIDC_ISSUER": "http://localhost:8080/realms/agentiam",
    "AGENTIAM_CONTROLPLANE_OIDC_CLIENT_ID": "agentiam-console",
    "AGENTIAM_CONTROLPLANE_OIDC_CLIENT_SECRET": "dev-console-secret-change-me",
}


def _set_oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _OIDC_ENV.items():
        monkeypatch.setenv(key, value)


def test_oidc_builds_from_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_oidc_env(monkeypatch)

    settings = OIDCSettings.from_env()

    assert settings.issuer == _OIDC_ENV["AGENTIAM_CONTROLPLANE_OIDC_ISSUER"]
    assert settings.client_id == "agentiam-console"
    assert settings.client_secret == "dev-console-secret-change-me"  # noqa: S105


@pytest.mark.parametrize("missing", list(_OIDC_ENV))
def test_oidc_missing_var_is_refused(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _set_oidc_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValueError, match=missing.removeprefix("AGENTIAM_CONTROLPLANE_")):
        OIDCSettings.from_env()
