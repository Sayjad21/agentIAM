"""Mandates — APICTA-02 Part B, gap 7.

A `mandates` table, a row for every mandate an existing budget already names, and then the
foreign key from `budgets.mandate_id` that ADR-014 could not add while no table existed. The
backfill comes first because the key would otherwise refuse the upgrade on any ledger that
has budgets — the demo's, at the very least.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `mandates`, backfill it from `budgets`, then key `budgets` to it."""
    op.create_table(
        "mandates",
        sa.Column("mandate_id", postgresql.UUID(), primary_key=True),
        sa.Column("task_id", postgresql.UUID(), nullable=True),
        sa.Column("principal_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "INSERT INTO mandates (mandate_id, created_at) "
        "SELECT DISTINCT mandate_id, now() FROM budgets"
    )
    op.create_foreign_key(
        "fk_budgets_mandate", "budgets", "mandates", ["mandate_id"], ["mandate_id"]
    )


def downgrade() -> None:
    """Drop the key, then the table."""
    op.drop_constraint("fk_budgets_mandate", "budgets", type_="foreignkey")
    op.drop_table("mandates")
