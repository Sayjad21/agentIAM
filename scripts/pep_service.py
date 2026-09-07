"""The PEP as a deployable service — T-056, `PLAN.md` §9 T-056.

This is the composition root a container runs. It is not `serve_pep.py`, and the split is
deliberate rather than duplication for its own sake:

* **`serve_pep.py` (T-053)** is the load-test harness. It generates an ephemeral root
  keypair per run, mints a mandate, seeds a budget row, hardcodes a two-line policy and a
  pool sized so a 500 RPS run cannot exhaust it. Its own docstring calls itself *"the shape
  T-056's deployment artifacts will want"* — the shape, not the thing — and the numbers in
  `docs/benchmarks/performance.md` depend on it staying exactly as it is.
* **This file** takes every one of those from configuration, seeds nothing, mints nothing,
  and refuses to start if anything it needs to enforce with is absent.

**Why `scripts/` and not `agentiam_pep`.** An assembled PEP needs the ledger
(`ACQUIRE`/`RELEASE`), the audit sink and the settlement sink — all three in
`agentiam_controlplane.db`. The `agentiam-pep` *package* deliberately never imports
`agentiam_controlplane`: every mention inside it is a docstring, and the sinks are declared
as structural `Protocol`s precisely so the two stay independent deployables (ADR-043 pt 4,
ADR-051 pt 4). Adding the dependency to move this inside the package would invert the
architecture. A composition root is the one component allowed to know about both, so it
lives at the repository layer.

**What this wires that nothing had wired before.** Checked against the running code rather
than assumed:

* `RedisRevocationSet` (T-038/T-039) had **never been constructed outside a test**. Both
  reference assemblies — `serve_pep.py` and `tests/chaos/pepstack.py` — use
  `InMemoryRevocationSet()`, which never revokes anything. So the push/pull consumer and
  the Bloom filter measured for NFR-4 had never run in an assembled PEP. **Required**,
  not defaulted: an in-memory revocation set is a PEP that cannot be told to stop
  trusting a stolen token, which is INV-10 enforced by nothing.
* `RuleBasedDriftOracle` (T-032/T-036) had been wired only by a chaos-test helper.
  **Optional**, not required, and for a different reason than revocation: `decide()`'s
  own contract is that `drift=None` means *no assessment*, not a failure (spec 06 §2.1 —
  an oracle failure is advisory, never fatal, which is the opposite posture from
  revocation). Configuring `AGENTIAM_PEP_OLLAMA_URL` wires a real
  `RuleBasedDriftOracle`; leaving it unset is a legitimate, safe configuration and
  `Service.drift_oracle` reports which one is in effect rather than leaving it to be
  inferred.

**Policy is a signed bundle read from disk, verified, and used directly** — not through
`PolicyCache`. That looks like the wrong choice and is not. `PolicyCache` adds staleness
(`POLICY_BUNDLE_STALE`), rollback protection and hot reload, all of which need something
to *publish* a newer bundle; no such service exists (ADR-039 — the console's
`DummyBundleStore` is still a stub). Wired against a file loaded once at boot, its
staleness clock would fire after `max_staleness` (300 s by default) and the PEP would begin
refusing every request five minutes after starting. The signature check is what actually
carries T-025's guarantee here, and it is applied directly. See ADR-056; the consequence —
that `POLICY_BUNDLE_STALE` is unreachable in this configuration — is recorded as a gap
rather than left for a reader of spec 09 §7 to discover.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

# `python scripts/pep_service.py` puts `scripts/` at sys.path[0], not the repo root;
# pytest's `pythonpath = ["."]` only applies under pytest. Same bootstrap as
# `generate_evidence_pack.py`, and for the same reason.
_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from biscuit_auth import PublicKey
    from fastapi import FastAPI

    from agentiam_core.decision import DriftOracle
    from agentiam_core.tokens import VerifiedToken
    from agentiam_pep.config import PepSettings
    from agentiam_pep.pipeline import Pipeline
    from agentiam_pep.policy import AgentPrincipal, CedarEngine
    from agentiam_pep.pool import LeasePool

logger = logging.getLogger(__name__)

ENV_PREFIX: Final = "AGENTIAM_PEP_"

#: Lease sizing. Spec 04 §12's defaults; adaptive sizing is T-015 and deferred.
DEFAULT_LEASE_SIZE: Final = Decimal("5000.0000")
DEFAULT_LEASE_TTL_S: Final = 60.0
#: A *fraction* of the lease, not an absolute amount — `PoolSettings` rejects anything
#: outside [0, 1). Measured against the real constructor rather than assumed.
DEFAULT_LOW_WATER: Final = Decimal("0.25")


class ServiceConfigError(RuntimeError):
    """The service cannot be assembled from the configuration it was given."""


def _require(name: str) -> str:
    """Read a required variable, naming it in the error so a container log is actionable."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required and is unset or empty")
    return value


