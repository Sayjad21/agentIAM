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
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

import pytest

from agentiam_core.attenuation import attenuate
from agentiam_core.models import Budget, Mandate, ScopeSubset
from agentiam_core.tokens import (
    RootKeySet,
    VerifiedToken,
    generate_keypair,
    mint_root,
    verify,
)
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

    def test_without_an_ollama_url_drift_is_off_not_silently_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # `decide()`'s own docstring: "drift=None means no assessment, which is not a
        # failure" (spec 06 §2.1 — an oracle failure is advisory, not fatal, unlike
        # revocation). So no `AGENTIAM_PEP_OLLAMA_URL` is a legitimate, safe
        # configuration, and `Service` says so explicitly rather than the caller having
        # to infer it from an absent attribute.
        _base_env(monkeypatch, tmp_path)
        monkeypatch.delenv("AGENTIAM_PEP_OLLAMA_URL", raising=False)
        settings = pep_service.ServiceSettings.from_env()

        service = pep_service.build_service(settings)
        assert service.drift_oracle is None

    def test_an_ollama_url_wires_a_real_drift_oracle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The finding this test exists for: `RuleBasedDriftOracle` (T-032/T-036) had
        # never been constructed outside `tests/chaos/pepstack.py`'s helper. Demo Beat 6
        # (goal drift escalates to a human) has no implementation in any deployed PEP
        # without this.
        from agentiam_pep.drift import RuleBasedDriftOracle

        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENTIAM_PEP_OLLAMA_URL", "http://ollama:11434")
        settings = pep_service.ServiceSettings.from_env()

        service = pep_service.build_service(settings)
        assert isinstance(service.drift_oracle, RuleBasedDriftOracle)

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


