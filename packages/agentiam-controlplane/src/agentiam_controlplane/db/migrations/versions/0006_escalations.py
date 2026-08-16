"""The escalation queue — T-037, `PLAN.md` §11.7, §8's escalations table.

One row per human-approval request. `id` is client-generated (the core `escalation.py` module
mints it with `uuid.uuid4()` before this row exists), so — unlike `budgets.id`/`leases.id` —
there is no `server_default` here either.

`state` only ever gets written as `pending`, `approved` or `denied` by this ticket: nothing
sweeps expired rows to `expired` yet (spec: "expiry is a state, not a sweeper" — `state_at()`
computes it from `expires_at` at read time). The CHECK still allows all four `EscalationState`
values so a future sweeper does not need its own migration.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the `escalations` table."""
    op.create_table(
        "escalations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("intent_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("requested_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_reason", sa.String(), nullable=True),
        sa.UniqueConstraint("decision_id", name="uq_escalations_decision_id"),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_escalations_state",
        ),
        sa.CheckConstraint("requested_amount >= 0", name="ck_escalations_amount_nonneg"),
    )
    op.create_index("ix_escalations_state_expires", "escalations", ["state", "expires_at"])


def downgrade() -> None:
    """Drop the table."""
    op.drop_index("ix_escalations_state_expires", table_name="escalations")
    op.drop_table("escalations")
