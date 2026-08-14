"""ORM models for the budget ledger.

`budgets` (T-012) and `leases` (T-013) land here. `reservations` (T-014, `PLAN.md` §7) and
`reconciliation_anomalies` (T-014, spec 04 §11 — not in `PLAN.md`'s data model, added by the
ticket that implements the late-commit gap the spec found, see `docs/DECISIONS.md`) land
alongside them.
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


class ReservationRow(Base):
    """A settled `LEDGER_COMMIT` — spec 04 §2.2, §10, `PLAN.md` §7.

    `id` is the **client-generated UUID** minted by the PEP at `RESERVE` time (spec 04 §10);
    it is the primary key and carries no `default=uuid.uuid4`, unlike `budgets.id`/`leases.id`.
    A row here exists only once `LEDGER_COMMIT` has actually applied it to the ledger — `RESERVE`
    and `COMMIT` are PEP-local and never write to this table (spec 04 §4.2, §4.3). A second
    `LEDGER_COMMIT` for the same `id` finds this row already present and is a no-op (G4):
    that is the whole idempotency mechanism, not a separate check.

    `amount` is the *clamped* amount actually applied (spec 04 §4.4's G2), which may be less
    than what the PEP reported — this row is the ledger's record of the truth, not the PEP's.
    """

    __tablename__ = "reservations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    lease_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leases.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReconciliationAnomalyRow(Base):
    """A rejected `LEDGER_COMMIT` against a non-`active` lease — spec 04 §11, ADR-009, TM-21.

    Recorded instead of applied: `RELEASE`/`REAP`/`REVOKE` already returned this lease's
    `outstanding` to the pool, so applying the commit as well would drive `leased` negative
    (G3). The spend already happened and is not reflected in `committed` — this row is the
    reconciliation trail spec 04 §11 requires, and its count must be zero in a clean chaos run.
    """

    __tablename__ = "reconciliation_anomalies"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("leases.id"), nullable=False
    )
    reservation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    reported_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    lease_state: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
