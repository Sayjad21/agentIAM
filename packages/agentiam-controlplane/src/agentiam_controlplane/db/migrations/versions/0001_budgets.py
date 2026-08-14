"""budgets — T-012, PLAN.md §7, spec 04 §2.1.

Only `budgets` lands here. `leases` and `reservations` are schema owned by T-013 and
T-014 (docs/DECISIONS.md ADR-014).

Revision ID: 0001
Revises:
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `budgets`."""
    op.create_table(
        "budgets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("mandate_id", sa.UUID(), nullable=False),
        sa.Column("dimension", sa.String(), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("committed", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("leased", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "committed >= 0 AND leased >= 0 AND committed + leased <= total",
            name="ck_budgets_invariant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mandate_id", "dimension", name="uq_budgets_mandate_dimension"),
    )


def downgrade() -> None:
    """Drop `budgets`."""
    op.drop_table("budgets")
