"""The activation gate — T-026's core acceptance criterion.

*A bundle cannot be activated while any attached policy test fails (409).*

`activate_bundle` is the gated path for operator-initiated bundle changes. It runs
all verification gates (signature, serial, parse) *and* the policy test corpus before
making the bundle current. If any test fails, the bundle is refused with a 409 and the
previously loaded bundle keeps serving — the same defence `PolicyCache.load` gives for
bad signatures, extended to bad policy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentiam_core.bundles import PolicyBundle, sign_bundle
from agentiam_core.models import BudgetDimension, RequestContext
from agentiam_core.policy_testing import PolicyTestCase
from agentiam_pep.activation import ActivationFailed, activate_bundle
from agentiam_pep.policy import AgentPrincipal, ToolFacts
from agentiam_pep.policy_cache import BundleRejected, PolicyCache

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

#: The "real" policy from T-024's conformance corpus — same source the demo uses.
PERMIT_SOURCE = """\
permit(principal, action == Action::"invoice:read", resource);
permit(principal, action == Action::"vendor:read", resource);

permit(principal, action == Action::"invoice:write", resource)
when { principal.role == "senior" };

permit(principal, action == Action::"payment:initiate", resource)
when {
  context.amount.lessThanOrEqual(decimal("500000.0")) && principal.depth <= 2
};

permit(principal, action == Action::"email:send", resource)
when { !resource.is_external };

forbid(principal, action, resource)
when { resource.sensitivity == "critical" && principal.role != "senior" };