def _decimal_env(name: str, default: Decimal) -> Decimal:
    """Read an optional positive-decimal setting, refusing anything that is not one.

    Unset means the default. Set-but-unparseable is an error rather than a silent fall
    back to the default: a typo in a lease size would otherwise take effect as "the number
    you did not write", and the whole point of this module is that a misconfigured PEP
    refuses to start rather than looking like it enforces.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except ArithmeticError as exc:
        raise ValueError(f"{name} is not a decimal number: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero, got {raw!r}")
    return value


def _mandate_id() -> uuid.UUID:
    """Parse `AGENTIAM_PEP_MANDATE_ID`, refusing anything that is not a UUID."""
    raw = _require(f"{ENV_PREFIX}MANDATE_ID")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError(f"{ENV_PREFIX}MANDATE_ID is not a valid UUID: {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """Everything the deployed PEP needs, all of it from the environment.

    Nothing here has a fallback that would let the service start in a state where it
    looks like it is enforcing and is not. That is the whole difference between this and
    the load-test harness.
    """

    pep: PepSettings
    pep_id: str
    database_url: str
    redis_url: str
    #: Where the pull backstop reads `GET /v1/revocations?since=seq`. Required, not
    #: optional: spec 07 §5.2 makes pull the *correctness* path and push the latency
    #: optimisation — "a deployment could delete the Redis channel entirely and still be
    #: correct, only slower. The reverse is not true."
    control_plane_url: str
    #: **The PEP is scoped to one mandate.** `LeasePool` binds a `mandate_id` at
    #: construction and `Pipeline` reserves without one, so a single process enforces
    #: budget for exactly one mandate. Measured against the constructors, not assumed.
    #: Everything else the PEP does — verification, revocation, policy, drift, audit — is
    #: mandate-agnostic; only the lease pool is bound. Stated as a known limitation in
    #: ADR-056 and `STATUS.md` rather than hidden behind a plausible default.
    mandate_id: uuid.UUID
    root_public_keys_hex: tuple[str, ...]
    policy_bundle_path: Path
    policy_bundle_sig_path: Path
    policy_public_key_hex: str
    routes_path: Path
    lease_size: Decimal = DEFAULT_LEASE_SIZE
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S
    low_water: Decimal = DEFAULT_LOW_WATER
    #: What `principal.role` a Cedar policy sees. Configuration, deliberately, and *not* the
    #: `role` fact in the token's attenuation block — that one is written by the delegating
    #: parent, and spec 01 §6.1 assigns it to the console and the audit trail. A Cedar bundle
    #: that grants on `principal.role` is asking what the *organization* says this agent is,
    #: and a delegating agent is not the organization. ADR-057; `principal_for` has the
    #: attack this closes. The parent's claim still reaches the console, as
    #: `AgentPrincipal.declared_role` and `DecisionRecord.role`.
    default_role: str = "agent"
    #: Where `RuleBasedDriftOracle` reaches an embedding model. `None` disables drift
    #: entirely — a legitimate configuration, not a degraded one (spec 06 §2.1).
    ollama_url: str | None = None

    @classmethod
    def from_env(cls) -> ServiceSettings:
        """Build from `AGENTIAM_PEP_*`.

        Raises:
            ValueError: Any required variable is unset, empty, or malformed. The message
                names the variable, because the first reader of it is a container log.
        """
        from agentiam_pep.config import PepSettings

        keys_raw = _require(f"{ENV_PREFIX}ROOT_PUBLIC_KEYS")
        keys = tuple(part.strip() for part in keys_raw.split(",") if part.strip())
        for key in keys:
            # Validated here rather than at first use: a malformed root key means no token
            # can ever verify, and finding that out on the first request is finding out
            # too late.
            if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
                raise ValueError(
                    f"{ENV_PREFIX}ROOT_PUBLIC_KEYS entry {key!r} is not 32 bytes of hex"
                )

        return cls(
            pep=PepSettings.from_env(),
            pep_id=os.environ.get(f"{ENV_PREFIX}ID", "").strip() or "pep-1",
            database_url=_require(f"{ENV_PREFIX}DATABASE_URL"),
            redis_url=_require(f"{ENV_PREFIX}REDIS_URL"),
            control_plane_url=_require(f"{ENV_PREFIX}CONTROL_PLANE_URL"),
            mandate_id=_mandate_id(),
            root_public_keys_hex=keys,
            policy_bundle_path=Path(_require(f"{ENV_PREFIX}POLICY_BUNDLE_PATH")),
            policy_bundle_sig_path=Path(_require(f"{ENV_PREFIX}POLICY_BUNDLE_SIG_PATH")),
            policy_public_key_hex=_require(f"{ENV_PREFIX}POLICY_PUBLIC_KEY"),
            routes_path=Path(_require(f"{ENV_PREFIX}ROUTES_PATH")),
            ollama_url=os.environ.get(f"{ENV_PREFIX}OLLAMA_URL", "").strip() or None,
            # Every other setting here reads an override and this one did not, so the
            # 5,000 default was the hard ceiling on any single payment a deployed PEP
            # could authorize — a request larger than the lease is refused with
            # LEASE_UNAVAILABLE and retrying never helps, because a top-up refills to
            # the lease size rather than to cover the request. Measured: three
            # attempts three seconds apart, all 429, against a 500,000 mandate.
            lease_size=_decimal_env(f"{ENV_PREFIX}LEASE_SIZE", DEFAULT_LEASE_SIZE),
        )


def load_policy(settings: ServiceSettings) -> CedarEngine:
    """Read the bundle, verify its signature, and build the engine.

    Raises:
        ServiceConfigError: The bundle is unreadable, its signature does not verify, or
            its Cedar does not parse. Every one of those is fail-closed at boot.
    """
    import json

    from agentiam_core.bundles import (
        BundleSignatureError,
        PolicyBundle,
        public_key_from_hex,
        verify_bundle,
    )
    from agentiam_pep.policy import CedarEngine, PolicyBundleError

    try:
        payload = json.loads(settings.policy_bundle_path.read_text(encoding="utf-8"))
        signature = settings.policy_bundle_sig_path.read_bytes()
    except OSError as exc:
        raise ServiceConfigError(f"cannot read the policy bundle: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ServiceConfigError(f"policy bundle is not valid JSON: {exc}") from exc

    bundle = PolicyBundle(
        version=payload["version"],
        cedar_source=payload["cedar_source"],
        serial=int(payload.get("serial", 0)),
        entity_schema=payload.get("entity_schema"),
    )

    try:
        verify_bundle(bundle, signature, public_key_from_hex(settings.policy_public_key_hex))
    except (BundleSignatureError, ValueError) as exc:
        # Deliberately not "load it anyway and warn": an unverified bundle is an
        # authorization layer anyone with disk access can rewrite.
        raise ServiceConfigError(f"policy bundle signature did not verify: {exc}") from exc

    try:
        return CedarEngine(bundle)
    except PolicyBundleError as exc:
        raise ServiceConfigError(f"policy bundle does not parse: {exc}") from exc


def _root_keys(settings: ServiceSettings) -> list[PublicKey]:
    """Decode the accepted root public keys, failing closed with an actionable message.

    `PublicKey.from_bytes` requires the algorithm explicitly — there is no `from_hex` —
    and it rejects any 32 bytes that are not a valid curve point. Measured against the
    installed `biscuit_auth`, not assumed. `from_env` already checked length and charset;
    this is where a well-formed-but-invalid key is caught, and it names the variable
    because the first reader of the error is a container log.
    """
    from biscuit_auth import Algorithm, PublicKey

    keys: list[PublicKey] = []
    for hexed in settings.root_public_keys_hex:
        try:
            # `biscuit-python`'s bundled stubs are stale: they declare
            # `from_bytes(cls, data)` and omit `Algorithm` entirely, while the runtime
            # requires the algorithm as a second argument (measured — a one-argument call
            # raises `TypeError: missing 1 required positional argument: 'alg'`). Same
            # stub-versus-runtime divergence ADR-021 records for `limits()`/`set_limits()`,
            # and handled the same way rather than by relaxing the strict-mypy config.
            keys.append(
                PublicKey.from_bytes(  # type: ignore[call-arg]
                    bytes.fromhex(hexed),
                    Algorithm.Ed25519,  # type: ignore[attr-defined]
                )
            )
        except ValueError as exc:
            raise ServiceConfigError(
                f"{ENV_PREFIX}ROOT_PUBLIC_KEYS entry {hexed!r} is not a valid "
                f"Ed25519 public key: {exc}"
            ) from exc
    return keys


@dataclass(frozen=True, slots=True)
class Service:
    """The assembled object graph, exposed so tests can assert on the parts."""

    app: FastAPI
    revocation: object
    policy: CedarEngine
    #: `None` when `AGENTIAM_PEP_OLLAMA_URL` is unset — a legitimate, safe configuration
    #: (spec 06 §2.1), not a degraded one. Exposed rather than left implicit so a caller
    #: (or a test) does not have to infer it from an absent constructor argument.
    drift_oracle: DriftOracle | None
    #: The lease pool, exposed so a test can assert the lifespan primes it. It was not,
    #: and nothing noticed: every budgeted request was refused with LEASE_UNAVAILABLE
    #: while `tests/unit/test_pep_service.py` stayed green, because the pool was
    #: unreachable from here.
    pool: LeasePool
    #: How a verified token becomes a policy principal. Exposed for the same reason as
    #: `pool`: while this was only a closure nothing outside `build_service` could see it,
    #: and it spent that time returning `agt-depth-{N}` for every agent at a depth — which
    #: the identity tree drew as one node per level instead of one per agent. A test that
    #: cannot reach the function cannot notice that.
    principal_for: Callable[[VerifiedToken], AgentPrincipal]
    #: The assembled pipeline. Exposed alongside `principal_for` for the same reason: what
    #: it was handed at construction — the caveat reader especially — is otherwise
    #: unobservable, and spec 09 §4's `failing_caveat` was empty for exactly as long.
    pipeline: Pipeline


def build_service(settings: ServiceSettings) -> Service:
    """Assemble the whole PEP from configuration. Performs no I/O against its dependencies.

    Constructing an engine or a Redis client does not connect, so this is safe to call in a
    test without a live Postgres or Redis; the connections happen inside the lifespan.
    """
    from contextlib import asynccontextmanager

    import httpx
    from redis.asyncio import Redis

    from agentiam_controlplane.db.audit_sink import LedgerAuditSink
    from agentiam_controlplane.db.base import make_engine, make_session_factory
    from agentiam_controlplane.db.ledger import acquire, release
    from agentiam_controlplane.db.settlement_sink import LedgerSettlementSink
    from agentiam_core.datalog import token_caveats, token_identity
    from agentiam_core.models import BudgetDimension
    from agentiam_core.tokens import RootKeySet
    from agentiam_pep.app import create_app
    from agentiam_pep.drift import RuleBasedDriftOracle
    from agentiam_pep.emitter import DecisionEmitter, EmitterSettings
    from agentiam_pep.extractor import RouteTable
    from agentiam_pep.pipeline import Pipeline, PipelineSettings
    from agentiam_pep.policy import AgentPrincipal
    from agentiam_pep.pool import LeaseGrant, LeasePool, PoolSettings
    from agentiam_pep.revocation import RedisRevocationSet
    from agentiam_pep.settlement import SettlementQueue, SettlementSettings

    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    policy = load_policy(settings)

    class Ledger:
        """The real `ACQUIRE`/`RELEASE` over the configured DSN — spec 04 §4.1, §4.5."""

        async def acquire(
            self,
            *,
            mandate_id: uuid.UUID,
            dimension: BudgetDimension,
            requested: Decimal,
            pep_id: str,
            ttl: timedelta,
            now: datetime,
        ) -> LeaseGrant | None:
            async with factory() as session:
                lease = await acquire(
                    session,
                    mandate_id=mandate_id,
                    dimension=dimension.value,
                    requested=requested,
                    pep_id=pep_id,
                    ttl=ttl,
                    now=now,
                )
            if lease is None or lease.granted <= 0:
                return None
            return LeaseGrant(id=lease.id, granted=lease.granted, expires_at=lease.expires_at)

        async def release(self, *, lease_id: uuid.UUID) -> None:
            async with factory() as session:
                await release(session, lease_id=lease_id)

    settlement = SettlementQueue(
        LedgerSettlementSink(factory), SettlementSettings(), now=lambda: datetime.now(UTC)
    )
    pool = LeasePool(
        Ledger(),
        PoolSettings(
            pep_id=settings.pep_id,
            lease_size=settings.lease_size,
            low_water=settings.low_water,
            ttl=timedelta(seconds=settings.lease_ttl_s),
        ),
        mandate_id=settings.mandate_id,
        now=lambda: datetime.now(UTC),
        before_release=settlement.drain,
    )
    emitter = DecisionEmitter(LedgerAuditSink(factory), EmitterSettings())

    revocation = RedisRevocationSet(
        redis_client=Redis.from_url(settings.redis_url),
        control_plane_client=httpx.AsyncClient(base_url=settings.control_plane_url),
    )

    drift_oracle = (
        RuleBasedDriftOracle(base_url=settings.ollama_url) if settings.ollama_url else None
    )

    def principal_for(token: VerifiedToken) -> AgentPrincipal:
        """Read the policy principal off the verified token.

        `agent_id` and `role` are not on `VerifiedToken` — they live in attenuation block
        facts (spec 01 §6.1), and block facts are invisible to the authorizer (spec 02 §9
        finding 13), so `block_source()` is the only route to them. `datalog.token_identity`
        is that route, and until it existed this returned `agt-depth-{N}`: every sibling at a
        depth shared one name, and the identity tree drew them as one node.

        **`role` still comes from configuration, and that is the point** (ADR-057). The
        block's `role` is written by the delegating parent, and spec 01 §6.1 gives it to "the
        console and audit". Cedar's `principal.role` is an authority attribute — the corpus
        bundle grants `invoice:write` on `role == "senior"` and forbids critical resources
        without it — so sourcing it from the block would let any agent that can attenuate
        name its own child `"senior"` and pass both guards. That is `declared_depth`'s
        mistake (ADR-005) one field over. The parent's claim travels as `declared_role`, onto
        the decision record and from there to the identity tree, where it belongs.

        `agent_id` falls back to the depth-derived name when the token declares none — a root
        token has no attenuation block, and a block whose `agent` fact was rendered
        ambiguously (TM-24) has none that can be believed. That fallback is not cosmetic: the
        alternative is letting a crafted block choose the Cedar entity uid.
        """
        identity = token_identity(token)
        return AgentPrincipal(
            agent_id=identity.agent_id or f"agt-depth-{token.depth}",
            role=settings.default_role,
            principal_id=token.principal_id,
            task_id=token.task_id,
            declared_role=identity.role or "",
        )

    pipeline = Pipeline(
        routes=RouteTable.from_file(settings.routes_path),
        key_set=RootKeySet(_root_keys(settings)),
        policy=policy,
        principal_for=principal_for,
        # Spec 09 §4's stated limitation, closed. `decide()` takes the caveat list as an
        # input because a `VerifiedToken` exposes the grant and not what later blocks added,
        # and a deployed PEP had nothing to pass — so `failing_caveat` was always `None`. An
        # incomplete list stays safe by construction: the caveat loop only ever adds a
        # denial, and biscuit's own authorizer enforces the chain regardless.
        caveats_for=token_caveats,
        pool=pool,
        emitter=emitter,
        revocation=revocation,
        settlement=settlement,
        settings=PipelineSettings(pep_id=settings.pep_id),
        now=lambda: datetime.now(UTC),
        drift_oracle=drift_oracle,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Start the background workers, then drain them in settle-before-release order.

        `@app.on_event` is deprecated in FastAPI 0.141; this is the supported replacement.
        The shutdown order is not arbitrary — settlement drains before the pool releases,
        because a lease retired while it still owes the ledger is ADR-049's second
        double-spend route.
        """
        await emitter.start()
        await settlement.start()
        await revocation.start()

        # Draw the first lease. Without this the pool holds no `_Held` for the dimension,
        # and `LeasePool.covers()` refuses *every* budgeted request with
        # LEASE_UNAVAILABLE — its own docstring names this case ("a PEP that never primed
        # the dimension, not one that ran dry"), and the refusal path cannot recover from
        # it because top-ups are scheduled only from an existing lease. The e2e slice
        # primed by hand (`tests/e2e/test_thin_slice.py`), so nothing caught that the
        # deployed service never did: measured against a live PEP, `leases` stayed empty
        # and every payment returned 429 while `budgets.committed` never left 0.
        #
        # Not fatal if it fails: an unreachable ledger at boot is the fail-closed case the
        # PEP already handles per request, and crash-looping here would take out the
        # read-only paths (`invoice:read` needs no budget) along with it.
        #
        # **`prime()` raises as well as returning False, and the difference is not
        # cosmetic.** `ACQUIRE` does `scalar_one()` on the budget row, so a mandate with no
        # row at all raises `NoResultFound`, and an exhausted pool raises
        # `LeaseUnavailableError`; neither the pool nor this module's ledger client catches
        # either. Handling only the `False` return turned both into a boot failure — and
        # `docker-compose.demo.yml` points the PEP at a *placeholder* mandate that has no
        # budget row by design ("this placeholder lets the container start and prove the
        # pipeline wiring"), so the container stopped starting and CI's demo-stack job
        # timed out waiting for it. Reproduced against a real Postgres before this line
        # existed.
        try:
            primed = await pool.prime(BudgetDimension.SPEND_BDT)
        except Exception:
            logger.warning(
                "could not acquire an initial %s lease (the mandate may have no budget row "
                "yet); budgeted requests will be refused until a top-up succeeds",
                BudgetDimension.SPEND_BDT.value,
                exc_info=True,
            )
        else:
            if not primed:
                logger.warning(
                    "could not acquire an initial %s lease; budgeted requests will be "
                    "refused until a top-up succeeds",
                    BudgetDimension.SPEND_BDT.value,
                )
        if drift_oracle is not None:
            # `EmbeddingClient.warm()` is synchronous and can take up to 60 s cold
            # (ADR-037 measured 14,244 ms for the embedding call alone) — run it off the
            # loop via `asyncio.to_thread` (ADR-012's established primitive for exactly
            # this) rather than block every other request during startup. A failed
            # warm-up is not fatal: `warm()` returns a bool and never raises, and an
            # unwarmed model just pays the cost on the first scored request instead
            # (spec 06 §2.1 — drift fails open, never the process).
            import asyncio

            await asyncio.to_thread(drift_oracle.embeddings.warm)
        try:
            yield
        finally:
            await revocation.aclose()
            await settlement.aclose()
            await pool.aclose()
            await emitter.aclose()
            await engine.dispose()

    app = create_app(settings=settings.pep, pipeline=pipeline, lifespan=lifespan)
    return Service(
        app=app,
        revocation=revocation,
        policy=policy,
        drift_oracle=drift_oracle,
        pool=pool,
        principal_for=principal_for,
        pipeline=pipeline,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without binding a port."""
    parser = argparse.ArgumentParser(
        prog="pep_service",
        description="Run the AgentIAM PEP as a deployable service (T-056).",
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104  # nosec B104
    parser.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Assemble from the environment and serve. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    import uvicorn

    try:
        settings = ServiceSettings.from_env()
        service = build_service(settings)
    except (ValueError, ServiceConfigError) as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(service.app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
