"""Structural checks on `docker-compose.demo.yml` and `Dockerfile` — T-056 Part 2.

Same shape as `test_observability_config.py`: what a unit test can prove without Docker is
that the files parse and say what they need to say — not that the stack actually reaches
healthy, which is a live `docker compose up --wait` concern (verified manually and in the
`infrastructure` CI job, not here; no Testcontainers module exists for "the whole demo
stack" the way Postgres has one).

Two defects these tests exist because of, both found only by actually running the stack on
this host rather than by reading the YAML:

* A fresh **named** Docker volume mounts as `root:root` unless the image pre-creates the
  mount point with the right ownership — the `bootstrap` container (uid 1000) got
  `PermissionError` writing to `demo-secrets` until the Dockerfile did that.
* Every app service needs `service_completed_successfully` on both one-shot services
  (`bootstrap`, `migrate`), not just one — a service that starts before its credentials or
  its schema exist is a service that starts broken.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (REPO_ROOT / "docker-compose.demo.yml").read_text(encoding="utf-8")
    )
    return loaded


class TestComposeFile:
    def test_it_parses_and_has_the_expected_services(self) -> None:
        compose = _compose()
        assert set(compose["services"]) == {
            "bootstrap",
            "migrate",
            "tools",
            "controlplane",
            "pep",
            "ollama",
        }

    def test_it_is_an_overlay_not_a_standalone_stack(self) -> None:
        # No postgres/redis/keycloak here — docker-compose.yml already defines them, and
        # `docker-compose.observability.yml` established the "overlay, not duplicate"
        # pattern for exactly this reason.
        compose = _compose()
        assert "postgres" not in compose["services"]
        assert "redis" not in compose["services"]
        assert "keycloak" not in compose["services"]

    def test_ollama_is_opt_in_via_a_profile(self) -> None:
        # Not part of the default bring-up NFR-8 is measured against: the LLM backend
        # defaults to hosted inference (ADR-040), and a model pull is several GB.
        compose = _compose()
        assert compose["services"]["ollama"].get("profiles") == ["llm"]

    def test_every_service_except_ollama_builds_from_the_one_dockerfile(self) -> None:
        # ADR-056 §5.1: one image, three entrypoints — not three separately-built images
        # for identical layers.
        compose = _compose()
        for name, service in compose["services"].items():
            if name == "ollama":
                continue
            assert service["build"]["dockerfile"] == "Dockerfile", name


class TestOneShotGating:
    def test_bootstrap_and_migrate_gate_both_app_services(self) -> None:
        compose = _compose()
        for name in ("controlplane", "pep"):
            depends_on = compose["services"][name]["depends_on"]
            for gate in ("bootstrap", "migrate"):
                assert gate in depends_on, f"{name} does not depend on {gate}"
                assert depends_on[gate]["condition"] == "service_completed_successfully"

    def test_pep_waits_for_a_healthy_controlplane_and_tools(self) -> None:
        compose = _compose()
        depends_on = compose["services"]["pep"]["depends_on"]
        assert depends_on["controlplane"]["condition"] == "service_healthy"
        assert depends_on["tools"]["condition"] == "service_healthy"

    def test_bootstrap_writes_and_the_apps_read_only_the_shared_secret_volume(self) -> None:
        compose = _compose()
        bootstrap_volumes = compose["services"]["bootstrap"]["volumes"]
        assert bootstrap_volumes == ["demo-secrets:/secrets"]  # read-write, no `:ro`

        for name in ("controlplane", "pep"):
            volumes = compose["services"][name]["volumes"]
            assert any(v.endswith(":ro") and "demo-secrets" in v for v in volumes), name


class TestHealthchecks:
    def test_controlplane_and_pep_and_tools_all_have_one(self) -> None:
        compose = _compose()
        for name in ("controlplane", "pep", "tools"):
            assert "healthcheck" in compose["services"][name], name

    def test_healthchecks_use_urllib_not_curl_or_wget(self) -> None:
        # The base image is python:3.12-slim, which has neither — measured. Adding one
        # just for a healthcheck is a new package for something the standard library
        # already does, and `scripts/run_load_test.py` already sets the precedent.
        compose = _compose()
        for name in ("controlplane", "pep", "tools"):
            test = " ".join(compose["services"][name]["healthcheck"]["test"])
            assert "urllib" in test, name
            assert "curl" not in test.lower(), name
            assert "wget" not in test.lower(), name


class TestSecretHandling:
    def test_credentials_are_read_from_files_not_baked_into_environment_values(self) -> None:
        # The compose file's own `environment:` block must never carry a literal private
        # key or bundle signature — those come from the `demo-secrets` volume at runtime,
        # via the shell wrapper reading files into env vars.
        compose = _compose()
        for name in ("controlplane", "pep"):
            environment = compose["services"][name].get("environment", {})
            blob = str(environment)
            assert "ROOT_PRIVATE_KEY" not in blob or "cat /secrets" in str(
                compose["services"][name].get("command", "")
            )

    def test_the_command_reads_secrets_from_the_mounted_volume(self) -> None:
        compose = _compose()
        controlplane_cmd = str(compose["services"]["controlplane"]["command"])
        pep_cmd = str(compose["services"]["pep"]["command"])
        assert "/secrets/root_private_key.hex" in controlplane_cmd
        assert "/secrets/root_public_key.hex" in pep_cmd
        assert "/secrets/policy_public_key.hex" in pep_cmd


class TestDockerfile:
    def _text(self) -> str:
        return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_it_creates_a_non_root_user(self) -> None:
        text = self._text()
        assert "useradd" in text
        assert "USER agentiam" in text

    def test_it_pre_creates_the_secrets_mount_point_with_the_right_ownership(self) -> None:
        # The defect this guards: a fresh named volume mounted at /secrets inherits
        # root:root unless the image already owns that path before the volume overlays
        # it. Measured: without this, the non-root `bootstrap` container cannot write.
        text = self._text()
        assert "mkdir -p /secrets" in text
        assert "chown agentiam:agentiam /secrets" in text

    def test_it_does_not_use_no_editable_install(self) -> None:
        # Measured: packages/agentiam-controlplane/alembic.ini's script_location is
        # relative to alembic's cwd, not the ini file, so the migration container needs
        # `src/` physically present — which only an editable (default) workspace install
        # guarantees. Checked against actual `RUN`/`uv sync` lines, not the whole file:
        # the file's own explanatory comment names `--no-editable` as the rejected
        # alternative, which a plain substring search would wrongly flag.
        commands = [
            line
            for line in self._text().splitlines()
            if line.strip().startswith(("RUN", "uv sync"))
        ]
        assert commands, "no RUN lines found; update this test"
        assert not any("--no-editable" in line for line in commands)

    def test_uv_is_pinned_to_a_specific_version_not_a_floating_tag(self) -> None:
        # Reproducibility: `:latest` would make a rebuild months from now silently pick
        # up a different uv, at a moment nobody is watching for it.
        import re

        text = self._text()
        match = re.search(r"ghcr\.io/astral-sh/uv:(\S+)", text)
        assert match is not None, "no ghcr.io/astral-sh/uv image reference found"
        assert re.fullmatch(r"\d+\.\d+\.\d+", match.group(1)), (
            f"uv pin {match.group(1)!r} is not a dotted version"
        )

    def test_dockerignore_excludes_dev_and_test_artifacts(self) -> None:
        ignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (".venv/", "tests/", ".git/", "__pycache__"):
            assert pattern in ignore, pattern
