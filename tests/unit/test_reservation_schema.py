"""Offline shape checks for `ReservationRow`/`ReconciliationAnomalyRow` (T-014, spec 04 §2.2/§11).

Mirrors `tests/unit/test_lease_schema.py`. `tests/integration/test_reservation_schema.py` and
`tests/integration/test_ledger.py` prove these shapes are enforced by Postgres and that
`ledger_commit()` behaves correctly against a real lock manager.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import Numeric, Table

from agentiam_controlplane.db.models import ReconciliationAnomalyRow, ReservationRow

_RESERVATIONS = cast(Table, ReservationRow.__table__)
_ANOMALIES = cast(Table, ReconciliationAnomalyRow.__table__)


def test_reservation_id_is_the_primary_key_with_no_server_default() -> None:
    """Spec 04 §10: the id is client-generated, not `default=uuid.uuid4` like other tables."""
    id_column = _RESERVATIONS.columns["id"]
    assert id_column.primary_key is True
    assert id_column.default is None


def test_reservation_amount_is_numeric_20_4() -> None:
    column_type = _RESERVATIONS.columns["amount"].type
    assert isinstance(column_type, Numeric)
    assert (column_type.precision, column_type.scale) == (20, 4)
    assert column_type.asdecimal is True


def test_reservation_lease_id_has_a_real_foreign_key() -> None:
    foreign_keys = _RESERVATIONS.columns["lease_id"].foreign_keys
    assert len(foreign_keys) == 1
    fk = next(iter(foreign_keys))
    assert fk.target_fullname == "leases.id"


def test_anomaly_id_has_a_server_default() -> None:
    """Unlike `reservations.id`, an anomaly's own id is never PEP-supplied — the ledger mints it."""
    id_column = _ANOMALIES.columns["id"]
    assert id_column.primary_key is True
    assert id_column.default is not None


def test_anomaly_lease_id_has_a_real_foreign_key() -> None:
    foreign_keys = _ANOMALIES.columns["lease_id"].foreign_keys
    assert len(foreign_keys) == 1
    fk = next(iter(foreign_keys))
    assert fk.target_fullname == "leases.id"


def test_anomaly_reservation_id_carries_no_foreign_key() -> None:
    """A rejected commit's `reservation_id` never gets a `reservations` row (§11).

    There's nothing for it to reference.
    """
    assert len(_ANOMALIES.columns["reservation_id"].foreign_keys) == 0


def test_anomaly_reported_amount_is_numeric_20_4() -> None:
    column_type = _ANOMALIES.columns["reported_amount"].type
    assert isinstance(column_type, Numeric)
    assert (column_type.precision, column_type.scale) == (20, 4)
    assert column_type.asdecimal is True
