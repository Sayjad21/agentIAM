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
    """A budget row — spec 04 §2.1 and §13.

    Two kinds, distinguished by `parent_budget_id`:

    * **Pool** (`parent_budget_id IS NULL`) — one per `(mandate_id, dimension)`, drawn
      from by every sibling under that mandate. The shared-pool mitigation for INV-5, and
      the default.
    * **Allocation** (`parent_budget_id` set) — one child agent's explicit slice of a
      pool, created by `split_budget`. The proportional-split mitigation.

    `allocated` is how much of this row's `total` has been carved out into child rows. It
    joins the pool invariant because budget handed to a child is spoken for: without it, a
    parent could allocate its whole pool and then lease the same money out again.

    `mandate_id` carries no foreign key: no `mandates` SQL table exists yet (T-005 built
    `Mandate` as a pure Pydantic model with no persistence). See ADR-014. `parent_budget_id`
    does get one — `budgets` is a real table.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        # T-012's guarantee, now scoped to pool rows. It has to become conditional rather
        # than disappear: allocation rows share their parent's `(mandate_id, dimension)`
        # by design, and an unconditional constraint refuses the whole feature — measured
        # before this change, a plain `UniqueViolationError`.
        Index(
            "uq_budgets_pool",
            "mandate_id",
            "dimension",
            unique=True,
            postgresql_where=text("parent_budget_id IS NULL"),
        ),
        UniqueConstraint("parent_budget_id", "agent_id", "dimension", name="uq_budgets_allocation"),
        # The pool invariant (spec 04 §2.1), enforced by the schema and not only by
        # application code: a bug that violates it must fail the transaction rather than
        # corrupt the pool.
        CheckConstraint(
            "committed >= 0 AND leased >= 0 AND allocated >= 0 "
            "AND committed + leased + allocated <= total",
            name="ck_budgets_invariant",
        ),
        # A row is a pool or an allocation, never half of each. One with a parent but no
        # agent belongs to nobody; one with an agent but no parent is a pool wearing a
        # name tag. Either would be skipped by the invariant checker's per-kind queries.
        CheckConstraint(
            "(parent_budget_id IS NULL) = (agent_id IS NULL)",
            name="ck_budgets_split_shape",
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
    allocated: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    parent_budget_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("budgets.id"), nullable=True
    )
    agent_id: Mapped[str | None] = mapped_column(nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=0)

    @property
    def available(self) -> Decimal:
        """What is left to lease or allocate: `total - committed - leased - allocated`."""
        return self.total - self.committed - self.leased - self.allocated


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
