"""Published policy bundles — APICTA-01, spec 05 §5.7.3.

`serial` comes from its own sequence rather than an identity column: the bundle is signed
over its serial, so the serial has to exist *before* the row does — `nextval`, sign, insert,
in one transaction. Two concurrent activations take two serials; a refused one leaves a gap,
which rollback protection (spec 05 §5.2) does not care about.

`created_at` is nullable only because the bootstrap bundle every existing deployment holds
was signed without one; the field is inside the signature and cannot be filled in later.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `policy_bundles` and the sequence its serials come from."""
    op.execute("CREATE SEQUENCE policy_bundle_serial START 1")
    op.create_table(
        "policy_bundles",
        sa.Column("serial", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("cedar_source", sa.String(), nullable=False),
        sa.Column("entity_schema", sa.String(), nullable=True),
        sa.Column("tools", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        sa.Column("activated_by", sa.String(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_serial", sa.BigInteger(), nullable=True),
        sa.Column("nl_digest", sa.String(length=16), nullable=True),
        sa.CheckConstraint("serial >= 0", name="ck_policy_bundles_serial"),
        sa.CheckConstraint("octet_length(signature) = 64", name="ck_policy_bundles_signature"),
    )


def downgrade() -> None:
    """Drop the table and its sequence."""
    op.drop_table("policy_bundles")
    op.execute("DROP SEQUENCE policy_bundle_serial")
