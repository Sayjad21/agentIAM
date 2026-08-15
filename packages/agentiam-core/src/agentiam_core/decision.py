"""The decision pipeline — T-019, spec 09, `PLAN.md` §3.1 steps 3-7.

`PLAN.md` §3.2 principle 4: *every deny is explainable — a decision record names the exact
caveat, policy statement, or budget that caused it. "Denied" without a reason is a bug.*

Naming a cause is only useful if the pipeline agrees in advance **which** cause to name
when several are true at once. Spec 09 §3 settles that: **the first failing step wins, in
step order.** The alternative — evaluate everything, report the most severe — sounds more
informative and is not. It lets a revoked token be reported as `SCOPE_NOT_GRANTED`, and
the operator spends an afternoon adjusting scopes on a credential that was killed hours
ago.

**This function performs no I/O, and that is what makes NFR-1 reachable.** `PLAN.md` §3.1
annotates steps 2-7 as microseconds; §3.2 forbids blocking on the network to reach a
verdict. Revocation sets, policy bundles and lease pools are all held locally and refreshed
asynchronously, so the oracles below are synchronous by design — they read in-memory state,
not services. `tests/unit/test_core_purity.py` enforces the absence of I/O statically.

Steps 1-2 (extract, verify) happen before there is a `VerifiedToken` to reason about;
`decision_for_token_error` turns their failures into the same `Decision` shape. Steps 8-10
(forward, commit, emit) act on the decision and belong to the PEP.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol

from agentiam_core.caveats import evaluate
from agentiam_core.errors import (
    AgentIAMError,
    DepthExceededError,
    InvalidSignatureError,
    MalformedTokenError,
    ReasonCode,
    TokenError,
    TokenExpiredError,
    TokenNotYetValidError,
    TokenTooLargeError,
)
from agentiam_core.models import CaveatRef, Outcome

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentiam_core.models import BudgetDimension, Caveat, RequestContext
    from agentiam_core.tokens import VerifiedToken

#: Above this, step 6 escalates. `PLAN.md` §6.6 and T-036: drift escalates, never denies.
DRIFT_ESCALATION_THRESHOLD: Final = Decimal("0.7")


class OracleUnavailable(AgentIAMError):
    """A locally-held fact could not be consulted at all.

    Distinct from an oracle that *says no*: this is "the answer is unknown", and spec 09 §5
    requires it to deny with `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`. Drift is the one
    exception — see `decide`.
    """


# ---------------------------------------------------------------------------
# Injected dependencies. All synchronous: they read local caches, not services.
# ---------------------------------------------------------------------------


class RevocationOracle(Protocol):
    """The PEP's in-memory counting Bloom filter plus exact set (T-039)."""

    def is_revoked(self, revocation_id: str) -> bool:
        """Whether this block id has been revoked. Raises `OracleUnavailable` if unknown."""
        ...


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """What the Cedar bundle said, plus whether the bundle can still be trusted."""

    allowed: bool
    statement: str | None = None
    version: str = ""
    stale: bool = False


