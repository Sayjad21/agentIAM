"""The revocation record — T-038, spec 07 §3.1, `PLAN.md` §7.

`seq` is a DB-generated identity column, separate from the UUID `id` primary key — it exists
only to give `GET /v1/revocations?since=seq` (spec 07 §4.2) a monotonic cursor to walk, the
same split `audit_records.seq`/`audit_records.decision_id` already uses (spec 08 §3).

`block_id UNIQUE` is the entire idempotency mechanism (spec 07 §9): revoking the same block id
twice finds the row already there.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the `revocations` table."""
    op.create_table(
        "revocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("revoked_by", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("block_id", name="uq_revocations_block_id"),
        sa.CheckConstraint("scope IN ('token', 'subtree', 'mandate')", name="ck_revocations_scope"),
    )
    # No separate index on `seq`: `unique=True` already gave it a btree index, and the pull
    # query (`WHERE seq > :since ORDER BY seq`) is exactly what that index serves.


def downgrade() -> None:
    """Drop the table."""
    op.drop_table("revocations")
