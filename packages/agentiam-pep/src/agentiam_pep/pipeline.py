"""The ten steps, wired — T-023, spec 09.

`decide()` is steps 3-7 and is pure. This module is everything around it: pulling a request
apart (T-020), verifying the token (T-007), reserving budget (T-021), recording the decision
(T-022), and turning the outcome into an HTTP response (spec 09 §11).

**The record is emitted before the request is forwarded, not after.** Spec 09 numbers emit as
step 10 and forward as step 8, but the ordering of those two is the PEP's to choose, and
ADR-026 forces this one: a full audit buffer must *deny*, and a policy that denies after the
upstream has already acted is not a policy. Everything the record contains — the verdict, the
failing cause, the reservation, the budget before and after — is known once step 7 finishes.
What is *not* in it is the upstream's answer, and spec 09 §6 says that was never a decision.

The cost is that a record exists for a call the upstream may then fail. That is the correct
direction: the record says *this was authorized*, which remains true.

Step 9 (settle) runs after the response, because only then is the real amount known.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from agentiam_core.decision import DriftOracle, decide
from agentiam_core.errors import ReasonCode, TokenError
from agentiam_core.models import BudgetDimension, Outcome, RequestContext
from agentiam_core.tokens import verify
from agentiam_pep.emitter import AuditBufferFullError, current_trace_id
from agentiam_pep.errors import ReservationInsufficientError
from agentiam_pep.extractor import ExtractionError, extract

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from contextlib import AbstractContextManager
    from datetime import datetime

    from opentelemetry.trace import Span

    from agentiam_core.models import Caveat, DecisionRecord
    from agentiam_core.tokens import RootKeySet, VerifiedToken
    from agentiam_pep.drift import FeatureExtractor
    from agentiam_pep.emitter import DecisionEmitter
    from agentiam_pep.escalation_sink import EscalationSink
    from agentiam_pep.extractor import Extraction, RouteTable
    from agentiam_pep.lease import Reservation
    from agentiam_pep.policy import AgentPrincipal, CedarEngine
    from agentiam_pep.pool import LeasePool
    from agentiam_pep.settlement import SettlementQueue

logger = logging.getLogger(__name__)

__all__ = ["Authorized", "Pipeline", "PipelineSettings", "Refused", "status_for"]

#: Spec 09 §11. Anything absent from this map is a bug rather than a default, so `status_for`
#: raises rather than guessing — a reason code with no defined status would otherwise ship as
#: a silent 500.
_STATUS: Final[dict[ReasonCode, int]] = {
    ReasonCode.MALFORMED_REQUEST: 401,
    ReasonCode.TOKEN_INVALID_SIGNATURE: 401,
    ReasonCode.TOKEN_EXPIRED: 401,
    ReasonCode.TOKEN_NOT_YET_VALID: 401,
    ReasonCode.TOKEN_TOO_LARGE: 401,
    ReasonCode.TOKEN_REVOKED: 401,
    ReasonCode.ANCESTOR_REVOKED: 401,
    ReasonCode.SCOPE_NOT_GRANTED: 403,
    ReasonCode.SCOPE_ATTENUATED_AWAY: 403,
    ReasonCode.TOOL_DENIED: 403,
    ReasonCode.ARG_PREDICATE_FAILED: 403,
    ReasonCode.INTENT_MISMATCH: 403,
    ReasonCode.DEPTH_EXCEEDED: 403,
    ReasonCode.POLICY_DENIED: 403,
    ReasonCode.APPROVAL_REQUIRED: 403,
    ReasonCode.DRIFT_ESCALATION: 403,
    ReasonCode.BUDGET_EXHAUSTED_MANDATE: 429,
    ReasonCode.BUDGET_EXHAUSTED_CAVEAT: 429,
    ReasonCode.LEASE_UNAVAILABLE: 429,
    ReasonCode.RATE_LIMITED: 429,
    ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED: 503,
    ReasonCode.POLICY_BUNDLE_STALE: 503,
    ReasonCode.VERIFICATION_LIMIT_EXCEEDED: 503,
    ReasonCode.LEASE_NOT_ACTIVE: 503,
    ReasonCode.UPSTREAM_ERROR: 502,
}

#: The header carrying the caller's asserted intent hash. Absent means the caller asserted
#: nothing, and the token's own intent is used — so `INTENT_MISMATCH` is reachable only when a
#: caller actually claims an intent. Binding it properly is T-032's job.
INTENT_HEADER: Final = "x-agentiam-intent"

#: Where the bearer token comes from.
AUTH_HEADER: Final = "authorization"


def status_for(reason: ReasonCode) -> int:
    """The HTTP status spec 09 §11 assigns to a reason code.

    Raises:
        KeyError: The code has no defined status. Deliberately not a default: a new code
            shipping as a silent 500 is exactly the kind of thing `PLAN.md` §6.9's closed
            enum exists to prevent.
    """
    return _STATUS[reason]


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """What the pipeline needs to know about itself."""

    pep_id: str
    policy_version_fallback: str = "none"
    #: How long a pending escalation stays actionable before `state_at()` reports it
    #: `EXPIRED` — spec: "expiry is a state, not a sweeper." 15 minutes by default: long
    #: enough for a human to notice a queue notification, short enough that a stale
    #: request does not sit there looking approvable.
    escalation_ttl_s: float = 900.0


@dataclass(frozen=True, slots=True)
class Refused:
    """A decision that stops the request here."""

    status: int
    reason_code: ReasonCode
    detail: str
    decision_id: uuid.UUID
    trace_id: str
    #: Set only for `APPROVAL_REQUIRED` / `DRIFT_ESCALATION` with a sink configured — spec
    #: 09 §11: "the escalation id travels in the body, because a client that cannot see it
    #: cannot follow up."
    escalation_id: uuid.UUID | None = None

    def body(self) -> dict[str, str]:
        """The response body spec 09 §11.3 fixes."""
        body = {
            "reason_code": self.reason_code.value,
            "detail": self.detail,
            "decision_id": str(self.decision_id),
            "trace_id": self.trace_id,
        }
        if self.escalation_id is not None:
            body["escalation_id"] = str(self.escalation_id)
        return body


@dataclass(frozen=True, slots=True)
class Authorized:
    """An allow, carrying what step 9 needs to settle it afterwards."""

    extraction: Extraction
    token: VerifiedToken
    reservation: Reservation | None
    decision_id: uuid.UUID
    trace_id: str


class Pipeline:
    """Steps 1 through 10, minus the proxy hop itself."""

    def __init__(
        self,
        *,
        routes: RouteTable,
        key_set: RootKeySet,
        policy: CedarEngine,
        principal_for: Callable[[VerifiedToken], AgentPrincipal],
        pool: LeasePool,
        emitter: DecisionEmitter,
        revocation: object,
        settings: PipelineSettings,
        now: Callable[[], datetime],
        caveats_for: Callable[[VerifiedToken], Sequence[Caveat]] | None = None,
        drift_oracle: DriftOracle | None = None,
        features: FeatureExtractor | None = None,
        escalation_sink: EscalationSink | None = None,
        settlement: SettlementQueue | None = None,
    ) -> None:
        """Assemble the five oracles and the two adapters the steps need."""
        self._routes = routes
        self._key_set = key_set
        self._policy = policy
        self._principal_for = principal_for
        self._pool = pool
        self._emitter = emitter
        self._revocation = revocation
        self._settings = settings
        self._now = now
        self._caveats_for = caveats_for or (lambda _token: ())
        self._drift_oracle = drift_oracle
        self._features = features
        self._escalation_sink = escalation_sink
        self._settlement = settlement

    def request_span(self) -> AbstractContextManager[Span | None]:
        """The whole-request span wrapping `authorize()`, `settle()`, and the upstream call.

        Must be opened by the caller *before* `authorize()` runs — T-049: `authorize()` calls
        `current_trace_id()` at its very first line, and that reads whatever span is
        already current. Opened here rather than inside `authorize()` itself so the
        upstream call the proxy makes afterward lands inside the same span — otherwise the
        decision and the call it authorized would show up as two unrelated traces in
        Tempo, which is what T-049 exists to not do. `agentiam.scope` is not known yet at
        this point (extraction hasn't run), so the caller sets it on the yielded span once
        `authorize()` returns.
        """
        return self._emitter.decision_span()

    def child_span(self, name: str) -> AbstractContextManager[Span | None]:
        """A nested span within whatever `request_span()` opened — the upstream call.

        Respects the same `tracing` on/off switch `request_span()` does, so T-053's load
        profile can measure with it fully disabled rather than half.
        """
        return self._emitter.span(name)

    async def authorize(
        self,
        *,
        method: str,
        path: str,
        query_string: str = "",
        headers: Iterable[tuple[str, str]] = (),
        body: bytes | None = None,
    ) -> Authorized | Refused:
        """Run steps 1-7, record the decision, and say whether to forward.

        Returns:
            `Authorized` when the request may proceed, `Refused` otherwise. Both carry a
            `decision_id` that ties the outcome to its audit record.
        """
        started = time.perf_counter()
        decision_id = uuid.uuid4()
        trace_id = current_trace_id() or str(decision_id)
        header_map = {name.lower(): value for name, value in headers}

        # --- step 1: extract -------------------------------------------------------
        try:
            extraction = extract(
                self._routes,
                method=method,
                path=path,
                query_string=query_string,
                headers=list(header_map.items()),
                body=body,
            )
        except ExtractionError as exc:
            return self._refuse(exc.reason, exc.detail, decision_id, trace_id)

        # --- step 2: verify --------------------------------------------------------
        raw = header_map.get(AUTH_HEADER, "")
        token_text = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
        try:
            token = verify(token_text, self._key_set, now=self._now())
        except TokenError as exc:
            reason = exc.reason_code or ReasonCode.MALFORMED_REQUEST
            return self._refuse(reason, str(exc), decision_id, trace_id)

        # --- steps 3-7: decide -----------------------------------------------------
        context = self._context_for(extraction, token, header_map)
        decision = decide(
            token,
            context,
            caveats=self._caveats_for(token),
            revocation=self._revocation,  # type: ignore[arg-type]
            policy=self._policy.bound(self._principal_for(token)),
            budget=self._pool,
            drift=self._drift_oracle,
        )

        # --- an escalating outcome opens a queue entry before it is recorded --------
        escalation_id: uuid.UUID | None = None
        if decision.outcome is Outcome.ESCALATE and self._escalation_sink is not None:
            try:
                escalation = await self._escalation_sink.create(
                    decision_id=decision_id,
                    task_id=token.task_id,
                    agent_id=self._principal_for(token).agent_id,
                    principal_id=token.principal_id,
                    intent_hash=context.request_intent,
                    requested_scopes=frozenset({context.operation}),
                    requested_amount=context.requested.get(BudgetDimension.SPEND_BDT, Decimal(0)),
                    reason=decision.reason_detail,
                    now=self._now(),
                    ttl=timedelta(seconds=self._settings.escalation_ttl_s),
                )
            except Exception as exc:
                # ADR-026's reasoning, applied here: a system that cannot record *that a
                # human was asked* must not tell the agent one was asked.
                return self._record_and_refuse(
                    ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED,
                    f"could not open an escalation: {exc}",
                    decision_id,
                    trace_id,
                    extraction=extraction,
                    token=token,
                    context=context,
                    started=started,
                )
            escalation_id = escalation.id

        reservation: Reservation | None = None
        if decision.outcome is Outcome.ALLOW:
            # Step 7 asked whether the lease covers this; now actually hold it. The gap
            # between the two is a within-process race that fails closed — a concurrent
            # request can empty the lease and this refuses (spec 04 §4.2).
            requested = context.requested[BudgetDimension.SPEND_BDT]
            if requested > 0:
                try:
                    reservation = self._pool.reserve(BudgetDimension.SPEND_BDT, requested)
                except ReservationInsufficientError as exc:
                    return self._record_and_refuse(
                        ReasonCode.LEASE_UNAVAILABLE,
                        str(exc),
                        decision_id,
                        trace_id,
                        extraction=extraction,
                        token=token,
                        context=context,
                        started=started,
                    )

        record = self._record(
            decision_id=decision_id,
            trace_id=trace_id,
            extraction=extraction,
            token=token,
            context=context,
            reason=decision.reason_code,
            outcome=decision.outcome,
            detail=decision.reason_detail,
            reservation=reservation,
            started=started,
            drift_score=decision.drift_score,
            context_for_features=context,
        )

        # --- step 10, deliberately before step 8 -----------------------------------
        try:
            self._emitter.emit(record)
        except AuditBufferFullError as exc:
            # ADR-026: a system that cannot record what it authorized must not authorize.
            # This is why emit precedes the forward — denying afterwards would be theatre.
            if reservation is not None:
                self._pool.commit(BudgetDimension.SPEND_BDT, reservation, Decimal(0))
            return self._refuse(
                ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED, str(exc), decision_id, trace_id
            )

        if decision.outcome is not Outcome.ALLOW:
            return Refused(
                status=status_for(decision.reason_code),
                reason_code=decision.reason_code,
                detail=decision.reason_detail,
                decision_id=decision_id,
                trace_id=trace_id,
                escalation_id=escalation_id,
            )

        return Authorized(
            extraction=extraction,
            token=token,
            reservation=reservation,
            decision_id=decision_id,
            trace_id=trace_id,
        )

    def settle(self, authorized: Authorized, *, actual: Decimal | None = None) -> None:
        """Step 9 — settle the reservation once the real amount is known.

        `actual` defaults to what was reserved. A tool that reports its own charge (the
        payment stub echoes one) passes it here, and spec 04 §4.3's refund or shortfall path
        does the rest.

        **Two settlements happen here, not one**, and until T-052 only the first did. The
        local one draws down `remaining_local` so this PEP stops spending budget it has
        already used. The second — enqueueing the resulting `CommitOutcome` for
        `LEDGER_COMMIT` — is what tells the *ledger*, and without it `budgets.committed`
        never moved and `RELEASE` handed spent budget back to the pool (spec 04 §4.4;
        `settlement.py` has the measurement).

        Still synchronous and still off the network: `enqueue()` appends to a deque and
        returns. A missing `settlement` queue leaves the old, local-only behaviour, which is
        what the pure-unit tests of `Pipeline` use.
        """
        if authorized.reservation is None:
            return
        outcome = self._pool.commit(
            BudgetDimension.SPEND_BDT,
            authorized.reservation,
            authorized.reservation.amount if actual is None else actual,
        )
        if self._settlement is not None:
            self._settlement.enqueue(outcome, now=self._now())

    # -- internals ------------------------------------------------------------------

    def _context_for(
        self,
        extraction: Extraction,
        token: VerifiedToken,
        headers: dict[str, str],
    ) -> RequestContext:
        requested = dict.fromkeys(BudgetDimension, Decimal(0))
        amount = extraction.args.get("payment.amount")
        if isinstance(amount, int):
            # The extractor scales declared-numeric args by 10^4 (spec 10 §4.3); the ledger
            # and the caveat language use the same scale, so this converts back once, here.
            requested[BudgetDimension.SPEND_BDT] = Decimal(amount) / 10**4

        task_intent_text = headers.get("agentiam-task-intent") or headers.get(
            "AgentIAM-Task-Intent"
        )
        action_intent_text = headers.get("agentiam-action-intent") or headers.get(
            "AgentIAM-Action-Intent"
        )

        if task_intent_text is not None:
            from agentiam_core.hashing import hash_object

            request_intent = hash_object(task_intent_text)
        else:
            request_intent = headers.get(INTENT_HEADER, token.intent_hash)

        return RequestContext(
            operation=extraction.scope,
            requested=requested,
            current_depth=token.depth,
            request_intent=request_intent,
            now=self._now(),
            tool=extraction.tool or None,
            args=dict(extraction.args),
            task_intent_text=task_intent_text,
            action_intent_text=action_intent_text,
            drift_mode=extraction.drift_mode,
        )

    def _refuse(
        self, reason: ReasonCode, detail: str, decision_id: uuid.UUID, trace_id: str
    ) -> Refused:
        return Refused(
            status=status_for(reason),
            reason_code=reason,
            detail=detail,
            decision_id=decision_id,
            trace_id=trace_id,
        )

    def _record_and_refuse(
        self,
        reason: ReasonCode,
        detail: str,
        decision_id: uuid.UUID,
        trace_id: str,
        *,
        extraction: Extraction,
        token: VerifiedToken,
        context: RequestContext,
        started: float,
    ) -> Refused:
        record = self._record(
            decision_id=decision_id,
            trace_id=trace_id,
            extraction=extraction,
            token=token,
            context=context,
            reason=reason,
            outcome=Outcome.DENY,
            detail=detail,
            reservation=None,
            started=started,
        )
        try:
            self._emitter.emit(record)
        except AuditBufferFullError:
            pass  # already refusing; the buffer's own denial cannot make this worse
        return self._refuse(reason, detail, decision_id, trace_id)

    def _record(
        self,
        *,
        decision_id: uuid.UUID,
        trace_id: str,
        extraction: Extraction,
        token: VerifiedToken,
        context: RequestContext,
        reason: ReasonCode,
        outcome: Outcome,
        detail: str,
        reservation: Reservation | None,
        started: float,
        drift_score: Decimal | None = None,
        context_for_features: RequestContext | None = None,
    ) -> DecisionRecord:
        from agentiam_core.models import Budget, DecisionRecord

        before = self._pool.remaining(BudgetDimension.SPEND_BDT)
        spent = reservation.amount if reservation is not None else Decimal(0)
        features = self._feature_vector(context_for_features)
        return DecisionRecord(
            decision_id=decision_id,
            trace_id=trace_id,
            timestamp=self._now(),
            pep_id=self._settings.pep_id,
            token_chain_ids=list(token.revocation_ids),
            principal_id=token.principal_id,
            task_id=token.task_id,
            agent_id=self._principal_for(token).agent_id,
            depth=token.depth,
            scope=extraction.scope,
            tool_id=extraction.tool,
            arg_digest=extraction.arg_digest,
            outcome=outcome,
            reason_code=reason,
            reason_detail=detail,
            policy_version=self._policy.bundle.version or self._settings.policy_version_fallback,
            budget_before=Budget(spend_bdt=before + spent),
            budget_after=Budget(spend_bdt=before),
            reservation_id=reservation.id if reservation is not None else None,
            drift_score=drift_score,
            drift_features=features,
            latency_us=int((time.perf_counter() - started) * 1e6),
        )

    def _feature_vector(self, context: RequestContext | None) -> dict[str, Decimal] | None:
        """The drift feature vector for this request, or `None` if none was computed.

        `None` rather than `{}`: an empty dict would read as *measured, and nothing was
        found*, which is a different claim from *not measured* (spec 06 §5.3).

        Extraction is observational and already swallows its own failures, but this is on
        the audit path, so it is belt-and-braces: no feature is worth failing a request
        that policy and budget both allowed.
        """
        if self._features is None or context is None:
            return None
        try:
            computed = self._features.extract(context).as_dict()
        except Exception:
            logger.warning("drift feature extraction failed", exc_info=True)
            return None
        return computed or None