class PolicyEngine(Protocol):
    """In-process Cedar over a cached, signed bundle (T-024, T-025)."""

    def evaluate(self, context: RequestContext) -> PolicyVerdict:
        """Evaluate the bundle. Raises `OracleUnavailable` if it cannot be consulted."""
        ...


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether the local lease can cover this request.

    `mandate_exhausted` distinguishes *the mandate has no budget left* from *this PEP's
    lease is empty and a top-up has not arrived* — the same refusal to the caller, two
    different fixes for whoever is paged.
    """

    ok: bool
    exhausted_dimension: BudgetDimension | None = None
    mandate_exhausted: bool = False


class BudgetOracle(Protocol):
    """The PEP-local lease pool (T-021). No network: spec 04 §4.2."""

    def check(self, requested: Mapping[BudgetDimension, Decimal]) -> BudgetVerdict:
        """Whether the lease covers `requested`. Raises `OracleUnavailable` if unknown."""
        ...


class DriftOracle(Protocol):
    """Intent drift, cached or scored inline (T-036)."""

    def score_for(self, context: RequestContext) -> Decimal | None:
        """A score in [0, 1], or None if not assessed."""
        ...


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """One verdict. Frozen: it becomes an audit record, and those do not change.

    Feeds `DecisionRecord` (`PLAN.md` §6.9) directly — the model cross-validates outcome
    against reason code, so an inconsistent pair fails at construction rather than in the
    ledger.
    """

    outcome: Outcome
    reason_code: ReasonCode
    reason_detail: str
    failing_caveat: CaveatRef | None = None
    drift_score: Decimal | None = None

    @property
    def allowed(self) -> bool:
        """True only for a plain allow. Escalation is not permission."""
        return self.outcome is Outcome.ALLOW


_ALLOW: Final = Decision(Outcome.ALLOW, ReasonCode.OK, "")


def _deny(code: ReasonCode, detail: str, caveat: CaveatRef | None = None) -> Decision:
    return Decision(Outcome.DENY, code, detail, caveat)


#: Steps 1-2. Each token error already fixes its own reason code (T-005), so this is a
#: transcription rather than a decision — but it is written explicitly so a new
#: `TokenError` subclass without a code fails here rather than defaulting to something
#: plausible.
_TOKEN_ERROR_CODES: Final[dict[type[TokenError], ReasonCode]] = {
    MalformedTokenError: ReasonCode.MALFORMED_REQUEST,
    InvalidSignatureError: ReasonCode.TOKEN_INVALID_SIGNATURE,
    TokenExpiredError: ReasonCode.TOKEN_EXPIRED,
    TokenNotYetValidError: ReasonCode.TOKEN_NOT_YET_VALID,
    TokenTooLargeError: ReasonCode.TOKEN_TOO_LARGE,
    DepthExceededError: ReasonCode.DEPTH_EXCEEDED,
}


def decision_for_token_error(error: TokenError) -> Decision:
    """Turn a step 1-2 failure into a `Decision`.

    Verification happens before there is a token to reason about, so its failures cannot
    go through `decide`. They still have to reach the audit ledger in the same shape.

    Args:
        error: What `tokens.verify` raised.

    Returns:
        A deny carrying the error's own reason code.
    """
    code = _TOKEN_ERROR_CODES.get(type(error), error.reason_code or ReasonCode.MALFORMED_REQUEST)
    return _deny(code, str(error))


def _caveat_ref(index: int, caveat: Caveat, detail: str) -> CaveatRef:
    """Point at the caveat that caused this, for the console and the audit trail.

    `block_index` is the caveat's position in the chain as supplied. Where the caller could
    only recover part of the chain it is still the best pointer available — see spec 09 §4
    on why the caveat list is an input rather than something read back from the token.
    """
    return CaveatRef(block_index=index, kind=caveat.kind, detail=detail)


def _evaluate_caveats(caveats: Sequence[Caveat], context: RequestContext) -> Decision | None:
    """Step 4. The first denying caveat wins; a deny anywhere outranks any escalation.

    Two rules from spec 09, and both are visible here rather than emergent:

    * **§3.1, INV-8 — deny beats escalate.** The whole list is scanned even after an
      escalation is found, because approval cannot grant authority the token does not
      have. Escalating a request that a later caveat forbids asks a human a question with
      only one correct answer.
    * **§3.2 — chain order decides.** Caveats are evaluated root-first, so the reported
      cause is the one nearer the root: the broader restriction, and the larger decision
      to reverse.
    """
    escalation: Decision | None = None

    for index, caveat in enumerate(caveats):
        result = evaluate(caveat, context)
        if result.outcome is Outcome.ALLOW:
            continue

        ref = _caveat_ref(index, caveat, result.detail)
        if result.outcome is Outcome.DENY:
            return _deny(result.reason_code, result.detail, ref)
        if escalation is None:
            escalation = Decision(result.outcome, result.reason_code, result.detail, ref)

    return escalation


def decide(
    token: VerifiedToken,
    context: RequestContext,
    *,
    caveats: Sequence[Caveat] = (),
    revocation: RevocationOracle,
    policy: PolicyEngine,
    budget: BudgetOracle,
    drift: DriftOracle | None = None,
) -> Decision:
    """Run steps 3-7 and return the verdict.

    Args:
        token: The already-verified token (step 2 succeeded).
        context: What the verifier says about this call — every budget dimension present,
            defaulting to zero, because a Datalog check whose fact is absent *fails*
            (ADR-005). `RequestContext` enforces that at construction.
        caveats: The caveats binding on this chain, root-first. An **input** rather than
            something read back from the token: a `VerifiedToken` exposes the authority
            block's grant, not what later blocks added (ADR-005, `STATUS.md` gap 2). An
            incomplete list still denies correctly — biscuit's own authorizer enforces the
            chain regardless — but `failing_caveat` may be `None` where a complete list
            would have named one. Spec 09 §4.
        revocation: In-memory revoked-id lookup.
        policy: In-process Cedar over a cached bundle.
        budget: The PEP-local lease pool.
        drift: Optional. Absent means no assessment, which is not a failure.

    Returns:
        The decision. `reason_code` is `OK` only for an allow.
    """
    # --- step 3: revocation ------------------------------------------------
    try:
        for position, revocation_id in enumerate(token.revocation_ids):
            if revocation.is_revoked(revocation_id):
                # The last id is this token's own; anything earlier is an ancestor, and
                # INV-10 says a revoked ancestor kills the chain without the children ever
                # being enumerated.
                is_self = position == len(token.revocation_ids) - 1
                return _deny(
                    ReasonCode.TOKEN_REVOKED if is_self else ReasonCode.ANCESTOR_REVOKED,
                    f"block {revocation_id} is revoked",
                )
    except OracleUnavailable as exc:
        return _deny(ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED, f"revocation: {exc}")

    # --- step 4: the token's own authority ---------------------------------
    #
    # The authority block carries six checks (spec 01 §5), and biscuit's own authorizer would
    # enforce every one of them — but `verify()` deliberately never calls `authorize()`, it
    # extracts facts (see its module docstring). So whatever this function does not
    # re-implement is enforced *nowhere on the path the PEP actually runs*.
    #
    # Found by wiring T-023: a request whose `request_intent` did not match the token's was
    # allowed here while biscuit denied it. The other five are covered — scope below, the
    # validity window and the chain's depth in `verify()`, the mandate's budget ceiling by the
    # ledger that issues the lease — and intent was covered by nothing (TM-27).
    if context.operation not in token.scopes:
        # Never in the mandate at all — distinct from granted and then attenuated away,
        # which the caveat loop below reports. The operator's fix differs: widen the
        # mandate, versus re-mint without the narrowing.
        return _deny(
            ReasonCode.SCOPE_NOT_GRANTED,
            f"scope {context.operation!r} is not in the mandate's grant",
        )

    if context.request_intent != token.intent_hash:
        # INV-7 and TM-10: the token is bound to one approved task, and this is what makes
        # "which task authorized this?" answerable. An injected instruction that redirects an
        # agent to a different task cannot carry the matching hash.
        return _deny(
            ReasonCode.INTENT_MISMATCH,
            "the request's intent does not match the intent this token is bound to",
        )

    if context.current_depth > token.max_depth:
        # `verify()` checks the *chain's* depth from its block count. This checks the depth
        # the verifier reports for this call, which is a separate fact and could disagree.
        return _deny(
            ReasonCode.DEPTH_EXCEEDED,
            f"depth {context.current_depth} exceeds the mandate's limit of {token.max_depth}",
        )

    caveat_decision = _evaluate_caveats(caveats, context)
    if caveat_decision is not None and caveat_decision.outcome is Outcome.DENY:
        return caveat_decision

    # --- step 5: policy ----------------------------------------------------
    try:
        verdict = policy.evaluate(context)
    except OracleUnavailable as exc:
        return _deny(ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED, f"policy: {exc}")

    if verdict.stale:
        # Its own code because the fix is different: refresh the bundle, do not edit it.
        # Checked before `allowed`, since a stale bundle's answer is not evidence either way.
        return _deny(
            ReasonCode.POLICY_BUNDLE_STALE,
            f"policy bundle {verdict.version or 'unknown'} is past its staleness limit",
        )
    if not verdict.allowed:
        return _deny(
            ReasonCode.POLICY_DENIED,
            f"denied by policy statement {verdict.statement or 'unnamed'}",
        )

    # --- step 6: drift -----------------------------------------------------
    # Deliberately *not* fail-closed (spec 09 §5). Drift is advisory, and an outage of an
    # advisory heuristic must not stop every payment in the system.
    from agentiam_core.models import DriftMode
    
    score: Decimal | None = None
    if drift is not None and context.drift_mode != DriftMode.OFF:
        try:
            score = drift.score_for(context)
        except OracleUnavailable:
            score = None

    if score is not None and score > DRIFT_ESCALATION_THRESHOLD and context.drift_mode == DriftMode.STRICT:
        return Decision(
            Outcome.ESCALATE,
            ReasonCode.DRIFT_ESCALATION,
            f"drift score {score} is above the escalation threshold {DRIFT_ESCALATION_THRESHOLD}",
            drift_score=score,
        )

    # An escalation from step 4 has survived every later deny check, so it stands.
    if caveat_decision is not None:
        return Decision(
            caveat_decision.outcome,
            caveat_decision.reason_code,
            caveat_decision.reason_detail,
            caveat_decision.failing_caveat,
            drift_score=score,
        )

    # --- step 7: budget ----------------------------------------------------
    try:
        budget_verdict = budget.check(context.requested)
    except OracleUnavailable as exc:
        return _deny(ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED, f"lease pool: {exc}")

    if not budget_verdict.ok:
        dimension = budget_verdict.exhausted_dimension
        named = dimension.value if dimension is not None else "the request"
        if budget_verdict.mandate_exhausted:
            return _deny(
                ReasonCode.BUDGET_EXHAUSTED_MANDATE,
                f"the mandate's budget for {named} is exhausted",
            )
        return _deny(
            ReasonCode.LEASE_UNAVAILABLE,
            f"this PEP's lease for {named} cannot cover the request",
        )

    return (
        _ALLOW if score is None else Decision(Outcome.ALLOW, ReasonCode.OK, "", drift_score=score)
    )


__all__ = [
    "DRIFT_ESCALATION_THRESHOLD",
    "BudgetOracle",
    "BudgetVerdict",
    "Decision",
    "DriftOracle",
    "OracleUnavailable",
    "PolicyEngine",
    "PolicyVerdict",
    "RevocationOracle",
    "decide",
    "decision_for_token_error",
]
