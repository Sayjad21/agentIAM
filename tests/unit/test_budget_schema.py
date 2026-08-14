"""Offline shape checks for `BudgetRow` — no database required.

These pin the column types and constraints that `tests/integration/test_budget_schema.py`
proves are actually enforced by Postgres. Kept separate so a mistake in shape (e.g. a
`float` sneaking into a money column) is caught by `make test` without Docker.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from sqlalchemy import CheckConstraint, Numeric, Table, UniqueConstraint

from agentiam_controlplane.db.models import BudgetRow

_TABLE = cast(Table, BudgetRow.__table__)


def test_money_columns_are_numeric_20_4() -> None:
    for name in ("total", "committed", "leased"):
        column_type = _TABLE.columns[name].type
        assert isinstance(column_type, Numeric)
        assert (column_type.precision, column_type.scale) == (20, 4)
        assert column_type.asdecimal is True


def test_money_columns_reject_float_at_the_python_layer() -> None:
    for name in ("total", "committed", "leased"):
        column = _TABLE.columns[name]
        assert column.type.python_type is Decimal


def test_unique_constraint_is_mandate_id_and_dimension() -> None:
    unique_constraints = [c for c in _TABLE.constraints if isinstance(c, UniqueConstraint)]
    assert len(unique_constraints) == 1
    assert {col.name for col in unique_constraints[0].columns} == {"mandate_id", "dimension"}


def test_invariant_check_constraint_present() -> None:
    check_constraints = [c for c in _TABLE.constraints if isinstance(c, CheckConstraint)]
    assert len(check_constraints) == 1
    sqltext = str(check_constraints[0].sqltext)
    assert "committed >= 0" in sqltext
    assert "leased >= 0" in sqltext
    assert "committed + leased <= total" in sqltext


def test_mandate_id_carries_no_foreign_key() -> None:
    """No `mandates` table exists yet (ADR-014) — this pins the gap, not just leaves it."""
    assert _TABLE.columns["mandate_id"].foreign_keys == set()
