"""Offline shape checks for the proportional-split columns — no database required.

T-017 gives `budgets` a second kind of row. Until now every row was a *pool*: one per
`(mandate_id, dimension)`, drawn from by every sibling under that mandate. A proportional
split adds *allocation* rows — one per child agent, carved out of a pool row, with their
own `total`.

The two kinds are distinguished by `parent_budget_id`, and the shape rules matter enough
to pin them here as well as in `tests/integration/test_sibling_budgets.py`: a row that is
half one kind and half the other would satisfy neither invariant.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from sqlalchemy import CheckConstraint, Numeric, Table, UniqueConstraint

from agentiam_controlplane.db.models import BudgetRow

_TABLE = cast(Table, BudgetRow.__table__)


class TestAllocatedColumn:
    def test_allocated_is_money(self) -> None:
        """It is carved out of `total`, so it obeys the same rule as the rest (rule 4)."""
        column_type = _TABLE.columns["allocated"].type
        assert isinstance(column_type, Numeric)
        assert (column_type.precision, column_type.scale) == (20, 4)
        assert column_type.asdecimal is True
        assert column_type.python_type is Decimal

    def test_allocated_is_not_nullable(self) -> None:
        """Absent means zero, never unknown — the same rule budgets follow everywhere."""
        assert _TABLE.columns["allocated"].nullable is False


class TestSplitColumns:
    def test_parent_budget_id_references_budgets(self) -> None:
        """Unlike `mandate_id` (ADR-014), this one points at a table that exists."""
        foreign_keys = list(_TABLE.columns["parent_budget_id"].foreign_keys)
        assert len(foreign_keys) == 1
        assert foreign_keys[0].column.table.name == "budgets"

    def test_a_pool_row_has_no_parent(self) -> None:
        assert _TABLE.columns["parent_budget_id"].nullable is True
        assert _TABLE.columns["agent_id"].nullable is True


class TestConstraints:
    @staticmethod
    def check(name: str) -> CheckConstraint:
        found = [c for c in _TABLE.constraints if isinstance(c, CheckConstraint) and c.name == name]
        assert found, f"no CHECK named {name!r}"
        return found[0]

    def test_the_pool_invariant_now_counts_allocated(self) -> None:
        """`committed + leased <= total` was true before splits existed and is not now.

        Budget handed to a child is spoken for. Leaving it out of the sum would let a
        parent allocate its whole pool and then lease it out a second time.
        """
        text = str(self.check("ck_budgets_invariant").sqltext)
        assert "allocated" in text
        assert "committed + leased + allocated <= total" in text.replace("\n", " ")

    def test_allocated_cannot_be_negative(self) -> None:
        assert "allocated >= 0" in str(self.check("ck_budgets_invariant").sqltext)

    def test_a_row_is_either_a_pool_or_an_allocation_never_half_of_each(self) -> None:
        """`parent_budget_id` and `agent_id` are set together or not at all.

        A row with a parent but no agent belongs to nobody; a row with an agent but no
        parent is a pool wearing a name tag. Both would be silently skipped by the
        invariant checker's per-kind queries.
        """
        text = str(self.check("ck_budgets_split_shape").sqltext).replace("\n", " ")
        assert "parent_budget_id" in text
        assert "agent_id" in text

    def test_pool_rows_stay_unique_per_mandate_and_dimension(self) -> None:
        """The original T-012 guarantee, now scoped to pool rows only.

        It has to become conditional rather than disappear: allocation rows share their
        parent's `(mandate_id, dimension)` by design, so an unconditional constraint
        refuses the entire feature — measured before this change, an ordinary
        `UniqueViolationError`.
        """
        indexes = {str(index.name): index for index in _TABLE.indexes}
        assert "uq_budgets_pool" in indexes, f"have: {sorted(indexes)}"
        pool_index = indexes["uq_budgets_pool"]
        assert pool_index.unique is True
        assert {c.name for c in pool_index.columns} == {"mandate_id", "dimension"}

    def test_one_allocation_per_agent_per_dimension(self) -> None:
        """Splitting twice for the same child is a bug, not a top-up."""
        uniques = [c for c in _TABLE.constraints if isinstance(c, UniqueConstraint)]
        names = {frozenset(col.name for col in c.columns) for c in uniques}
        assert frozenset({"parent_budget_id", "agent_id", "dimension"}) in names, names
