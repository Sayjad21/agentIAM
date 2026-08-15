"""Proportional split — T-017, spec 04 §13, INV-5.

Gives `budgets` a second kind of row. Until now every row was a *pool*: one per
`(mandate_id, dimension)`, drawn from by every sibling under that mandate. A proportional
split adds *allocation* rows — one per child agent, carved out of a pool, with their own
`total`.

Three consequences for the schema, and each is a change rather than an addition:

* `allocated` joins the pool invariant. Budget handed to a child is spoken for; leaving it
  out of the sum would let a parent allocate its whole pool and then lease the same money
  out again.
* `uq_budgets_mandate_dimension` becomes a **partial** unique index over pool rows.
  Allocation rows share their parent's `(mandate_id, dimension)` by design, so the
  unconditional constraint refuses the entire feature — measured before writing this, an
  ordinary `UniqueViolationError`.
* A new `CHECK` keeps the two kinds distinct: `parent_budget_id` and `agent_id` are set
  together or not at all.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVARIANT = (
    "committed >= 0 AND leased >= 0 AND allocated >= 0 AND committed + leased + allocated <= total"
)
_INVARIANT_BEFORE_SPLIT = "committed >= 0 AND leased >= 0 AND committed + leased <= total"


def upgrade() -> None:
    """Add the split columns and re-scope the constraints around them."""
    op.add_column(
        "budgets",
        sa.Column(
            "allocated",
            sa.Numeric(20, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("budgets", sa.Column("parent_budget_id", sa.UUID(), nullable=True))
    op.add_column("budgets", sa.Column("agent_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_budgets_parent_budget_id", "budgets", "budgets", ["parent_budget_id"], ["id"]
    )

    # The pool uniqueness survives, scoped to rows that are pools.
    op.drop_constraint("uq_budgets_mandate_dimension", "budgets", type_="unique")
    op.create_index(
        "uq_budgets_pool",
        "budgets",
        ["mandate_id", "dimension"],
        unique=True,
        postgresql_where=sa.text("parent_budget_id IS NULL"),
    )
    op.create_unique_constraint(
        "uq_budgets_allocation", "budgets", ["parent_budget_id", "agent_id", "dimension"]
    )

    op.drop_constraint("ck_budgets_invariant", "budgets", type_="check")
    op.create_check_constraint("ck_budgets_invariant", "budgets", _INVARIANT)
    op.create_check_constraint(
        "ck_budgets_split_shape",
        "budgets",
        "(parent_budget_id IS NULL) = (agent_id IS NULL)",
    )


def downgrade() -> None:
    """Drop the split columns, restoring the pre-T-017 shape.

    **This destroys data, and there is no version of it that does not.** Below this
    revision an allocation row cannot be represented at all — the columns that make it one
    are gone — so its leases, its settled reservations, and any reconciliation anomalies
    against them go with it. Merging a child back into its parent is not a safer
    alternative: it would have to invent an answer for a child's already-`committed`
    spend, and guessing in a downgrade is worse than deleting loudly.

    Take a dump first. The deletes run in foreign-key order — anomalies and reservations
    reference leases, leases reference budgets — because the obvious single `DELETE FROM
    budgets` fails on `leases_budget_id_fkey` the moment a split has been spent against.
    Measured: the integration fixture's teardown caught exactly that.
    """
    # Written out in full rather than composed: every fragment is a literal, and
    # composing them reads like dynamic SQL to a linter and to the next person.
    op.execute(
        sa.text(
            "DELETE FROM reconciliation_anomalies WHERE lease_id IN ("
            "SELECT id FROM leases WHERE budget_id IN ("
            "SELECT id FROM budgets WHERE parent_budget_id IS NOT NULL))"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM reservations WHERE lease_id IN ("
            "SELECT id FROM leases WHERE budget_id IN ("
            "SELECT id FROM budgets WHERE parent_budget_id IS NOT NULL))"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM leases WHERE budget_id IN ("
            "SELECT id FROM budgets WHERE parent_budget_id IS NOT NULL)"
        )
    )
    op.execute(sa.text("DELETE FROM budgets WHERE parent_budget_id IS NOT NULL"))

    # `IF EXISTS` on the CHECK drops. A downgrade must not care whether the constraint is
    # already gone — and it can legitimately be: `tests/integration/test_invariant_checker.py`
    # drops `ck_budgets_invariant` on purpose to inject a pool violation the schema would
    # otherwise refuse, and leaves the rebuild to the fixture. A strict drop turned that
    # into eight cascading teardown errors the first time these two tickets met.
    op.execute(sa.text("ALTER TABLE budgets DROP CONSTRAINT IF EXISTS ck_budgets_split_shape"))
    op.execute(sa.text("ALTER TABLE budgets DROP CONSTRAINT IF EXISTS ck_budgets_invariant"))
    op.create_check_constraint("ck_budgets_invariant", "budgets", _INVARIANT_BEFORE_SPLIT)

    op.drop_constraint("uq_budgets_allocation", "budgets", type_="unique")
    op.drop_index("uq_budgets_pool", table_name="budgets")
    op.create_unique_constraint(
        "uq_budgets_mandate_dimension", "budgets", ["mandate_id", "dimension"]
    )

    op.drop_constraint("fk_budgets_parent_budget_id", "budgets", type_="foreignkey")
    op.drop_column("budgets", "agent_id")
    op.drop_column("budgets", "parent_budget_id")
    op.drop_column("budgets", "allocated")
