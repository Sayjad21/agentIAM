"""Escalation delivery — APICTA-03.

`mandate_id` and `agent_path` are what the PEP knew about the requesting token when it opened
the escalation: the pool the elevated token must spend from, and the delegation path its role
assignment is keyed on. `grant` is the elevated token itself, with its expiry, scopes and
amount, written in the same transaction that approves the row so an approval can never be
committed without the token the agent is waiting for.

All three are nullable: rows opened before this migration, or through `POST /v1/escalations`
without a token in hand, have none of them and mint as T-037 did.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the requesting token's mandate and path, and the stored grant."""
    op.add_column("escalations", sa.Column("mandate_id", postgresql.UUID(), nullable=True))
    op.add_column("escalations", sa.Column("agent_path", postgresql.JSONB(), nullable=True))
    op.add_column("escalations", sa.Column("grant", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    """Drop the three columns."""
    op.drop_column("escalations", "grant")
    op.drop_column("escalations", "agent_path")
    op.drop_column("escalations", "mandate_id")
