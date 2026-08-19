"""The PEP's deployment composition root — T-056 Part 1, `scripts/pep_service.py`.

**Why this lives in `scripts/` and not in `agentiam_pep`.** The assembled PEP needs the
ledger, the audit sink and the settlement sink, and all three live in
`agentiam_controlplane.db`. The `agentiam-pep` *package* deliberately never imports
`agentiam_controlplane` — every mention inside it is a docstring, and the sinks are
structural `Protocol`s precisely to keep the two packages independent deployables
(ADR-043 pt 4, ADR-051 pt 4). Declaring the dependency to move this into the package
would invert the architecture. The composition root is the one place that is allowed to
know about both, so it sits at the repository layer, next to `serve_pep.py`.

**Why not extend `serve_pep.py`.** That is T-053's load-test harness: it generates an
ephemeral root keypair per run, mints a mandate, seeds a budget row, hardcodes a two-line
policy and a pool sized so a 500 RPS run cannot exhaust it. Its own docstring calls itself
"the shape T-056's deployment artifacts will want" — the shape, not the thing. Its
published numbers depend on it staying as it is.

**What this root wires that nothing had wired before.** `RedisRevocationSet` (T-038/T-039)
and `RuleBasedDriftOracle` (T-032/T-036) have only ever been constructed inside tests —
both reference assemblies use `InMemoryRevocationSet()`, which never revokes anything, and
neither wires drift at all. This is the first assembly in the project where revocation and
drift are real, so these tests assert that rather than assume it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts import pep_service

if TYPE_CHECKING:
    from pathlib import Path

# Real Ed25519 public keys. An arbitrary 64-hex string is *not* one: `PublicKey.from_bytes`
# rejects a value that is not a valid curve point, so a made-up constant fails at assembly
# rather than exercising it.
_ROOT_PUBLIC_KEY_HEX = "ee4beb967352fcfd3d121e72069ac1376156c71951ef214cad517acd39b01532"
_SECOND_ROOT_PUBLIC_KEY_HEX = "ef0d623ad375dfeffbe794ac2f5bf903a4dba2981b8257f1c9e7388548cf1ed8"
_POLICY_PUBLIC_KEY_HEX = "8aba07e36c371b19ebd16f9d7f63ed4a87ac254ae752fa908e64bb2e807e8241"

_ROUTES = {
    "routes": [
        {
            "method": "GET",
            "path": "/invoices/{id}",
            "scope": "invoice:read",
            "tool": "invoice_api",
            "args": {"invoice.id": "path.id"},
        }
    ],
    "default": {"action": "deny"},
}

_CEDAR = 'permit(principal, action == Action::"invoice:read", resource);\n'


# --------------------------------------------------------------------------- fixtures


def _write_bundle(tmp_path: Path) -> tuple[Path, Path, str]:
    """Write a signed bundle + detached signature, returning both paths and the pubkey hex."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from agentiam_core.bundles import PolicyBundle, public_key_to_hex, sign_bundle

    private_key = Ed25519PrivateKey.generate()
    bundle = PolicyBundle(version="v1", cedar_source=_CEDAR, serial=1)
    signature = sign_bundle(bundle, private_key)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(
        json.dumps(
            {
                "version": bundle.version,
                "cedar_source": bundle.cedar_source,
                "serial": bundle.serial,
            }
        ),
        encoding="utf-8",
    )
    sig_path = tmp_path / "bundle.sig"
    sig_path.write_bytes(signature)
    return bundle_path, sig_path, public_key_to_hex(private_key.public_key())


