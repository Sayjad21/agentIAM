"""leases — T-013, PLAN.md §7, spec 04 §2.2, §3.

Unlike `budgets.mandate_id` (ADR-014), `budget_id` gets a real foreign key: `budgets` is a
real table now. `reservations` is schema owned by T-014.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `leases`."""
    op.create_table(
        "leases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("budget_id", sa.UUID(), nullable=False),
        sa.Column("pep_id", sa.String(), nullable=False),
        sa.Column("granted", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("settled", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'released', 'expired', 'revoked')",
            name="ck_leases_state",
        ),
        sa.CheckConstraint(
            "granted >= 0 AND settled >= 0 AND settled <= granted",
            name="ck_leases_outstanding",
        ),
        sa.ForeignKeyConstraint(["budget_id"], ["budgets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_leases_budget_id_state", "leases", ["budget_id", "state"], unique=False)
    op.create_index(
        "ix_leases_expires_at_active",
        "leases",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    """Drop `leases`."""
    op.drop_index(
        "ix_leases_expires_at_active",
        table_name="leases",
        postgresql_where=sa.text("state = 'active'"),
    )
    op.drop_index("ix_leases_budget_id_state", table_name="leases")
    op.drop_table("leases")
