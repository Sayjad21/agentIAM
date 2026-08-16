"""`ControlPlaneSettings.from_env` — T-037.

Two stopgaps documented as such in the module: the root private key (no issuance service
exists yet) and the approver list (no OIDC login exists yet, T-043).
"""

from __future__ import annotations

import pytest

from agentiam_controlplane.settings import ControlPlaneSettings

_VALID_KEY_HEX = "8aba07e36c371b19ebd16f9d7f63ed4a87ac254ae752fa908e64bb2e807e8241"


def test_builds_from_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", _VALID_KEY_HEX)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "kc:manager, kc:cfo")

    settings = ControlPlaneSettings.from_env()

    assert settings.approvers == frozenset({"kc:manager", "kc:cfo"})
    assert settings.elevation_max_depth == 1


def test_missing_root_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "kc:manager")
    with pytest.raises(ValueError, match="ROOT_PRIVATE_KEY"):
        ControlPlaneSettings.from_env()


def test_non_hex_root_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", "not-hex")
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "kc:manager")
    with pytest.raises(ValueError, match="hex"):
        ControlPlaneSettings.from_env()


def test_wrong_length_root_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", "aa")
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "kc:manager")
    with pytest.raises(ValueError, match="32 bytes"):
        ControlPlaneSettings.from_env()


def test_missing_approvers_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", _VALID_KEY_HEX)
    monkeypatch.delenv("AGENTIAM_CONTROLPLANE_APPROVERS", raising=False)
    with pytest.raises(ValueError, match="APPROVERS"):
        ControlPlaneSettings.from_env()


def test_blank_approvers_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY", _VALID_KEY_HEX)
    monkeypatch.setenv("AGENTIAM_CONTROLPLANE_APPROVERS", "  , ,")
    with pytest.raises(ValueError, match="APPROVERS"):
        ControlPlaneSettings.from_env()
