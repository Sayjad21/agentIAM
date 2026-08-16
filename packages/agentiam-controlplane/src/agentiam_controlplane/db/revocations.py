"""Persistence for revocation records — T-038, spec 07 §4.

Two operations, both deliberately thin: `revoke()` persists (and best-effort publishes) one
block id, `pull()` reads everything past a cursor. Every rule that carries security weight is
already true of `agentiam_core.decision.decide()` (spec 07 §2 — it already walks every id in
a chain and denies on the first revoked one); this module's only job is to keep the set of
revoked ids correct and to get new ones out to every PEP quickly.

**Persist before publish, always** (spec 07 §4.1, EC-R07). A publish failure is caught and
logged here, never raised — the row is durable in Postgres before `revoke()` ever attempts to
tell anyone about it, so a dead Redis instance loses only latency, not correctness (spec 07
§5.2's pull path is what makes that true).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentiam_controlplane.db.models import RevocationRow

logger = logging.getLogger(__name__)

__all__ = ["RevocationPublisher", "pull", "revoke"]


class RevocationPublisher(Protocol):
    """Best-effort fast-path notification — spec 07 §5.1. Never the source of truth."""

    async def publish(self, *, block_id: str, seq: int) -> None:
        """Announce a revocation. May raise; `revoke()` catches it and continues."""
        ...


async def revoke(
    session: AsyncSession,
    *,
    block_id: str,
    scope: str,
    reason: str,
    revoked_by: str,
    expires_at: datetime,
    now: datetime,
    publisher: RevocationPublisher | None = None,
) -> RevocationRow:
    """Persist a revocation, then best-effort publish it.

    Idempotent on `block_id` (spec 07 §9, EC-R05): a repeat call finds the existing row and
    returns it rather than erroring or duplicating. `scope` is descriptive only (spec 07 §2)
    and is not reconciled against a prior call's `scope` on a repeat — the first revoke wins.

    Args:
        session: A fresh session.
        block_id: The biscuit revocation id being revoked.
        scope: `"token"`, `"subtree"`, or `"mandate"` — audit/console labelling only.
        reason: Free text, surfaced to whoever asks why a token stopped working.
        revoked_by: Who revoked it — an approver id, an operator, a script name.
        expires_at: The *original token's* expiry (spec 07 §3.1, §8) — when this row becomes
            safe to prune, not when the revocation itself was made.
        now: `revoked_at` for a new row; ignored for an existing one.
        publisher: Where to announce the fast path. `None` runs pull-only (spec 07 §5.2 — a
            deployment without Redis wired up is still correct, only slower).

    Returns:
        The persisted row, new or pre-existing.
    """
    async with session.begin():
        stmt = (
            pg_insert(RevocationRow)
            .values(
                block_id=block_id,
                scope=scope,
                reason=reason,
                revoked_by=revoked_by,
                revoked_at=now,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["block_id"])
            .returning(RevocationRow)
        )
        inserted = (await session.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            record = inserted
        else:
            record = (
                await session.execute(
                    select(RevocationRow).where(RevocationRow.block_id == block_id)
                )
            ).scalar_one()

    if publisher is not None:
        try:
            await publisher.publish(block_id=record.block_id, seq=record.seq)
        except Exception:
            # Never raised (spec 07 §4.1 step 3): the row above is already durable, and
            # every PEP's pull loop (spec 07 §5.2) converges on it regardless.
            logger.warning(
                "failed to publish revocation %s to the fast path; pull will still deliver it",
                block_id,
                exc_info=True,
            )

    return record


async def pull(session: AsyncSession, *, since_seq: int) -> list[RevocationRow]:
    """Every revocation with `seq > since_seq`, in order — spec 07 §4.2.

    A caller merges `entries[].block_id` into its local set and remembers the highest `seq`
    seen (or `since_seq` unchanged if the result is empty) as its next cursor.
    """
    rows = (
        (
            await session.execute(
                select(RevocationRow)
                .where(RevocationRow.seq > since_seq)
                .order_by(RevocationRow.seq)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
