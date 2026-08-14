"""reservations, reconciliation_anomalies — T-014, PLAN.md §7, spec 04 §2.2, §10, §11.

`reservations.id` is the client-generated UUID minted by the PEP at `RESERVE` (spec 04 §10);
it carries no server default. `reconciliation_anomalies` is not in `PLAN.md`'s data model —
it exists because spec 04 §11's model-checking found the late-commit gap in `PLAN.md` §6.4's
original pseudocode and requires the rejection to be recorded, not silently dropped.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `reservations` and `reconciliation_anomalies`."""
    op.create_table(
        "reservations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("lease_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "reconciliation_anomalies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("lease_id", sa.UUID(), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("reported_amount", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("lease_state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lease_id"], ["leases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop `reconciliation_anomalies` and `reservations`."""
    op.drop_table("reconciliation_anomalies")
    op.drop_table("reservations")
