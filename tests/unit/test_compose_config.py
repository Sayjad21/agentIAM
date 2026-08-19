"""Compose files have to work on Linux too — T-056.

Two defects found by actually running `docker compose up` on a Fedora host, both invisible
on the Docker Desktop setup the project was developed against.

**1. SELinux labels.** On an SELinux-enforcing host (Fedora, RHEL, CentOS, Rocky) a bind
mount without a `z`/`Z` option is unreadable inside the container. Measured: the same mount
gives `Permission denied` without `:z` and reads fine with `:ro,z`. Keycloak's symptom was
`ERROR: directory not found` followed by a crash-restart loop, so `make up` never reached
healthy at all. The label is a **no-op on Docker Desktop**, which is what makes it the
cross-platform fix rather than a Linux-only workaround.

**2. Keycloak's realm-import naming convention.** Keycloak 26's `DirImportProvider` only
imports files named `<realm>-realm.json`. Given any other name it logs
*"Import finished successfully"* and imports **nothing** — measured: `realm.json` yields no
realm and a 404 on `/realms/agentiam/...`, while the identical bytes as
`agentiam-realm.json` log `Realm 'agentiam' imported` and answer 200. A silent success is
worse than a failure, which is why this is pinned by a test rather than a comment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILES = [
    _REPO_ROOT / "docker-compose.yml",
    _REPO_ROOT / "docker-compose.observability.yml",
]


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _bind_mounts(compose: dict[str, Any]) -> list[tuple[str, str]]:
    """Every `./host:container[:opts]` short-syntax bind mount, as (service, spec)."""
    found: list[tuple[str, str]] = []
    for name, service in compose.get("services", {}).items():
        for volume in service.get("volumes", []) or []:
            if isinstance(volume, str) and volume.startswith("."):
                found.append((name, volume))
    return found


def _all_bind_mounts() -> list[tuple[Path, str, str]]:
    return [
        (path, service, spec)
        for path in _COMPOSE_FILES
        for service, spec in _bind_mounts(_load(path))
    ]


class TestSelinuxLabels:
    def test_there_are_bind_mounts_to_check(self) -> None:
        # Guard against the whole suite passing vacuously if the compose files are
        # restructured to long syntax — this test would then be the one that fails.
        assert _all_bind_mounts(), "no short-syntax bind mounts found; update this test"

    @pytest.mark.parametrize(
        ("compose_path", "service", "spec"),
        [
            pytest.param(p, s, v, id=f"{p.name}:{s}:{v.split(':')[1]}")
            for p, s, v in _all_bind_mounts()
        ],
    )
    def test_every_bind_mount_carries_an_selinux_label(
        self, compose_path: Path, service: str, spec: str
    ) -> None:
        # Without this the container gets `Permission denied` on an SELinux-enforcing
        # host. Docker Desktop ignores the option, so it costs nothing there.
        options = spec.split(":")[2:]
        flags = {flag for option in options for flag in option.split(",")}
        assert flags & {"z", "Z"}, (
            f"{compose_path.name} service {service!r} mounts {spec!r} without an SELinux "
            f"label; add `z` (shared) so it works on Fedora/RHEL as well as Docker Desktop"
        )


class TestKeycloakRealmImport:
    """Keycloak imports nothing unless the file is named `<realm>-realm.json`."""

    def _keycloak_volumes(self) -> list[str]:
        compose = _load(_REPO_ROOT / "docker-compose.yml")
        volumes = compose["services"]["keycloak"]["volumes"]
        return [v for v in volumes if isinstance(v, str)]

    def test_the_import_target_follows_keycloaks_naming_convention(self) -> None:
        targets = [
            v.split(":")[1] for v in self._keycloak_volumes() if "/data/import" in v.split(":")[1]
        ]
        assert targets, "keycloak mounts nothing into /opt/keycloak/data/import"
        for target in targets:
            name = Path(target).name
            assert name.endswith("-realm.json"), (
                f"keycloak import target {target!r} does not match Keycloak's "
                f"`<realm>-realm.json` convention; it would be silently skipped"
            )

    def test_the_filename_matches_the_realm_inside_the_file(self) -> None:
        # The convention encodes the realm name, so a rename on either side silently
        # stops the import. Cross-check them.
        realm_file = _REPO_ROOT / "deploy" / "keycloak" / "realm-export.json"
        declared = json.loads(realm_file.read_text(encoding="utf-8"))["realm"]

        targets = [
            v.split(":")[1] for v in self._keycloak_volumes() if "/data/import" in v.split(":")[1]
        ]
        names = {Path(t).name for t in targets}
        assert f"{declared}-realm.json" in names, (
            f"realm-export.json declares realm {declared!r}, so the import target must be "
            f"{declared}-realm.json; found {sorted(names)}"
        )

    def test_the_realm_file_still_declares_the_client_and_approvers_the_env_names(self) -> None:
        # `.env.example` pins two approver subs against this file (ADR-046: only a realm
        # import can fix a user's `sub`). If the file loses them, login works and every
        # approval 403s.
        realm = json.loads(
            (_REPO_ROOT / "deploy" / "keycloak" / "realm-export.json").read_text(encoding="utf-8")
        )
        assert [c["clientId"] for c in realm["clients"]] == ["agentiam-console"]
        subs = {u["id"] for u in realm["users"]}
        env_example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for sub in subs:
            assert sub in env_example, f"user sub {sub} is not named in .env.example"
