"""The policy-bundle cache — spec 05 §5.2-§5.4, T-025.

Four behaviours, and two of them are counterintuitive enough to be the reason this file is
long:

* `TestRollback` — an old bundle is *correctly signed*. Signature verification cannot see
  anything wrong with it; only a monotonic serial can.
* `TestARejectedBundleKeepsTheOldOne` — everything else in this system fails closed and this
  does not, deliberately. Discarding the last known good policy when handed garbage would let
  an attacker disable the policy layer by sending garbage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentiam_core.bundles import PolicyBundle, public_key_to_hex, sign_bundle
from agentiam_core.decision import OracleUnavailable
from agentiam_core.models import BudgetDimension, RequestContext
from agentiam_pep.policy import AgentPrincipal, ToolFacts
from agentiam_pep.policy_cache import BundleRejected, PolicyCache

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
PERMIT = 'permit(principal, action == Action::"invoice:read", resource);'
FORBID = "forbid(principal, action, resource);"

TOOLS = {"invoice_api": ToolFacts(tool_id="invoice_api", sensitivity="low")}


@pytest.fixture(scope="module")
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def a_bundle(serial: int = 1, source: str = PERMIT, version: str = "v1") -> PolicyBundle:
    return PolicyBundle(version=version, cedar_source=source, serial=serial, created_at=NOW)


class Clock:
    def __init__(self) -> None:
        """Start at NOW; tests move it forward explicitly."""
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t


def a_cache(
    key: Ed25519PrivateKey, *, max_staleness: timedelta | None = None
) -> tuple[PolicyCache, Clock]:
    clock = Clock()
    cache = PolicyCache(
        public_key=key.public_key(),
        tools=TOOLS,
        max_staleness=max_staleness or timedelta(seconds=300),
        now=clock,
    )
    return cache, clock


def a_principal() -> AgentPrincipal:
    import uuid

    return AgentPrincipal(
        agent_id="agt-1", role="worker", principal_id="kc:alice", task_id=uuid.uuid4()
    )


def ctx(operation: str = "invoice:read") -> RequestContext:
    from decimal import Decimal

    return RequestContext(
        operation=operation,
        requested=dict.fromkeys(BudgetDimension, Decimal(0)),
        current_depth=1,
        request_intent="a" * 64,
        now=NOW,
        tool="invoice_api",
        args={},
    )


class TestLoading:
    def test_a_signed_bundle_loads(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        bundle = a_bundle()
        cache.load(bundle, sign_bundle(bundle, key))
        assert cache.serial == 1
        assert cache.bound(a_principal()).evaluate(ctx()).allowed

    def test_an_unsigned_bundle_is_rejected(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        with pytest.raises(BundleRejected):
            cache.load(a_bundle(), b"")

    def test_a_badly_signed_bundle_is_rejected(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        other = Ed25519PrivateKey.generate()
        bundle = a_bundle()
        with pytest.raises(BundleRejected):
            cache.load(bundle, sign_bundle(bundle, other))

    def test_a_bundle_that_does_not_parse_is_rejected(self, key: Ed25519PrivateKey) -> None:
        """Correctly signed nonsense is still nonsense, and must not become the current one."""
        cache, _ = a_cache(key)
        bundle = a_bundle(source="this is not cedar")
        with pytest.raises(BundleRejected, match="parse"):
            cache.load(bundle, sign_bundle(bundle, key))

    def test_rejections_are_counted(self, key: Ed25519PrivateKey) -> None:
        """EC-P01 asks for an alert, and a counter is what an alert reads."""
        cache, _ = a_cache(key)
        for _ in range(3):
            with pytest.raises(BundleRejected):
                cache.load(a_bundle(), b"")
        assert cache.status()["rejected"] == 3


class TestARejectedBundleKeepsTheOldOne:
    """`PLAN.md` §11 EC-P01 — the one place this system does not fail closed."""

    def test_a_forged_replacement_does_not_disturb_the_current_bundle(
        self, key: Ed25519PrivateKey
    ) -> None:
        cache, _ = a_cache(key)
        good = a_bundle(serial=1, source=PERMIT)
        cache.load(good, sign_bundle(good, key))

        forged = a_bundle(serial=2, source=FORBID)
        other = Ed25519PrivateKey.generate()
        with pytest.raises(BundleRejected):
            cache.load(forged, sign_bundle(forged, other))

        assert cache.serial == 1
        assert cache.bound(a_principal()).evaluate(ctx()).allowed, (
            "the previously loaded bundle must keep serving"
        )

    def test_an_attacker_cannot_disable_policy_by_sending_garbage(
        self, key: Ed25519PrivateKey
    ) -> None:
        """The argument for that behaviour, stated as a test.

        If a rejected bundle emptied the cache, a stream of garbage would take the policy
        layer down — which is a cheaper attack than forging a signature.
        """
        cache, _ = a_cache(key)
        good = a_bundle()
        cache.load(good, sign_bundle(good, key))

        for _ in range(10):
            with pytest.raises(BundleRejected):
                cache.load(a_bundle(serial=99), b"\x00" * 64)

        assert cache.bound(a_principal()).evaluate(ctx()).allowed


class TestRollback:
    """Spec 05 §5.2 — the attack a valid signature cannot detect."""

    def test_an_older_serial_is_refused_even_though_it_verifies(
        self, key: Ed25519PrivateKey
    ) -> None:
        cache, _ = a_cache(key)
        new = a_bundle(serial=5, source=FORBID, version="v5")
        cache.load(new, sign_bundle(new, key))

        old = a_bundle(serial=4, source=PERMIT, version="v4")
        with pytest.raises(BundleRejected, match="rollback"):
            cache.load(old, sign_bundle(old, key))  # a genuine signature, genuinely old

        assert cache.serial == 5

    def test_the_same_serial_is_refused(self, key: Ed25519PrivateKey) -> None:
        """Equal is not an advance. Re-publishing under one serial would swap policy silently."""
        cache, _ = a_cache(key)
        first = a_bundle(serial=3, source=FORBID)
        cache.load(first, sign_bundle(first, key))

        second = a_bundle(serial=3, source=PERMIT)
        with pytest.raises(BundleRejected):
            cache.load(second, sign_bundle(second, key))

    def test_a_higher_serial_is_accepted(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        for serial in (1, 2, 7):
            bundle = a_bundle(serial=serial)
            cache.load(bundle, sign_bundle(bundle, key))
        assert cache.serial == 7

    def test_serials_need_not_be_consecutive(self, key: Ed25519PrivateKey) -> None:
        """Only monotonic. A gap means a bundle was published and never reached this PEP."""
        cache, _ = a_cache(key)
        first, second = a_bundle(serial=1), a_bundle(serial=1000)
        cache.load(first, sign_bundle(first, key))
        cache.load(second, sign_bundle(second, key))
        assert cache.serial == 1000

    def test_the_label_does_not_order_anything(self, key: Ed25519PrivateKey) -> None:
        """`"v10" < "v9"` lexicographically, which is why the serial exists (spec 05 §5.2)."""
        assert "v10" < "v9"
        cache, _ = a_cache(key)
        first = a_bundle(serial=9, version="v9")
        cache.load(first, sign_bundle(first, key))
        second = a_bundle(serial=10, version="v10")
        cache.load(second, sign_bundle(second, key))
        assert cache.serial == 10


class TestStaleness:
    def test_a_fresh_bundle_is_not_stale(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        bundle = a_bundle()
        cache.load(bundle, sign_bundle(bundle, key))
        assert not cache.stale
        assert not cache.bound(a_principal()).evaluate(ctx()).stale

    def test_past_max_staleness_the_verdict_says_so(self, key: Ed25519PrivateKey) -> None:
        """Spec 09 turns this into POLICY_BUNDLE_STALE, its own code with its own fix."""
        cache, clock = a_cache(key, max_staleness=timedelta(seconds=300))
        bundle = a_bundle()
        cache.load(bundle, sign_bundle(bundle, key))

        clock.t = NOW + timedelta(seconds=301)

        assert cache.stale
        assert cache.bound(a_principal()).evaluate(ctx()).stale

    def test_the_boundary_is_exclusive(self, key: Ed25519PrivateKey) -> None:
        cache, clock = a_cache(key, max_staleness=timedelta(seconds=300))
        bundle = a_bundle()
        cache.load(bundle, sign_bundle(bundle, key))
        clock.t = NOW + timedelta(seconds=300)
        assert not cache.stale

    def test_reloading_resets_the_clock(self, key: Ed25519PrivateKey) -> None:
        cache, clock = a_cache(key, max_staleness=timedelta(seconds=300))
        first = a_bundle(serial=1)
        cache.load(first, sign_bundle(first, key))

        clock.t = NOW + timedelta(seconds=299)
        second = a_bundle(serial=2)
        cache.load(second, sign_bundle(second, key))

        clock.t = NOW + timedelta(seconds=400)
        assert not cache.stale, "the second load should have restarted the staleness window"

    def test_an_empty_cache_is_unavailable_not_stale(self, key: Ed25519PrivateKey) -> None:
        """Different denials with different fixes: fetch a bundle, versus refresh a stale one."""
        cache, _ = a_cache(key)
        assert not cache.stale
        with pytest.raises(OracleUnavailable):
            cache.bound(a_principal()).evaluate(ctx())

    def test_a_stale_engine_does_not_mutate_the_shared_one(self, key: Ed25519PrivateKey) -> None:
        """Flipping a flag on the shared engine would change the answer under in-flight requests."""
        cache, clock = a_cache(key, max_staleness=timedelta(seconds=10))
        bundle = a_bundle()
        cache.load(bundle, sign_bundle(bundle, key))

        clock.t = NOW + timedelta(seconds=11)
        assert cache.bound(a_principal()).evaluate(ctx()).stale

        clock.t = NOW
        assert not cache.bound(a_principal()).evaluate(ctx()).stale


class TestHotReload:
    def test_a_new_bundle_changes_the_verdict(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        permit = a_bundle(serial=1, source=PERMIT)
        cache.load(permit, sign_bundle(permit, key))
        assert cache.bound(a_principal()).evaluate(ctx()).allowed

        forbid = a_bundle(serial=2, source=FORBID)
        cache.load(forbid, sign_bundle(forbid, key))
        assert not cache.bound(a_principal()).evaluate(ctx()).allowed

    def test_an_engine_taken_before_the_swap_keeps_its_answer(self, key: Ed25519PrivateKey) -> None:
        """An in-flight request finishes against the bundle it started with.

        The correct outcome rather than a compromise: a decision made under bundle *n* was in
        fact made under bundle *n*, and the record says so.
        """
        cache, _ = a_cache(key)
        permit = a_bundle(serial=1, source=PERMIT)
        cache.load(permit, sign_bundle(permit, key))
        in_flight = cache.bound(a_principal())

        forbid = a_bundle(serial=2, source=FORBID)
        cache.load(forbid, sign_bundle(forbid, key))

        assert in_flight.evaluate(ctx()).allowed, "the in-flight engine changed underneath"
        assert not cache.bound(a_principal()).evaluate(ctx()).allowed

    def test_the_version_label_reaches_the_verdict(self, key: Ed25519PrivateKey) -> None:
        """It is what lands in `DecisionRecord.policy_version`."""
        cache, _ = a_cache(key)
        bundle = a_bundle(version="2026-08-15.3")
        cache.load(bundle, sign_bundle(bundle, key))
        assert cache.bound(a_principal()).evaluate(ctx()).version == "2026-08-15.3"


class TestStatus:
    def test_status_reports_what_readyz_needs(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        bundle = a_bundle(serial=4)
        cache.load(bundle, sign_bundle(bundle, key))
        with pytest.raises(BundleRejected):
            cache.load(a_bundle(serial=1), sign_bundle(a_bundle(serial=1), key))

        status = cache.status()
        assert status == {"serial": 4, "loaded": 1, "rejected": 1, "stale": False}

    def test_an_empty_cache_reports_no_serial(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        assert cache.status()["serial"] is None


class TestKeyHelpers:
    def test_the_configured_key_is_the_one_that_verifies(self, key: Ed25519PrivateKey) -> None:
        """The path an operator actually takes: paste hex into config, load a bundle."""
        from agentiam_core.bundles import public_key_from_hex

        text = public_key_to_hex(key.public_key())
        cache = PolicyCache(
            public_key=public_key_from_hex(text),
            tools=TOOLS,
            max_staleness=timedelta(seconds=300),
            now=lambda: NOW,
        )
        bundle = a_bundle()
        cache.load(bundle, sign_bundle(bundle, key))
        assert cache.serial == 1
