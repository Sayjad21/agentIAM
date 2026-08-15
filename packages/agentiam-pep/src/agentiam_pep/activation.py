"""The activation gate — spec 05 §5.5, T-026.

`activate_bundle` is the gated path for operator-initiated bundle changes. It runs
the standard verification chain (`PolicyCache.load`) *and* the policy test corpus
before making the bundle current. If any test fails, the bundle is refused with
`ActivationFailed` — which the API layer maps to HTTP 409.

Why a separate module rather than a method on `PolicyCache`:

* `PolicyCache`'s job is **serving** policy: holding the current engine, checking
  staleness, binding a principal. The activation gate's job is **testing** a candidate.
  Mixing them puts the test runner on the hot-path import, and a cache that also runs
  tests is a cache with a reason to break that has nothing to do with serving.
* `load()` stays the fast path for PEP-side hot reload where tests have already been
  run at publish time. `activate_bundle()` is the slow path for human/operator-initiated
  changes. Two callers with two performance budgets belong in two entry points.

ADR-030 documents this split.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from agentiam_core.models import BudgetDimension, RequestContext
from agentiam_core.policy_testing import PolicyTestSummary, run_policy_tests, summarize
from agentiam_pep.errors import PepError
from agentiam_pep.policy import AgentPrincipal, CedarEngine, PolicyBundleError
from agentiam_pep.policy_cache import BundleRejected

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from agentiam_core.bundles import PolicyBundle
    from agentiam_core.decision import PolicyVerdict
    from agentiam_core.policy_testing import PolicyTestCase
    from agentiam_pep.policy_cache import PolicyCache

__all__ = ["ActivationFailed", "activate_bundle"]


class ActivationFailed(PepError):
    """A candidate bundle passed verification but failed its policy test corpus.

    The bundle's signature verified, its serial advanced, and its Cedar parsed — but
    at least one test case produced the wrong verdict. The previously loaded bundle
    keeps serving.

    The API layer maps this to HTTP 409: the client's request conflicts with the
    system's correctness requirement. 400 would imply a malformed request; 403 would
    imply an authorization failure. 409 says *the resource cannot transition to this
    state*, which is precisely what a failing test corpus means.
    """

    def __init__(self, summary: PolicyTestSummary, *, version: str) -> None:
        """Build the error with the full test summary for the failure report."""
        failed_names = [r.case.name for r in summary.failures]
        super().__init__(
            f"bundle {version!r} failed {summary.failed} of {summary.total} policy "
            f"tests: {', '.join(failed_names)}"
        )
        self.summary = summary


def _evaluate_case(
    engine: CedarEngine,
    case: PolicyTestCase,
    now: datetime,
) -> PolicyVerdict:
    """Build a `RequestContext` from a test case and evaluate it against the engine.

    Binds a principal from the case's fields, constructs the context, and calls the
    Cedar engine. This is the adapter between the pure `run_policy_tests` and the
    Cedar-specific `BoundCedarEngine.evaluate`.
    """
    principal = AgentPrincipal(
        agent_id=case.agent_id,
        role=case.role,
        principal_id=case.principal_id,
        task_id=uuid.UUID(case.task_id),
    )
    requested = dict.fromkeys(BudgetDimension, Decimal(0))
    requested[BudgetDimension.SPEND_BDT] = case.amount
    context = RequestContext(
        operation=case.operation,
        requested=requested,
        current_depth=case.depth,
        request_intent="a" * 64,  # dummy — policy tests do not check intent
        now=now,
        tool=case.tool,
        args={},
    )
    bound = engine.bound(principal)
    return bound.evaluate(context)


def activate_bundle(
    cache: PolicyCache,
    bundle: PolicyBundle,
    signature: bytes,
    test_cases: Sequence[PolicyTestCase],
) -> PolicyTestSummary:
    """Verify, test, then activate a bundle — or refuse with the full failure report.

    The verification chain is the same as `PolicyCache.load`: signature, serial, parse.
    The test chain is additional: every case in `test_cases` is evaluated against the
    candidate bundle, and the bundle is accepted only if all pass.

    An empty `test_cases` list passes vacuously — with zero tests, none fail. The
    returned summary's `total=0` is the warning; the operator should be alerted but
    not blocked, since the control plane might not have a corpus yet during initial
    setup.

    Returns:
        A `PolicyTestSummary` on success.

    Raises:
        BundleRejected: Signature, serial or parse failure (from `PolicyCache.load`).
        ActivationFailed: At least one test case produced the wrong verdict. The
            previously loaded bundle keeps serving. The exception carries the full
            summary for the failure report.
    """
    from agentiam_core.bundles import BundleSignatureError, verify_bundle

    # -- Gate 1: signature ---------------------------------------------------
    try:
        verify_bundle(bundle, signature, cache._public_key)
    except BundleSignatureError as exc:
        cache.rejected += 1
        raise BundleRejected(str(exc)) from exc

    # -- Gate 2: rollback ----------------------------------------------------
    if cache._serial is not None and bundle.serial <= cache._serial:
        cache.rejected += 1
        raise BundleRejected(
            f"bundle serial {bundle.serial} does not advance past the current "
            f"{cache._serial}; refusing a rollback"
        )

    # -- Gate 3: parse -------------------------------------------------------
    try:
        engine = CedarEngine(bundle, tools=cache._tools)
    except PolicyBundleError as exc:
        cache.rejected += 1
        raise BundleRejected(str(exc)) from exc


    now = cache._now()

    def evaluate_fn(case: PolicyTestCase) -> PolicyVerdict:
        return _evaluate_case(engine, case, now)

    results = run_policy_tests(test_cases, evaluate_fn)
    summary = summarize(results)

    if not summary.all_passed:
        raise ActivationFailed(summary, version=bundle.version)

    # -- All gates passed — make it current ----------------------------------
    cache._engine = engine
    cache._serial = bundle.serial
    cache._loaded_at = now
    cache.loaded += 1

    return summary
