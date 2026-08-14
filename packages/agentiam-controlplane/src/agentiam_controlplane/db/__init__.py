"""The budget ledger's persistence layer (`PLAN.md` §7, `docs/specs/04-lease-protocol.md`).

Async SQLAlchemy over `asyncpg`, matching `DATABASE_URL` in `.env.example`. Alembic
migrations live in `db/migrations/`.
"""

from __future__ import annotations
