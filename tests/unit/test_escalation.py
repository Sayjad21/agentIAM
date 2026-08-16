"""The escalation workflow — T-037, PLAN.md §11.7.

Six acceptance criteria (PLAN.md:1151) and five edge cases (EC-A07…EC-A11). The ones that
carry security weight, and why:

* **The elevated grant is ⊆ what was requested** (EC-A09). An approver may narrow; nothing
  may widen. If approval could grant more than was asked for, the request text an operator
  reads on screen would stop being what they are authorising.
* **Approval mints a fresh mandate rather than touching the token** (PLAN §207). Elevation
  *widens*, and `attenuate()` cannot widen by construction — so the only honest mechanism
  is a new short-lived grant. That makes "the original token is unchanged" structural
  rather than a promise, and the test asserts it anyway.
* **One escalation resolves once** (EC-A10). Two approvers racing must not produce two
  elevated tokens.

Rule 4: money is `Decimal`, never `float`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentiam_core.escalation import (
    ApproverNotAuthorized,
    Escalation,
    EscalationExpired,
    EscalationNotPending,
    EscalationState,
    NarrowingWidensRequest,
    approve,
    deny,
    request_escalation,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
APPROVERS = frozenset({"kc:manager", "kc:cfo"})


def an_escalation(**over: object) -> Escalation:
    base: dict[str, object] = {
        "decision_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "agent_id": "agt-1",
        "principal_id": "kc:alice",
        "requested_scopes": frozenset({"payment:initiate"}),
        "requested_amount": Decimal(50000),
        "reason": "invoice INV-2291 exceeds the standing ceiling",
        "now": NOW,
        "ttl": timedelta(minutes=15),
    }
    return request_escalation(**(base | over))  # type: ignore[arg-type]


class TestRequesting:
    def test_a_request_starts_pending_with_an_id(self) -> None:
        escalation = an_escalation()
        assert escalation.state is EscalationState.PENDING
        assert escalation.id is not None

    def test_the_request_carries_the_decision_it_came_from(self) -> None:
        # Without this the audit chain cannot answer "who authorised this payment?" —
        # the escalation and the decision that provoked it would be unlinked.
        decision_id = uuid.uuid4()
        assert an_escalation(decision_id=decision_id).decision_id == decision_id

    def test_the_request_expires_after_its_ttl(self) -> None:
        escalation = an_escalation(ttl=timedelta(minutes=15))
        assert escalation.expires_at == NOW + timedelta(minutes=15)

    def test_a_request_with_no_scopes_is_refused(self) -> None:
        # An escalation that asks for nothing cannot be approved into anything, and would
        # sit in the queue forever looking like work.
        with pytest.raises(ValueError, match="at least one scope"):
            an_escalation(requested_scopes=frozenset())

    def test_a_negative_amount_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            an_escalation(requested_amount=Decimal(-1))


class TestExpiry:
    """Criterion: TTL expiry auto-denies. EC-A07: approval after TTL is rejected."""

    def test_it_is_not_expired_before_the_ttl(self) -> None:
        escalation = an_escalation(ttl=timedelta(minutes=15))
        assert not escalation.is_expired(NOW + timedelta(minutes=14))

    def test_it_is_expired_after_the_ttl(self) -> None:
        escalation = an_escalation(ttl=timedelta(minutes=15))
        assert escalation.is_expired(NOW + timedelta(minutes=16))

    def test_expiry_is_inclusive_at_the_boundary(self) -> None:
        # Stated because "expires at 12:15" is ambiguous in English and must not be in
        # code: at exactly the expiry instant the window is over.
        escalation = an_escalation(ttl=timedelta(minutes=15))
        assert escalation.is_expired(NOW + timedelta(minutes=15))

    def test_approving_an_expired_escalation_is_refused(self) -> None:
        escalation = an_escalation(ttl=timedelta(minutes=15))
        with pytest.raises(EscalationExpired):
            approve(
                escalation,
                approver="kc:manager",
                authorized=APPROVERS,
                now=NOW + timedelta(minutes=16),
                elevation_ttl=timedelta(minutes=5),
            )

    def test_an_expired_escalation_reports_itself_denied(self) -> None:
        # "Auto-denies" is a *state*, not a background job: anything reading the queue
        # after the TTL sees a denial without a sweeper having run.
        escalation = an_escalation(ttl=timedelta(minutes=15))
        assert escalation.state_at(NOW + timedelta(minutes=16)) is EscalationState.EXPIRED
        assert escalation.state_at(NOW + timedelta(minutes=1)) is EscalationState.PENDING


class TestApproval:
    def test_approval_resolves_the_escalation(self) -> None:
        resolved, _ = approve(
            an_escalation(),
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        assert resolved.state is EscalationState.APPROVED
        assert resolved.resolved_by == "kc:manager"

    def test_approval_yields_a_grant_matching_the_request(self) -> None:
        escalation = an_escalation()
        _, grant = approve(
            escalation,
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        assert grant.scopes == escalation.requested_scopes
        assert grant.amount == escalation.requested_amount

    def test_the_grant_is_short_lived(self) -> None:
        _, grant = approve(
            an_escalation(),
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        assert grant.expires_at == NOW + timedelta(minutes=5)
        assert grant.not_before == NOW

    def test_the_grant_names_its_approver(self) -> None:
        # `DecisionRecord.elevated_by` exists so a later audit can say who widened this.
        _, grant = approve(
            an_escalation(),
            approver="kc:cfo",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        assert grant.approved_by == "kc:cfo"

    def test_the_grant_keeps_the_task_it_belongs_to(self) -> None:
        escalation = an_escalation()
        _, grant = approve(
            escalation,
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        assert grant.task_id == escalation.task_id
        assert grant.principal_id == escalation.principal_id


class TestNarrowing:
    """EC-A09, and the property that makes an approval screen meaningful."""

    def test_an_approver_may_narrow_the_scopes(self) -> None:
        escalation = an_escalation(
            requested_scopes=frozenset({"payment:initiate", "invoice:write"})
        )
        _, grant = approve(
            escalation,
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
            narrowed_scopes=frozenset({"payment:initiate"}),
        )
        assert grant.scopes == frozenset({"payment:initiate"})

    def test_an_approver_may_reduce_the_amount(self) -> None:
        _, grant = approve(
            an_escalation(requested_amount=Decimal(50000)),
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
            max_amount=Decimal(10000),
        )
        assert grant.amount == Decimal(10000)

    def test_the_grant_is_always_a_subset_of_the_request(self) -> None:
        escalation = an_escalation(requested_scopes=frozenset({"payment:initiate"}))
        _, grant = approve(
            escalation,
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        assert grant.scopes <= escalation.requested_scopes
        assert grant.amount <= escalation.requested_amount

    def test_widening_the_scopes_is_refused(self) -> None:
        # The whole point: an operator approves the request they were shown. Granting a
        # scope nobody asked for would make the approval screen a lie.
        escalation = an_escalation(requested_scopes=frozenset({"invoice:read"}))
        with pytest.raises(NarrowingWidensRequest, match="payment:initiate"):
            approve(
                escalation,
                approver="kc:manager",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
                narrowed_scopes=frozenset({"invoice:read", "payment:initiate"}),
            )

    def test_raising_the_amount_is_refused(self) -> None:
        with pytest.raises(NarrowingWidensRequest, match="amount"):
            approve(
                an_escalation(requested_amount=Decimal(1000)),
                approver="kc:manager",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
                max_amount=Decimal(5000),
            )

    def test_narrowing_to_nothing_is_refused(self) -> None:
        # An empty grant is a denial wearing an approval's clothes. Deny it explicitly,
        # so the agent gets a reason rather than a token that permits nothing.
        with pytest.raises(ValueError, match="at least one scope"):
            approve(
                an_escalation(),
                approver="kc:manager",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
                narrowed_scopes=frozenset(),
            )


class TestAuthorization:
    """EC-A08: approval by a non-authorized principal is rejected and audited."""

    def test_an_unauthorized_approver_is_refused(self) -> None:
        with pytest.raises(ApproverNotAuthorized):
            approve(
                an_escalation(),
                approver="kc:intruder",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
            )

    def test_the_refusal_names_the_attempted_approver_for_the_audit(self) -> None:
        # EC-A08 says the attempt is *audited*, so the identity has to survive into the
        # error rather than being swallowed as a generic 403.
        with pytest.raises(ApproverNotAuthorized) as exc:
            approve(
                an_escalation(),
                approver="kc:intruder",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
            )
        assert exc.value.approver == "kc:intruder"

    def test_the_agent_may_not_approve_its_own_escalation(self) -> None:
        # Self-approval would make the human-in-the-loop decorative. Checked even when
        # the agent's principal is otherwise an authorized approver.
        escalation = an_escalation(principal_id="kc:manager")
        with pytest.raises(ApproverNotAuthorized, match="own"):
            approve(
                escalation,
                approver="kc:manager",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
            )


class TestResolutionIsOnce:
    """EC-A10: two approvers race the same escalation; the first wins."""

    def test_approving_twice_is_refused(self) -> None:
        resolved, _ = approve(
            an_escalation(),
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        with pytest.raises(EscalationNotPending):
            approve(
                resolved,
                approver="kc:cfo",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
            )

    def test_denying_an_approved_escalation_is_refused(self) -> None:
        resolved, _ = approve(
            an_escalation(),
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        with pytest.raises(EscalationNotPending):
            deny(resolved, approver="kc:cfo", authorized=APPROVERS, now=NOW, reason="no")

    def test_approving_a_denied_escalation_is_refused(self) -> None:
        denied = deny(
            an_escalation(),
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            reason="not this vendor",
        )
        with pytest.raises(EscalationNotPending):
            approve(
                denied,
                approver="kc:cfo",
                authorized=APPROVERS,
                now=NOW,
                elevation_ttl=timedelta(minutes=5),
            )


class TestDenial:
    """Criterion: the denial reason propagates to the agent."""

    def test_denial_records_the_reason(self) -> None:
        denied = deny(
            an_escalation(),
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            reason="vendor is not on the approved list",
        )
        assert denied.state is EscalationState.DENIED
        assert denied.resolution_reason == "vendor is not on the approved list"
        assert denied.resolved_by == "kc:manager"

    def test_a_denial_needs_a_reason(self) -> None:
        # "Denied" with no reason gives the agent nothing to act on and the operator
        # nothing to defend later.
        with pytest.raises(ValueError, match="reason"):
            deny(an_escalation(), approver="kc:manager", authorized=APPROVERS, now=NOW, reason="")

    def test_an_unauthorized_principal_may_not_deny_either(self) -> None:
        with pytest.raises(ApproverNotAuthorized):
            deny(
                an_escalation(),
                approver="kc:intruder",
                authorized=APPROVERS,
                now=NOW,
                reason="no",
            )

    def test_an_expired_escalation_cannot_be_denied_again(self) -> None:
        with pytest.raises(EscalationExpired):
            deny(
                an_escalation(ttl=timedelta(minutes=15)),
                approver="kc:manager",
                authorized=APPROVERS,
                now=NOW + timedelta(minutes=16),
                reason="too late",
            )


class TestImmutability:
    """Criterion: approval never mutates what already exists."""

    def test_resolving_returns_a_new_escalation_and_leaves_the_original_pending(self) -> None:
        original = an_escalation()
        resolved, _ = approve(
            original,
            approver="kc:manager",
            authorized=APPROVERS,
            now=NOW,
            elevation_ttl=timedelta(minutes=5),
        )
        assert original.state is EscalationState.PENDING
        assert resolved is not original
        assert resolved.id == original.id

    def test_an_escalation_is_frozen(self) -> None:
        escalation = an_escalation()
        with pytest.raises((AttributeError, TypeError)):
            escalation.state = EscalationState.APPROVED  # type: ignore[misc]