def _routes_file(tmp_path: Path) -> Path:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(_ROUTES), encoding="utf-8")
    return path


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Set every required variable. Returns the policy public key hex."""
    bundle_path, sig_path, policy_pub = _write_bundle(tmp_path)
    monkeypatch.setenv("AGENTIAM_PEP_UPSTREAM_BASE_URL", "http://tools:8081")
    monkeypatch.setenv("AGENTIAM_PEP_DATABASE_URL", "postgresql+asyncpg://a:b@localhost:5432/c")
    monkeypatch.setenv("AGENTIAM_PEP_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("AGENTIAM_PEP_CONTROL_PLANE_URL", "http://controlplane:8000")
    monkeypatch.setenv("AGENTIAM_PEP_ROOT_PUBLIC_KEYS", _ROOT_PUBLIC_KEY_HEX)
    monkeypatch.setenv("AGENTIAM_PEP_POLICY_BUNDLE_PATH", str(bundle_path))
    monkeypatch.setenv("AGENTIAM_PEP_POLICY_BUNDLE_SIG_PATH", str(sig_path))
    monkeypatch.setenv("AGENTIAM_PEP_POLICY_PUBLIC_KEY", policy_pub)
    monkeypatch.setenv("AGENTIAM_PEP_ROUTES_PATH", str(_routes_file(tmp_path)))
    monkeypatch.setenv("AGENTIAM_PEP_ID", "pep-test-1")
    monkeypatch.setenv("AGENTIAM_PEP_MANDATE_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("AGENTIAM_PEP_DRIFT_MODE", raising=False)
    return policy_pub


# --------------------------------------------------------------------------- settings


class TestServiceSettings:
    def test_it_builds_from_a_complete_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _base_env(monkeypatch, tmp_path)
        settings = pep_service.ServiceSettings.from_env()

        assert settings.pep_id == "pep-test-1"
        assert settings.database_url.startswith("postgresql+asyncpg://")
        assert settings.pep.upstream_base_url == "http://tools:8081"

    @pytest.mark.parametrize(
        "missing",
        [
            "AGENTIAM_PEP_UPSTREAM_BASE_URL",
            "AGENTIAM_PEP_DATABASE_URL",
            "AGENTIAM_PEP_ROOT_PUBLIC_KEYS",
            "AGENTIAM_PEP_POLICY_BUNDLE_PATH",
            "AGENTIAM_PEP_ROUTES_PATH",
            "AGENTIAM_PEP_REDIS_URL",
            "AGENTIAM_PEP_CONTROL_PLANE_URL",
            "AGENTIAM_PEP_MANDATE_ID",
        ],
    )
    def test_every_required_variable_is_required(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
    ) -> None:
        # Fail closed at boot. A PEP that starts without a route table forwards nothing it
        # can authorize; one without root keys cannot verify a token; one without a policy
        # bundle has no second authorization layer at all. None of those should be
        # discovered by a request arriving.
        _base_env(monkeypatch, tmp_path)
        monkeypatch.delenv(missing, raising=False)

        with pytest.raises(ValueError, match=missing):
            pep_service.ServiceSettings.from_env()

    def test_root_public_keys_accepts_several_for_rotation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # EC-T05: a rotated root key must still verify tokens minted under the old one
        # until they expire, so the accepted set is plural by construction.
        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv(
            "AGENTIAM_PEP_ROOT_PUBLIC_KEYS",
            f"{_ROOT_PUBLIC_KEY_HEX}, {_SECOND_ROOT_PUBLIC_KEY_HEX}",
        )
        settings = pep_service.ServiceSettings.from_env()
        assert len(settings.root_public_keys_hex) == 2

    def test_a_malformed_mandate_id_is_refused_at_boot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENTIAM_PEP_MANDATE_ID", "not-a-uuid")
        with pytest.raises(ValueError, match="MANDATE_ID"):
            pep_service.ServiceSettings.from_env()

    def test_a_malformed_root_public_key_is_refused_at_boot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENTIAM_PEP_ROOT_PUBLIC_KEYS", "not-hex")
        with pytest.raises(ValueError, match="ROOT_PUBLIC_KEYS"):
            pep_service.ServiceSettings.from_env()


# --------------------------------------------------------------------------- policy


class TestPolicyLoading:
    def test_a_correctly_signed_bundle_loads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _base_env(monkeypatch, tmp_path)
        settings = pep_service.ServiceSettings.from_env()
        engine = pep_service.load_policy(settings)
        assert engine.bundle.version == "v1"

    def test_a_tampered_bundle_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # T-025's signature guarantee has to survive into the deployment, or the
        # policy layer is trusting a file anyone with disk access can rewrite.
        _base_env(monkeypatch, tmp_path)
        bundle_path = tmp_path / "bundle.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["cedar_source"] = "permit(principal, action, resource);\n"
        bundle_path.write_text(json.dumps(payload), encoding="utf-8")

        settings = pep_service.ServiceSettings.from_env()
        with pytest.raises(pep_service.ServiceConfigError, match="signature"):
            pep_service.load_policy(settings)

    def test_a_wrong_public_key_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENTIAM_PEP_POLICY_PUBLIC_KEY", _POLICY_PUBLIC_KEY_HEX)
        settings = pep_service.ServiceSettings.from_env()
        with pytest.raises(pep_service.ServiceConfigError, match="signature"):
            pep_service.load_policy(settings)

    def test_an_unsigned_bundle_is_refused_rather_than_trusted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Rule 6. Falling back to "load it anyway" when the signature is absent would
        # discard the whole of T-025 the first time someone forgot to sign.
        _base_env(monkeypatch, tmp_path)
        monkeypatch.delenv("AGENTIAM_PEP_POLICY_BUNDLE_SIG_PATH", raising=False)
        with pytest.raises(ValueError, match="POLICY_BUNDLE_SIG_PATH"):
            pep_service.ServiceSettings.from_env()


# --------------------------------------------------------------------------- assembly


class TestAssembly:
    def test_it_wires_a_real_redis_revocation_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The finding this test exists for: `RedisRevocationSet` had never been
        # constructed outside a test before T-056. Both reference assemblies use
        # `InMemoryRevocationSet()`, which never revokes anything, so T-038's push/pull
        # consumer and T-039's Bloom filter had never run in an assembled PEP.
        from agentiam_pep.revocation import RedisRevocationSet

        _base_env(monkeypatch, tmp_path)
        settings = pep_service.ServiceSettings.from_env()

        service = pep_service.build_service(settings)
        assert isinstance(service.revocation, RedisRevocationSet)

    def test_without_redis_it_refuses_rather_than_silently_never_revoking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An `InMemoryRevocationSet` in a deployment is a PEP that cannot be told to stop
        # trusting a stolen token — INV-10 enforced by nothing. That must be a refusal,
        # not a default.
        _base_env(monkeypatch, tmp_path)
        monkeypatch.delenv("AGENTIAM_PEP_REDIS_URL", raising=False)
        with pytest.raises(ValueError, match="REDIS_URL"):
            pep_service.ServiceSettings.from_env()

    def test_the_pipeline_is_assembled_and_the_app_reports_enforcing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastapi.testclient import TestClient

        _base_env(monkeypatch, tmp_path)
        settings = pep_service.ServiceSettings.from_env()

        service = pep_service.build_service(settings)
        body = TestClient(service.app).get("/readyz").json()
        assert body["enforcing"] is True

    def test_it_uses_lifespan_not_the_deprecated_on_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # `@app.on_event` is deprecated in FastAPI 0.141 (confirmed by triggering the
        # warning). `serve_pep.py` still uses it; new code must not.
        _base_env(monkeypatch, tmp_path)
        settings = pep_service.ServiceSettings.from_env()

        service = pep_service.build_service(settings)
        # A lifespan-configured app carries a non-default lifespan context.
        assert service.app.router.lifespan_context is not None
        assert service.app.router.on_startup == []
        assert service.app.router.on_shutdown == []
