"""Structural checks on `deploy/k3s/` — T-056 Part 3.

Same posture as `test_demo_compose.py` and `test_observability_config.py`: what a unit
test can prove without a cluster is that the manifests parse and are internally
consistent with the real settings classes and route table, not that a live deployment
reaches `Ready` — no Testcontainers module exists for "a Kubernetes cluster" the way
Postgres has one, and CI does not run one. See `deploy/k3s/README.md`'s "What is and is
not proven" for the manual `kubectl apply --dry-run=server` verification these tests do
not replace.

**Every required environment variable name is read from the real settings classes**
(`scripts.pep_service.ENV_PREFIX` + its required-field names, and
`agentiam_controlplane.settings`'s own `ENV_PREFIX`), never retyped as a literal string
here. A settings class renaming a variable is exactly the class of drift this file exists
to catch, and retyping the names would make the test agree with a stale copy of itself
rather than with the code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K3S = REPO_ROOT / "deploy" / "k3s"

_MANIFEST_FILES = [
    "namespace.yaml",
    "postgres.yaml",
    "redis.yaml",
    "migrate-job.yaml",
    "tools.yaml",
    "controlplane.yaml",
    "pep.yaml",
]


def _docs(filename: str) -> list[dict[str, Any]]:
    """Every YAML document in one manifest file (`---`-separated multi-doc files)."""
    text = (K3S / filename).read_text(encoding="utf-8")
    return [doc for doc in yaml.safe_load_all(text) if doc is not None]


def _all_docs() -> list[dict[str, Any]]:
    return [doc for filename in _MANIFEST_FILES for doc in _docs(filename)]


def _by_kind(kind: str) -> list[dict[str, Any]]:
    return [doc for doc in _all_docs() if doc.get("kind") == kind]


def _pep_container(deployment: dict[str, Any]) -> dict[str, Any]:
    containers: list[dict[str, Any]] = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


class TestEveryFileParses:
    @pytest.mark.parametrize("filename", _MANIFEST_FILES)
    def test_it_parses_as_yaml(self, filename: str) -> None:
        assert _docs(filename), f"{filename} produced no documents"

    def test_every_document_has_a_namespace(self) -> None:
        # `namespace.yaml` itself is the one exception — it *creates* the namespace.
        for doc in _all_docs():
            if doc.get("kind") == "Namespace":
                continue
            assert doc["metadata"]["namespace"] == "agentiam", doc.get("metadata", {}).get("name")

    def test_kustomization_lists_every_manifest_file(self) -> None:
        kustomization = yaml.safe_load((K3S / "kustomization.yaml").read_text(encoding="utf-8"))
        assert set(kustomization["resources"]) == set(_MANIFEST_FILES)


class TestImages:
    def test_every_container_uses_the_one_placeholder_image(self) -> None:
        # ADR-056 §5.1: one image, three entrypoints. A manifest referencing a second
        # image name would be building something T-056 Part 2 never produced.
        for doc in _all_docs():
            if doc.get("kind") not in ("Deployment", "Job"):
                continue
            spec = doc["spec"]["template"]["spec"]
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                if container.get("image", "").startswith("postgres:") or container.get(
                    "image", ""
                ).startswith("redis:"):
                    continue
                assert container["image"] == "agentiam:latest", (
                    f"{doc['metadata']['name']}/{container['name']}"
                )

    def test_every_agentiam_latest_container_sets_imagepullpolicy_ifnotpresent(self) -> None:
        # Measured against a real kind cluster: kubelet defaults `imagePullPolicy` to
        # `Always` for any `:latest` tag regardless of local image presence, so a locally
        # `kind load docker-image`'d build still hits `ImagePullBackOff` (no registry to
        # pull `agentiam:latest` from) unless this is set explicitly.
        for doc in _all_docs():
            if doc.get("kind") not in ("Deployment", "Job"):
                continue
            spec = doc["spec"]["template"]["spec"]
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                if container.get("image") != "agentiam:latest":
                    continue
                assert container.get("imagePullPolicy") == "IfNotPresent", (
                    f"{doc['metadata']['name']}/{container['name']}"
                )


class TestMigrateJob:
    def test_it_runs_alembic_upgrade_head_from_the_right_working_directory(self) -> None:
        # Measured while building the Dockerfile (Part 2): alembic.ini's script_location
        # resolves against the *working directory*, not the ini file's own location.
        (job,) = _by_kind("Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["alembic", "upgrade", "head"]
        assert container["workingDir"] == "/app/packages/agentiam-controlplane"

    def test_it_never_restarts_in_place(self) -> None:
        (job,) = _by_kind("Job")
        assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"

    def test_it_waits_for_postgres_before_migrating(self) -> None:
        (job,) = _by_kind("Job")
        init_containers = job["spec"]["template"]["spec"].get("initContainers", [])
        assert any("postgres" in str(c) for c in init_containers)


class TestControlplaneDeployment:
    def _deployment(self) -> dict[str, Any]:
        (deployment,) = [
            d for d in _by_kind("Deployment") if d["metadata"]["name"] == "controlplane"
        ]
        return deployment

    def test_it_runs_create_app_from_env_not_the_console_only_app(self) -> None:
        # `app = create_app()` (no args) is T-027's console-only object — database-less
        # by design (Part 1). A deployment pointed at it would silently have no
        # escalation router, no revocation router, no session middleware.
        container = _pep_container(self._deployment())
        assert "create_app_from_env" in " ".join(str(x) for x in container["command"])

    def test_every_env_var_it_sets_is_one_controlplanesettings_actually_reads(self) -> None:
        from agentiam_controlplane.settings import ENV_PREFIX as CP_PREFIX

        container = _pep_container(self._deployment())
        names = {e["name"] for e in container["env"]}
        # Every AGENTIAM_CONTROLPLANE_* name set here must carry the real prefix — a
        # typo'd prefix would be silently ignored by `os.environ.get`, not an error.
        for name in names:
            if name.startswith("AGENTIAM_CONTROLPLANE_"):
                assert name.startswith(CP_PREFIX), name

    def test_the_root_private_key_comes_from_the_secret_not_a_literal(self) -> None:
        from agentiam_controlplane.settings import ENV_PREFIX as CP_PREFIX

        container = _pep_container(self._deployment())
        by_name = {e["name"]: e for e in container["env"]}
        entry = by_name[f"{CP_PREFIX}ROOT_PRIVATE_KEY"]
        assert "valueFrom" in entry, "root private key must not be a literal env value"
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "agentiam-secrets"
        assert entry["valueFrom"]["secretKeyRef"]["key"] == "root_private_key.hex"

    def test_probes_target_the_real_healthz_route(self) -> None:
        container = _pep_container(self._deployment())
        for probe_name in ("readinessProbe", "livenessProbe"):
            assert container[probe_name]["httpGet"]["path"] == "/healthz"
            assert container[probe_name]["httpGet"]["port"] == 8000


class TestPepDeployment:
    def _deployment(self) -> dict[str, Any]:
        (deployment,) = [d for d in _by_kind("Deployment") if d["metadata"]["name"] == "pep"]
        return deployment

    def test_every_required_settings_field_has_a_corresponding_env_var(self) -> None:
        # Cross-checked against the dataclass fields `ServiceSettings.from_env()` treats
        # as required (scripts/pep_service.py) rather than a hand-maintained list, so a
        # new required field with no manifest entry fails this test instead of failing
        # silently at pod startup.
        container = _pep_container(self._deployment())
        names = {e["name"] for e in container["env"]}
        required_suffixes = [
            "UPSTREAM_BASE_URL",
            "DATABASE_URL",
            "REDIS_URL",
            "CONTROL_PLANE_URL",
            "MANDATE_ID",
            "ROOT_PUBLIC_KEYS",
            "POLICY_BUNDLE_PATH",
            "POLICY_BUNDLE_SIG_PATH",
            "POLICY_PUBLIC_KEY",
            "ROUTES_PATH",
        ]
        from scripts.pep_service import ENV_PREFIX

        for suffix in required_suffixes:
            assert f"{ENV_PREFIX}{suffix}" in names, suffix

    def test_the_public_keys_come_from_the_secret_not_literals(self) -> None:
        from scripts.pep_service import ENV_PREFIX

        container = _pep_container(self._deployment())
        by_name = {e["name"]: e for e in container["env"]}
        for suffix, key in (
            ("ROOT_PUBLIC_KEYS", "root_public_key.hex"),
            ("POLICY_PUBLIC_KEY", "policy_public_key.hex"),
        ):
            entry = by_name[f"{ENV_PREFIX}{suffix}"]
            assert entry["valueFrom"]["secretKeyRef"]["name"] == "agentiam-secrets"
            assert entry["valueFrom"]["secretKeyRef"]["key"] == key

    def test_the_file_based_paths_point_at_the_mounted_secret_volume(self) -> None:
        from scripts.pep_service import ENV_PREFIX

        container = _pep_container(self._deployment())
        by_name = {e["name"]: e.get("value") for e in container["env"]}
        assert by_name[f"{ENV_PREFIX}POLICY_BUNDLE_PATH"] == "/secrets/policy_bundle.json"
        assert by_name[f"{ENV_PREFIX}POLICY_BUNDLE_SIG_PATH"] == "/secrets/policy_bundle.sig"
        assert by_name[f"{ENV_PREFIX}ROUTES_PATH"] == "/secrets/routes.json"

    def test_the_secret_volume_actually_mounts_at_secrets(self) -> None:
        deployment = self._deployment()
        container = _pep_container(deployment)
        mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
        assert mounts.get("secrets") == "/secrets"

        volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
        assert volumes["secrets"]["secret"]["secretName"] == "agentiam-secrets"

    def test_the_secret_mount_is_read_only(self) -> None:
        container = _pep_container(self._deployment())
        (mount,) = [m for m in container["volumeMounts"] if m["name"] == "secrets"]
        assert mount["readOnly"] is True

    def test_probes_target_the_real_healthz_route(self) -> None:
        container = _pep_container(self._deployment())
        for probe_name in ("readinessProbe", "livenessProbe"):
            assert container[probe_name]["httpGet"]["path"] == "/healthz"
            assert container[probe_name]["httpGet"]["port"] == 8080


class TestNoSecretMaterialCommitted:
    def test_no_manifest_carries_a_literal_looking_secret_value(self) -> None:
        # The deliberate absence of a committed Secret manifest (see README.md) is the
        # real guard; this is the belt-and-braces check that nobody added one back with
        # a plausible-looking placeholder that isn't actually a placeholder.
        for filename in _MANIFEST_FILES:
            text = (K3S / filename).read_text(encoding="utf-8")
            assert "kind: Secret" not in text, filename

    def test_secret_key_names_referenced_match_what_bootstrap_actually_writes(self) -> None:
        from scripts.bootstrap_demo_secrets import _FILES

        referenced_keys: set[str] = set()
        for doc in _all_docs():
            if doc.get("kind") != "Deployment":
                continue
            for container in doc["spec"]["template"]["spec"]["containers"]:
                for env in container.get("env", []):
                    ref = env.get("valueFrom", {}).get("secretKeyRef")
                    if ref:
                        referenced_keys.add(ref["key"])

        assert referenced_keys, "no secretKeyRef found; update this test"
        assert referenced_keys <= set(_FILES), referenced_keys - set(_FILES)
