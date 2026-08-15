"""The audit hash chain — M4, spec 08, NFR-6, TM-12.

Two tables, and the small one is the interesting one.

`audit_records` holds the chain: each row binds the previous row's hash *inside* its own
hashed structure, so altering any record changes every hash after it.

`audit_chain_head` is a single row that every append locks. It exists because appending is a
read-modify-write and `SELECT max(seq) FOR UPDATE` cannot serialize an empty table — the first
two concurrent appends would both find nothing and both insert `seq = 1` (spec 08 §4). It is
also the only witness against head truncation: deleting the newest records leaves a chain that
verifies perfectly, and a head disagreeing with `max(seq)` is the evidence they existed.

The `CHECK` on `prev_hash` is not decoration. Without it a later record carrying a NULL
`prev_hash` would verify as a fresh genesis and hide every record before it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the chain and its head row."""
    op.create_table(
        "audit_chain_head",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_audit_chain_head_singleton"),
        sa.CheckConstraint("last_seq >= 0", name="ck_audit_chain_head_seq_nonneg"),
    )
    # Seeded here rather than lazily, so the very first append finds a row to lock.
    op.execute("INSERT INTO audit_chain_head (id, last_seq, last_hash) VALUES (1, 0, NULL)")

    op.create_table(
        "audit_records",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seq > 0", name="ck_audit_records_seq_positive"),
        sa.CheckConstraint(
            "(seq = 1 AND prev_hash IS NULL) OR (seq > 1 AND prev_hash IS NOT NULL)",
            name="ck_audit_records_genesis_only_seq_one",
        ),
        sa.UniqueConstraint("decision_id", name="uq_audit_records_decision_id"),
    )
    op.create_index("ix_audit_records_decision", "audit_records", ["decision_id"])
    # The custody query filters on the task id inside the JSONB body (spec 08 §6).
    op.execute("CREATE INDEX ix_audit_records_task ON audit_records ((record ->> 'task_id'))")


def downgrade() -> None:
    """Drop both tables. The chain is not recoverable afterwards, which is the point of it."""
    op.execute("DROP INDEX IF EXISTS ix_audit_records_task")
    op.drop_index("ix_audit_records_decision", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_table("audit_chain_head")
