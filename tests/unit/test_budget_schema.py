"""Offline shape checks for `BudgetRow` — no database required.

These pin the column types and constraints that `tests/integration/test_budget_schema.py`
proves are actually enforced by Postgres. Kept separate so a mistake in shape (e.g. a
`float` sneaking into a money column) is caught by `make test` without Docker.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from sqlalchemy import CheckConstraint, Numeric, Table

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


def test_one_pool_row_per_mandate_and_dimension() -> None:
    """T-012's guarantee, still enforced — as a partial unique index since T-017.

    Allocation rows (`parent_budget_id` set) deliberately share their parent's
    `(mandate_id, dimension)`, so the constraint is scoped to pool rows rather than
    dropped. `tests/unit/test_split_schema.py` covers the allocation side.
    """
    pool_indexes = [i for i in _TABLE.indexes if i.name == "uq_budgets_pool"]
    assert len(pool_indexes) == 1
    assert pool_indexes[0].unique is True
    assert {col.name for col in pool_indexes[0].columns} == {"mandate_id", "dimension"}
    assert "parent_budget_id IS NULL" in str(pool_indexes[0].dialect_options["postgresql"]["where"])


def test_invariant_check_constraint_present() -> None:
    """The pool invariant, as the schema states it.

    `allocated` joined the sum in T-017: budget given to a child is spoken for, and
    leaving it out would let a parent lease out money it had already given away.
    """
    checks = {c.name: c for c in _TABLE.constraints if isinstance(c, CheckConstraint)}
    sqltext = str(checks["ck_budgets_invariant"].sqltext)
    assert "committed >= 0" in sqltext
    assert "leased >= 0" in sqltext
    assert "allocated >= 0" in sqltext
    assert "committed + leased + allocated <= total" in sqltext.replace("\n", " ")


def test_mandate_id_carries_no_foreign_key() -> None:
    """No `mandates` table exists yet (ADR-014) — this pins the gap, not just leaves it."""
    assert _TABLE.columns["mandate_id"].foreign_keys == set()
