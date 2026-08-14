"""`RESERVE`/`COMMIT` — spec 04 §4.2, §4.3, T-014.

PEP-side, pure, no I/O — same reason `agentiam-core` has none (rule 3): this is the hot
path, and every check here is against in-memory state (`LocalLease.remaining_local`), never
the database. `LEDGER_COMMIT`, the one operation that does touch Postgres, is
`agentiam_controlplane.db.ledger.ledger_commit` and is covered by
`tests/integration/test_ledger_commit.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentiam_core.models import LeaseState
from agentiam_pep.errors import ReservationInsufficientError
from agentiam_pep.lease import LocalLease, commit, reserve

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SKEW = timedelta(seconds=5)


def _lease(**overrides: object) -> LocalLease:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "granted": Decimal("100.0000"),
        "remaining_local": Decimal("100.0000"),
        "expires_at": _NOW + timedelta(seconds=60),
        "state": LeaseState.ACTIVE,
    }
    return LocalLease(**{**defaults, **overrides})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RESERVE
# ---------------------------------------------------------------------------


def test_reserve_decrements_remaining_local_and_returns_a_reservation() -> None:
    lease = _lease(remaining_local=Decimal("10.0000"))
    reservation = reserve(lease, Decimal("4.0000"), now=_NOW, skew=_SKEW)
    assert reservation.amount == Decimal("4.0000")
    assert reservation.lease_id == lease.id
    assert lease.remaining_local == Decimal("6.0000")


def test_reserve_generates_a_uuid_reservation_id_when_none_given() -> None:
    lease = _lease()
    reservation = reserve(lease, Decimal("1"), now=_NOW, skew=_SKEW)
    assert isinstance(reservation.id, uuid.UUID)


def test_reserve_uses_a_caller_supplied_reservation_id() -> None:
    lease = _lease()
    given_id = uuid.uuid4()
    reservation = reserve(lease, Decimal("1"), now=_NOW, skew=_SKEW, reservation_id=given_id)
    assert reservation.id == given_id


def test_reserve_fails_when_lease_is_not_active() -> None:
    lease = _lease(state=LeaseState.RELEASED, remaining_local=Decimal("10.0000"))
    with pytest.raises(ReservationInsufficientError):
        reserve(lease, Decimal("1.0000"), now=_NOW, skew=_SKEW)
    assert lease.remaining_local == Decimal("10.0000")  # untouched on failure


def test_reserve_fails_at_expires_at_minus_skew() -> None:
    """Spec 04 §9: a PEP MUST stop using a lease at `expires_at - S` — expire early."""
    lease = _lease(expires_at=_NOW + _SKEW, remaining_local=Decimal("10.0000"))
    with pytest.raises(ReservationInsufficientError):
        reserve(lease, Decimal("1.0000"), now=_NOW, skew=_SKEW)


def test_reserve_succeeds_just_before_the_skew_boundary() -> None:
    lease = _lease(expires_at=_NOW + _SKEW + timedelta(seconds=1), remaining_local=Decimal("10"))
    reserve(lease, Decimal("1"), now=_NOW, skew=_SKEW)  # must not raise


def test_reserve_fails_when_remaining_local_is_insufficient() -> None:
    lease = _lease(remaining_local=Decimal("2.0000"))
    with pytest.raises(ReservationInsufficientError):
        reserve(lease, Decimal("2.0001"), now=_NOW, skew=_SKEW)
    assert lease.remaining_local == Decimal("2.0000")  # untouched on failure


def test_reserve_of_exactly_remaining_local_succeeds() -> None:
    lease = _lease(remaining_local=Decimal("2.0000"))
    reserve(lease, Decimal("2.0000"), now=_NOW, skew=_SKEW)
    assert lease.remaining_local == Decimal("0.0000")


def test_reserve_rejects_a_negative_amount() -> None:
    lease = _lease()
    with pytest.raises(ValueError, match="non-negative"):
        reserve(lease, Decimal("-1"), now=_NOW, skew=_SKEW)


# ---------------------------------------------------------------------------
# COMMIT
# ---------------------------------------------------------------------------


def test_commit_with_exact_estimate_leaves_remaining_local_unchanged() -> None:
    lease = _lease(remaining_local=Decimal("6.0000"))
    reservation = reserve(lease, Decimal("4.0000"), now=_NOW, skew=_SKEW)
    outcome = commit(lease, reservation, Decimal("4.0000"), now=_NOW, skew=_SKEW)
    assert lease.remaining_local == Decimal("2.0000")  # unchanged since RESERVE, not since grant
    assert outcome.amount == Decimal("4.0000")
    assert outcome.escalated is False
    assert outcome.lease_id == lease.id
    assert outcome.reservation_id == reservation.id


def test_commit_over_estimate_refunds_precisely() -> None:
    """PLAN.md §9's T-014 acceptance bar, exact at 4dp with the values it names."""
    lease = _lease(granted=Decimal("999999.9999"), remaining_local=Decimal("999999.9999"))
    reservation = reserve(lease, Decimal("999999.9999"), now=_NOW, skew=_SKEW)
    outcome = commit(lease, reservation, Decimal("999999.9998"), now=_NOW, skew=_SKEW)
    assert lease.remaining_local == Decimal("0.0001")
    assert outcome.amount == Decimal("999999.9998")
    assert outcome.escalated is False


def test_commit_under_estimate_tops_up_from_remaining_local() -> None:
    lease = _lease(remaining_local=Decimal("10.0000"))
    reservation = reserve(lease, Decimal("2.0000"), now=_NOW, skew=_SKEW)
    outcome = commit(lease, reservation, Decimal("5.0000"), now=_NOW, skew=_SKEW)
    # 2 reserved up front, 3 more pulled from remaining_local (10 - 2 - 3 = 5)
    assert lease.remaining_local == Decimal("5.0000")
    assert outcome.amount == Decimal("5.0000")
    assert outcome.escalated is False


def test_commit_under_estimate_escalates_when_local_headroom_is_insufficient() -> None:
    lease = _lease(remaining_local=Decimal("3.0000"))
    reservation = reserve(lease, Decimal("2.0000"), now=_NOW, skew=_SKEW)  # remaining -> 1.0
    outcome = commit(lease, reservation, Decimal("10.0000"), now=_NOW, skew=_SKEW)  # needs 8 more
    assert lease.remaining_local == Decimal("1.0000")  # the failed top-up attempt is a no-op
    assert outcome.amount == Decimal("10.0000")  # the real spend is still reported, unclamped
    assert outcome.escalated is True


def test_commit_zero_delta_at_exactly_zero_remaining_local() -> None:
    lease = _lease(remaining_local=Decimal("0.0000"))
    reservation = reserve(lease, Decimal("0.0000"), now=_NOW, skew=_SKEW)
    outcome = commit(lease, reservation, Decimal("0.0000"), now=_NOW, skew=_SKEW)
    assert lease.remaining_local == Decimal("0.0000")
    assert outcome.escalated is False
