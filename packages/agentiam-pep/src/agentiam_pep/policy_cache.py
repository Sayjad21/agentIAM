"""The PEP's policy-bundle cache — spec 05 §5.2-§5.4, T-025.

Holds the bundle the PEP is currently enforcing, and decides what to do with a new one.

Three rules, and the second is the one that looks inconsistent:

1. **Verify before swapping.** A bundle is parsed and its signature checked before anything
   is replaced, so a request can never observe a half-loaded bundle.
2. **A rejected bundle leaves the previous one serving** (`PLAN.md` §11 EC-P01). Everything
   else in this system fails closed; this does not, because a forged bundle is evidence of an
   attack and discarding the last known good policy in response would let an attacker disable
   the policy layer by sending garbage. The attacker achieves nothing, and the staleness clock
   keeps running underneath — so if the real bundle never arrives, the PEP fails closed on age
   anyway.
3. **Rollback is refused on the serial, not the signature.** An old bundle is *correctly
   signed*; signature verification cannot see anything wrong with it. Only a monotonic serial
   can (spec 05 §5.2).

Hot reload is a single attribute rebind, which is atomic under the GIL. In-flight requests
finish against the engine they started with — the correct outcome rather than a compromise,
since a decision made under bundle *n* was made under bundle *n*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentiam_core.bundles import BundleSignatureError, verify_bundle
from agentiam_pep.errors import PepError
from agentiam_pep.policy import CedarEngine, PolicyBundleError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime, timedelta

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from agentiam_core.bundles import PolicyBundle
    from agentiam_pep.policy import AgentPrincipal, BoundCedarEngine, ToolFacts

__all__ = ["BundleRejected", "PolicyCache"]


class BundleRejected(PepError):
    """A candidate bundle was refused. The previously loaded one keeps serving."""


class PolicyCache:
    """The current bundle, its age, and the gate every candidate passes through."""

    def __init__(
        self,
        *,
        public_key: Ed25519PublicKey,
        tools: Mapping[str, ToolFacts] | None = None,
        max_staleness: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        """Start empty. Until a bundle loads, the engine reports itself unavailable."""
        self._public_key = public_key
        self._tools = dict(tools or {})
        self._max_staleness = max_staleness
        self._now = now
        self._engine: CedarEngine | None = None
        self._serial: int | None = None
        self._loaded_at: datetime | None = None
        self.rejected = 0
        self.loaded = 0

    # -- state --------------------------------------------------------------------

    @property
    def serial(self) -> int | None:
        """The serial currently enforced, or None if nothing has loaded."""
        return self._serial

    @property
    def stale(self) -> bool:
        """Whether the held bundle is past `max_staleness`.

        An empty cache is not *stale* — it is unavailable, which is a different denial with a
        different fix (fetch a bundle, versus refresh a stale one).
        """
        if self._loaded_at is None:
            return False
        return self._now() - self._loaded_at > self._max_staleness

    def status(self) -> dict[str, object]:
        """What `/readyz` reports. A bundle being refused repeatedly is EC-P01's alert."""
        return {
            "serial": self._serial,
            "loaded": self.loaded,
            "rejected": self.rejected,
            "stale": self.stale,
        }

    # -- loading ------------------------------------------------------------------

    def load(self, bundle: PolicyBundle, signature: bytes) -> None:
        """Verify a candidate and, if it passes every gate, make it current.

        Raises:
            BundleRejected: The signature does not verify, the serial does not advance, or
                the Cedar source does not parse. The previously loaded bundle keeps serving
                in every case.
        """
        try:
            verify_bundle(bundle, signature, self._public_key)
        except BundleSignatureError as exc:
            self.rejected += 1
            raise BundleRejected(str(exc)) from exc

        if self._serial is not None and bundle.serial <= self._serial:
            # A correctly-signed *old* bundle. This is the rollback attack, and the signature
            # is not evidence of anything wrong with it (spec 05 §5.2).
            self.rejected += 1
            raise BundleRejected(
                f"bundle serial {bundle.serial} does not advance past the current "
                f"{self._serial}; refusing a rollback"
            )

        try:
            engine = CedarEngine(bundle, tools=self._tools)
        except PolicyBundleError as exc:
            self.rejected += 1
            raise BundleRejected(str(exc)) from exc

        # Everything that can fail has failed by here, so the swap cannot leave a half-loaded
        # bundle visible. One rebind, atomic under the GIL.
        self._engine = engine
        self._serial = bundle.serial
        self._loaded_at = self._now()
        self.loaded += 1

    # -- serving ------------------------------------------------------------------

    def bound(self, principal: AgentPrincipal) -> BoundCedarEngine:
        """A `PolicyEngine` over the current bundle, for one agent.

        An empty cache yields an engine that raises `OracleUnavailable`, which `decide()`
        turns into `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` — a policy layer that cannot be
        consulted must not be assumed permissive.
        """
        engine = self._engine
        if engine is None:
            return CedarEngine.unavailable("no policy bundle has been loaded").bound(principal)
        if self.stale:
            # Rebuilt rather than mutated: `CedarEngine` is shared by every in-flight request,
            # and flipping a flag on it would change the answer under requests already running.
            stale_engine = CedarEngine(engine.bundle, tools=self._tools, stale=True)
            return stale_engine.bound(principal)
        return engine.bound(principal)
