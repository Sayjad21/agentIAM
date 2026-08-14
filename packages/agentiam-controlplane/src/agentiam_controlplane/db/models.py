"""ORM models for the budget ledger.

Only `budgets` lands in T-012. `leases` and `reservations` (`PLAN.md` §7) are schema
owned by T-013 and T-014 respectively, which are the tickets that implement the
operations that give those tables meaning — see `docs/DECISIONS.md`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from agentiam_controlplane.db.base import Base

#: Matches `NUMERIC(20,4)` — the money rule (`PLAN.md` §7, `ENGINEERING-RULES.md` rule 4).
MONEY = Numeric(20, 4, asdecimal=True)


class BudgetRow(Base):
    """One ledger row per `(mandate_id, dimension)` — spec 04 §2.1.

    `mandate_id` carries no foreign key: no `mandates` SQL table exists yet (T-005 built
    `Mandate` as a pure Pydantic model with no persistence). See ADR-014.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("mandate_id", "dimension", name="uq_budgets_mandate_dimension"),
        # The pool invariant (spec 04 §2.1), enforced by the schema and not only by
        # application code: a bug that violates it must fail the transaction rather than
        # corrupt the pool.
        CheckConstraint(
            "committed >= 0 AND leased >= 0 AND committed + leased <= total",
            name="ck_budgets_invariant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mandate_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    dimension: Mapped[str] = mapped_column(nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    committed: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    leased: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    version: Mapped[int] = mapped_column(nullable=False, default=0)
