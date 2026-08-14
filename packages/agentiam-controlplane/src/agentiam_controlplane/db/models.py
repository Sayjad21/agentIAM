"""ORM models for the budget ledger.

`budgets` (T-012) and `leases` (T-013) land here. `reservations` (`PLAN.md` §7) is schema
owned by T-014, the ticket that implements the operations that give it meaning — see
`docs/DECISIONS.md`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from agentiam_controlplane.db.base import Base
from agentiam_core.models import LeaseState

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


_LEASE_STATES_SQL = ", ".join(f"'{state.value}'" for state in LeaseState)


class LeaseRow(Base):
    """One held slice of a budget — spec 04 §2.2, §3.

    `granted` and `settled` are the ledger's view; `outstanding` (spec 04's name for what's
    still unspent) is derived, never stored, so it can never drift out of sync with the two
    numbers it's computed from. `PLAN.md` §7 names these columns `amount`/`remaining`
    instead — spec 04's note under §2.2 explains why the split is the correct one and
    supersedes the plan here.

    Unlike `budgets.mandate_id` (ADR-014), `budget_id` gets a real foreign key: `budgets`
    is a real table now.
    """

    __tablename__ = "leases"
    __table_args__ = (
        Index("ix_leases_budget_id_state", "budget_id", "state"),
        # Partial index, spec 04 / PLAN.md §7: only active leases are ever scanned by REAP.
        Index(
            "ix_leases_expires_at_active",
            "expires_at",
            postgresql_where=text("state = 'active'"),
        ),
        CheckConstraint(
            "granted >= 0 AND settled >= 0 AND settled <= granted",
            name="ck_leases_outstanding",
        ),
        CheckConstraint(f"state IN ({_LEASE_STATES_SQL})", name="ck_leases_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    budget_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("budgets.id"), nullable=False
    )
    pep_id: Mapped[str] = mapped_column(nullable=False)
    granted: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    settled: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(nullable=False, default=LeaseState.ACTIVE.value)

    @property
    def outstanding(self) -> Decimal:
        """`granted - settled` — spec 04 §2.2. What `RELEASE`/`REAP`/`REVOKE` return to the pool."""
        return self.granted - self.settled
