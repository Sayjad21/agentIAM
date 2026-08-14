"""Offline shape checks for `LeaseRow` — no database required (T-013, spec 04 §2.2).

Mirrors `tests/unit/test_budget_schema.py`. `tests/integration/test_lease_schema.py` and
`tests/integration/test_ledger.py` prove these shapes are actually enforced by Postgres
and that the operations in `db/ledger.py` behave correctly against a real lock manager.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from sqlalchemy import CheckConstraint, Numeric, Table

from agentiam_controlplane.db.models import LeaseRow

_TABLE = cast(Table, LeaseRow.__table__)


def test_money_columns_are_numeric_20_4() -> None:
    for name in ("granted", "settled"):
        column_type = _TABLE.columns[name].type
        assert isinstance(column_type, Numeric)
        assert (column_type.precision, column_type.scale) == (20, 4)
        assert column_type.asdecimal is True


def test_budget_id_has_a_real_foreign_key() -> None:
    """Unlike `budgets.mandate_id` (ADR-014), `leases.budget_id` references a real table."""
    foreign_keys = _TABLE.columns["budget_id"].foreign_keys
    assert len(foreign_keys) == 1
    fk = next(iter(foreign_keys))
    assert fk.target_fullname == "budgets.id"


def test_outstanding_invariant_check_present() -> None:
    check_constraints = [c for c in _TABLE.constraints if isinstance(c, CheckConstraint)]
    sqltexts = {str(c.sqltext) for c in check_constraints}
    assert any("settled <= granted" in text for text in sqltexts)
    assert any("granted >= 0" in text and "settled >= 0" in text for text in sqltexts)


def test_state_check_covers_all_four_states() -> None:
    check_constraints = [c for c in _TABLE.constraints if isinstance(c, CheckConstraint)]
    sqltexts = {str(c.sqltext) for c in check_constraints}
    state_check = next(text for text in sqltexts if "state IN" in text)
    for state in ("active", "released", "expired", "revoked"):
        assert f"'{state}'" in state_check


def test_outstanding_is_derived_not_stored() -> None:
    assert "outstanding" not in _TABLE.columns
    row = LeaseRow(
        budget_id=None,
        pep_id="pep-1",
        granted=Decimal("10.0000"),
        settled=Decimal("3.5000"),
        granted_at=None,
        expires_at=None,
        state="active",
    )
    assert row.outstanding == Decimal("6.5000")


def test_leases_index_on_budget_id_and_state_exists() -> None:
    index_columns = {tuple(col.name for col in ix.columns) for ix in _TABLE.indexes}
    assert ("budget_id", "state") in index_columns


def test_expires_at_partial_index_targets_active_state() -> None:
    matching = [ix for ix in _TABLE.indexes if ix.name == "ix_leases_expires_at_active"]
    assert len(matching) == 1
    where_clause = matching[0].dialect_options["postgresql"]["where"]
    assert "active" in str(where_clause)