forbid(principal, action == Action::"payment:initiate", resource)
when { decimal("1000000.0").lessThan(context.amount) };
"""

FORBID_ALL = "forbid(principal, action, resource);"

TOOLS = {
    "invoice_api": ToolFacts(tool_id="invoice_api", server="erp", sensitivity="low"),
    "vendor_api": ToolFacts(tool_id="vendor_api", server="erp", sensitivity="low"),
    "payment_api": ToolFacts(
        tool_id="payment_api", server="bank", sensitivity="critical", is_external=True
    ),
    "email_internal": ToolFacts(tool_id="email_internal", server="mail", sensitivity="low"),
    "email_external": ToolFacts(
        tool_id="email_external", server="mail", sensitivity="medium", is_external=True
    ),
}


@pytest.fixture(scope="module")
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def a_bundle(serial: int = 1, source: str = PERMIT_SOURCE, version: str = "v1") -> PolicyBundle:
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
    return AgentPrincipal(
        agent_id="agt-1", role="worker", principal_id="kc:alice", task_id=uuid.uuid4()
    )


def ctx(operation: str = "invoice:read") -> RequestContext:
    return RequestContext(
        operation=operation,
        requested=dict.fromkeys(BudgetDimension, Decimal(0)),
        current_depth=1,
        request_intent="a" * 64,
        now=NOW,
        tool="invoice_api",
        args={},
    )


# -- Cases that match the PERMIT_SOURCE bundle ---------------------------------

PASSING_CASES = [
    PolicyTestCase(
        name="worker_reads_invoices",
        description="unconditional permit",
        operation="invoice:read",
        expected=True,
    ),
    PolicyTestCase(
        name="worker_reads_vendors",
        description="unconditional permit",
        operation="vendor:read",
        expected=True,
    ),
    PolicyTestCase(
        name="worker_cannot_admin",
        description="no permit mentions admin:write",
        operation="admin:write",
        expected=False,
    ),
]

FAILING_CASE = PolicyTestCase(
    name="should_fail",
    description="deliberately wrong expectation to test the gate",
    operation="invoice:read",
    expected=False,  # wrong — invoice:read IS permitted for a worker
)


class TestActivationGateAcceptsCriterion:
    """The acceptance criterion: a bundle cannot be activated while any test fails (409)."""

    def test_activation_succeeds_when_all_tests_pass(
        self, key: Ed25519PrivateKey
    ) -> None:
        cache, _ = a_cache(key)
        bundle = a_bundle()
        sig = sign_bundle(bundle, key)
        summary = activate_bundle(cache, bundle, sig, PASSING_CASES)
        assert summary.all_passed
        assert cache.serial == 1

    def test_activation_fails_when_any_test_fails(self, key: Ed25519PrivateKey) -> None:
        """The core acceptance criterion. 409 on failure."""
        cache, _ = a_cache(key)
        bundle = a_bundle()
        sig = sign_bundle(bundle, key)
        cases = [*PASSING_CASES, FAILING_CASE]
        with pytest.raises(ActivationFailed) as exc_info:
            activate_bundle(cache, bundle, sig, cases)
        assert exc_info.value.summary.failed == 1
        assert not exc_info.value.summary.all_passed

    def test_activation_failure_does_not_change_the_current_bundle(
        self, key: Ed25519PrivateKey
    ) -> None:
        """The defence: a bad policy cannot become the current one via the gate."""
        cache, _ = a_cache(key)
        # Load a good bundle first via the ungated path
        good = a_bundle(serial=1, source=PERMIT_SOURCE)
        cache.load(good, sign_bundle(good, key))
        assert cache.serial == 1

        # Attempt to activate a second bundle with a failing test
        bad = a_bundle(serial=2, source=PERMIT_SOURCE)
        cases = [*PASSING_CASES, FAILING_CASE]
        with pytest.raises(ActivationFailed):
            activate_bundle(cache, bad, sign_bundle(bad, key), cases)

        # The first bundle must still be current
        assert cache.serial == 1
        assert cache.bound(a_principal()).evaluate(ctx()).allowed

    def test_activation_failure_reports_which_tests_failed(
        self, key: Ed25519PrivateKey
    ) -> None:
        cache, _ = a_cache(key)
        bundle = a_bundle()
        sig = sign_bundle(bundle, key)
        cases = [*PASSING_CASES, FAILING_CASE]
        with pytest.raises(ActivationFailed) as exc_info:
            activate_bundle(cache, bundle, sig, cases)
        failures = exc_info.value.summary.failures
        assert len(failures) == 1
        assert failures[0].case.name == "should_fail"


class TestActivationGateStillEnforcesVerification:
    """The gate adds tests; it does not remove the existing guards."""

    def test_forged_signature_rejected(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        bundle = a_bundle()
        other = Ed25519PrivateKey.generate()
        with pytest.raises(BundleRejected):
            activate_bundle(cache, bundle, sign_bundle(bundle, other), PASSING_CASES)

    def test_rollback_rejected(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        first = a_bundle(serial=5)
        cache.load(first, sign_bundle(first, key))

        old = a_bundle(serial=4)
        with pytest.raises(BundleRejected, match="rollback"):
            activate_bundle(cache, old, sign_bundle(old, key), PASSING_CASES)

    def test_unparseable_cedar_rejected(self, key: Ed25519PrivateKey) -> None:
        cache, _ = a_cache(key)
        bundle = a_bundle(source="this is not cedar at all")
        with pytest.raises(BundleRejected, match="parse"):
            activate_bundle(cache, bundle, sign_bundle(bundle, key), PASSING_CASES)


class TestActivationEdgeCases:
    """Boundaries that matter for real deployment."""

    def test_empty_corpus_activates_with_warning(self, key: Ed25519PrivateKey) -> None:
        """Zero tests → none fail → bundle activates. `total=0` is the warning."""
        cache, _ = a_cache(key)
        bundle = a_bundle()
        sig = sign_bundle(bundle, key)
        summary = activate_bundle(cache, bundle, sig, [])
        assert summary.all_passed
        assert summary.total == 0
        assert cache.serial == 1

    def test_load_still_works_without_tests(self, key: Ed25519PrivateKey) -> None:
        """Existing `load()` is not affected — backwards compatible."""
        cache, _ = a_cache(key)
        bundle = a_bundle()
        cache.load(bundle, sign_bundle(bundle, key))
        assert cache.serial == 1

    def test_activation_with_forbid_all_bundle(self, key: Ed25519PrivateKey) -> None:
        """A bundle that denies everything should still activate if the tests expect that."""
        deny_cases = [
            PolicyTestCase(
                name="everything_denied",
                description="forbid-all bundle denies invoice:read",
                operation="invoice:read",
                expected=False,
            ),
        ]
        cache, _ = a_cache(key)
        bundle = a_bundle(source=FORBID_ALL)
        sig = sign_bundle(bundle, key)
        summary = activate_bundle(cache, bundle, sig, deny_cases)
        assert summary.all_passed