class TestLeasePriming:
    """The deployed service must draw a lease at boot, or it refuses every budgeted call.

    `LeasePool` acquires its first lease in `prime()`. The e2e slice calls it by hand;
    this composition root did not, and `LeasePool.covers()` has no way back from that —
    it returns `ok=False` when no `_Held` exists for the dimension, and top-ups are
    scheduled only from an existing lease. Its own docstring names the case: "a PEP that
    never primed the dimension, not one that ran dry".

    Measured against a live PEP before the fix: `leases` stayed empty, `budgets.committed`
    never left 0.0000 across 15 recorded decisions, and a 100 BDT payment against a
    500,000 pool returned 429. **This suite passed identically before and after** — 22
    tests either way — because nothing here could see the pool. That is what these cover.
    """

    @staticmethod
    async def _run_lifespan(service: pep_service.Service, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enter and exit the app's lifespan with the I/O-bound workers stubbed out.

        Only the workers are stubbed. `prime` is left alone — it is the thing under test —
        and it is safe to let it run against an unreachable ledger precisely because the
        fix made a failed prime non-fatal.
        """
        from agentiam_pep.emitter import DecisionEmitter
        from agentiam_pep.pool import LeasePool
        from agentiam_pep.revocation import RedisRevocationSet
        from agentiam_pep.settlement import SettlementQueue

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        for cls, names in (
            (DecisionEmitter, ("start", "aclose")),
            (SettlementQueue, ("start", "aclose")),
            (RedisRevocationSet, ("start", "aclose")),
            (LeasePool, ("aclose",)),
        ):
            for name in names:
                monkeypatch.setattr(cls, name, noop)

        async with service.app.router.lifespan_context(service.app):
            pass

    async def test_the_lifespan_primes_the_spend_lease(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The regression. Remove the `prime()` call and this fails."""
        from agentiam_core.models import BudgetDimension
        from agentiam_pep.pool import LeasePool

        primed: list[BudgetDimension] = []

        async def record(_self: object, dimension: BudgetDimension) -> bool:
            primed.append(dimension)
            return True

        monkeypatch.setattr(LeasePool, "prime", record)

        _base_env(monkeypatch, tmp_path)
        service = pep_service.build_service(pep_service.ServiceSettings.from_env())
        await self._run_lifespan(service, monkeypatch)

        assert primed == [BudgetDimension.SPEND_BDT]

    async def test_a_pool_that_cannot_prime_still_lets_the_service_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unreachable ledger at boot must not take the read-only paths down with it.

        `invoice:read` consumes no budget, so a PEP that cannot lease can still serve it
        correctly. Crash-looping here would refuse those too, and fail-closed per request
        is already the behaviour when a budgeted call arrives.
        """
        from agentiam_pep.pool import LeasePool

        async def cannot_prime(_self: object, _dimension: object) -> bool:
            return False

        monkeypatch.setattr(LeasePool, "prime", cannot_prime)

        _base_env(monkeypatch, tmp_path)
        service = pep_service.build_service(pep_service.ServiceSettings.from_env())
        await self._run_lifespan(service, monkeypatch)  # must not raise

    async def test_a_prime_that_raises_still_lets_the_service_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`prime()` raises as well as returning False, and only one of those was handled.

        `ACQUIRE` does `scalar_one()` on the budget row, so a mandate with no row raises
        `NoResultFound` and an exhausted pool raises `LeaseUnavailableError`. Neither the
        pool nor this module's ledger client catches either, so handling only the `False`
        return turned both into a boot failure.

        That is not hypothetical: `docker-compose.demo.yml` points the PEP at a
        *placeholder* mandate with no budget row on purpose — its own comment says the
        placeholder "lets the container start and prove the pipeline wiring" — so the
        container stopped starting and CI's demo-stack job timed out waiting for it.
        Reproduced against a real Postgres.
        """
        from sqlalchemy.exc import NoResultFound

        from agentiam_pep.pool import LeasePool

        async def explodes(_self: object, _dimension: object) -> bool:
            raise NoResultFound("No row was found when one was required")

        monkeypatch.setattr(LeasePool, "prime", explodes)

        _base_env(monkeypatch, tmp_path)
        service = pep_service.build_service(pep_service.ServiceSettings.from_env())
        await self._run_lifespan(service, monkeypatch)  # must not raise

    async def test_a_prime_that_raises_is_logged_with_its_cause(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The traceback is what tells an operator *which* misconfiguration this is."""
        from sqlalchemy.exc import NoResultFound

        from agentiam_pep.pool import LeasePool

        async def explodes(_self: object, _dimension: object) -> bool:
            raise NoResultFound("No row was found when one was required")

        monkeypatch.setattr(LeasePool, "prime", explodes)

        _base_env(monkeypatch, tmp_path)
        service = pep_service.build_service(pep_service.ServiceSettings.from_env())
        with caplog.at_level(logging.WARNING, logger="scripts.pep_service"):
            await self._run_lifespan(service, monkeypatch)

        assert any("lease" in r.message.lower() for r in caplog.records), caplog.text
        assert "NoResultFound" in caplog.text, "the cause must survive into the log"

    async def test_a_failed_prime_is_logged_so_it_is_diagnosable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-fatal must not mean silent: every budgeted call will refuse until a top-up."""
        from agentiam_pep.pool import LeasePool

        async def cannot_prime(_self: object, _dimension: object) -> bool:
            return False

        monkeypatch.setattr(LeasePool, "prime", cannot_prime)

        _base_env(monkeypatch, tmp_path)
        service = pep_service.build_service(pep_service.ServiceSettings.from_env())
        with caplog.at_level(logging.WARNING, logger="scripts.pep_service"):
            await self._run_lifespan(service, monkeypatch)

        assert any("lease" in r.message.lower() for r in caplog.records), caplog.text

    def test_the_pool_is_reachable_from_the_assembled_service(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Exposed deliberately — the tests above cannot exist without it."""
        from agentiam_pep.pool import LeasePool

        _base_env(monkeypatch, tmp_path)
        service = pep_service.build_service(pep_service.ServiceSettings.from_env())
        assert isinstance(service.pool, LeasePool)


class TestLeaseSizing:
    """`AGENTIAM_PEP_LEASE_SIZE` — TODO item 6.

    Every other setting in `from_env` read an override; this one did not, so the 5,000
    default was the hard ceiling on any single payment a deployed PEP could authorize. A
    request larger than the lease is refused with LEASE_UNAVAILABLE, and retrying never
    helps because a top-up refills *to the lease size* rather than to cover the request
    — measured against a live PEP: three attempts three seconds apart, all 429, against a
    mandate granting 500,000.
    """

    def test_unset_keeps_the_spec_04_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _base_env(monkeypatch, tmp_path)
        monkeypatch.delenv(f"{pep_service.ENV_PREFIX}LEASE_SIZE", raising=False)
        assert pep_service.ServiceSettings.from_env().lease_size == pep_service.DEFAULT_LEASE_SIZE

    def test_a_larger_lease_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from decimal import Decimal

        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv(f"{pep_service.ENV_PREFIX}LEASE_SIZE", "250000.0000")
        assert pep_service.ServiceSettings.from_env().lease_size == Decimal("250000.0000")

    def test_it_reaches_the_pool_rather_than_stopping_at_the_settings_object(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A setting that parses and is never used is the bug this replaces, not a fix."""
        from decimal import Decimal

        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv(f"{pep_service.ENV_PREFIX}LEASE_SIZE", "250000.0000")
        service = pep_service.build_service(pep_service.ServiceSettings.from_env())
        assert service.pool._settings.lease_size == Decimal("250000.0000")

    @pytest.mark.parametrize("bad", ["nonsense", "0", "-1"])
    def test_an_unusable_value_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: str
    ) -> None:
        """Not a silent fall back to the default — that takes effect as a number nobody wrote."""
        _base_env(monkeypatch, tmp_path)
        monkeypatch.setenv(f"{pep_service.ENV_PREFIX}LEASE_SIZE", bad)
        with pytest.raises(ValueError, match="LEASE_SIZE"):
            pep_service.ServiceSettings.from_env()


class TestAgentIdentity:
    """The deployed PEP reads the delegated agent's name off its own token — TODO item 4.

    Before this, `principal_for` returned `agt-depth-{N}`. Three siblings at depth 1 all
    called `agt-depth-1` is not a labelling wart: the identity tree is built from decision
    records keyed on `agent_id`, so three agents collapsed into one node and `DEMO.md` beat 2
    showed a chain where the product's claim is a *tree*.

    The names were in the tokens the whole time — `attenuate()` writes `agent()` and `role()`
    into every block (spec 01 §6.1) — and nothing could read them back, because block facts
    are invisible to the authorizer (spec 02 §9 finding 13). `agentiam_core.datalog` is the
    route to them.
    """

    NOW: ClassVar = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

    @classmethod
    def _root(
        cls, scopes: frozenset[str] = frozenset({"invoice:read"})
    ) -> tuple[VerifiedToken, RootKeySet]:
        """A verified root token, and the key set its descendants verify against."""
        keys = generate_keypair()
        key_set = RootKeySet((keys.public_key,))
        mandate = Mandate(
            mandate_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            principal_id="kc:alice",
            intent_hash="a" * 64,
            scopes=scopes,
            budget=Budget(spend_bdt=Decimal("500000")),
            max_depth=4,
            not_before=cls.NOW,
            expires_at=cls.NOW + timedelta(hours=1),
        )
        return verify(mint_root(mandate, keys.private_key), key_set, now=cls.NOW), key_set

    @classmethod
    def _delegate(
        cls, parent: VerifiedToken, key_set: RootKeySet, *, agent_id: str, role: str
    ) -> VerifiedToken:
        """One level of real delegation, with the names a parent would actually assign."""
        child = attenuate(
            parent,
            [ScopeSubset(scopes=frozenset({"invoice:read"}))],
            agent_id=agent_id,
            role=role,
        )
        return verify(child, key_set, now=cls.NOW)

    @staticmethod
    def _service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pep_service.Service:
        _base_env(monkeypatch, tmp_path)
        return pep_service.build_service(pep_service.ServiceSettings.from_env())

    def test_the_principal_carries_the_agent_id_the_parent_assigned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        service = self._service(monkeypatch, tmp_path)
        root, key_set = self._root()
        token = self._delegate(root, key_set, agent_id="agt-payer", role="payer")

        assert service.principal_for(token).agent_id == "agt-payer"

    def test_siblings_at_one_depth_get_distinct_ids(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The defect itself: `agt-depth-1` made three agents one node in the tree."""
        service = self._service(monkeypatch, tmp_path)
        root, key_set = self._root(frozenset({"invoice:read", "vendor:read"}))
        siblings = [
            self._delegate(root, key_set, agent_id=name, role="worker")
            for name in ("agt-doc-reader", "agt-negotiator", "agt-payer")
        ]

        ids = [service.principal_for(t).agent_id for t in siblings]
        assert ids == ["agt-doc-reader", "agt-negotiator", "agt-payer"]

    def test_a_deeper_agent_is_named_by_its_own_block_not_its_parent_s(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Depth 2 is `agt-settlement`, not the `agt-payer` it inherited authority from."""
        service = self._service(monkeypatch, tmp_path)
        root, key_set = self._root()
        payer = self._delegate(root, key_set, agent_id="agt-payer", role="payer")
        settlement = self._delegate(payer, key_set, agent_id="agt-settlement", role="payer")

        assert service.principal_for(settlement).agent_id == "agt-settlement"

    def test_a_root_token_falls_back_to_the_depth_derived_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No attenuation block means no `agent()` fact, and no name to invent."""
        service = self._service(monkeypatch, tmp_path)
        root, _ = self._root()

        principal = service.principal_for(root)
        assert principal.agent_id == "agt-depth-0"
        assert principal.declared_role == ""

    def test_an_ambiguous_agent_fact_falls_back_rather_than_letting_it_choose(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """TM-24 reaches the Cedar entity uid, so the fallback is a control, not cosmetics.

        `attenuate()` cannot build this block — `validate_label` refuses the value — so it is
        appended the way a third party would append one. Two `agent` facts is what a value
        that broke out of its own string literal renders as, and there is no sound way to
        pick between them; picking either would let the crafted block choose the entity a
        Cedar policy matches on.
        """
        from biscuit_auth import BlockBuilder

        service = self._service(monkeypatch, tmp_path)
        root, key_set = self._root()
        forged = root.biscuit.append(
            BlockBuilder('agent("real");\nagent("forged");\nrole("worker");\n')
        ).to_base64()

        principal = service.principal_for(verify(forged, key_set, now=self.NOW))
        assert principal.agent_id == "agt-depth-1"
        assert principal.agent_id not in ("real", "forged")

    def test_the_parent_asserted_role_travels_as_declared_role(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Spec 01 §6.1's use for `role`: the console and the audit trail."""
        service = self._service(monkeypatch, tmp_path)
        root, key_set = self._root()
        token = self._delegate(root, key_set, agent_id="agt-payer", role="payer")

        assert service.principal_for(token).declared_role == "payer"

    def test_the_cedar_role_is_configuration_not_the_token_s_claim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ADR-057, and the whole reason `declared_role` is a separate field.

        The corpus bundle grants `invoice:write` on `principal.role == "senior"` and forbids
        critical resources without it. If `principal.role` came from the attenuation block,
        any agent that can attenuate could name its own child `"senior"` and pass both — the
        `declared_depth` mistake (ADR-005) one field over.
        """
        service = self._service(monkeypatch, tmp_path)
        root, key_set = self._root()
        self_promoted = self._delegate(root, key_set, agent_id="agt-sneaky", role="senior")

        principal = service.principal_for(self_promoted)
        assert principal.declared_role == "senior"
        assert principal.role == "agent"
        assert principal.role != principal.declared_role

    def test_the_pipeline_is_given_a_caveat_reader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Spec 09 §4's stated limitation, closed.

        `decide()` takes the caveat list as an input because a `VerifiedToken` exposes the
        grant and not what later blocks added. A deployed PEP had nothing to pass, so
        `failing_caveat` could never name the caveat that refused a request.
        """
        from agentiam_core.models import ScopeSubset

        service = self._service(monkeypatch, tmp_path)
        root, key_set = self._root()
        token = self._delegate(root, key_set, agent_id="agt-payer", role="payer")

        recovered = service.pipeline._caveats_for(token)
        assert [sorted(c.scopes) for c in recovered if isinstance(c, ScopeSubset)] == [
            ["invoice:read"]
        ]
