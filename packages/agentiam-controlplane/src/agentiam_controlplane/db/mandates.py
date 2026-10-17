"""Issuing a mandate — APICTA-02 Part B.

A mandate becomes spendable when the ledger has a pool for it. `create` writes the mandate's
row and its spend pool in one transaction — a mandate without a pool would be refused at every
PEP as `MANDATE_UNKNOWN`, and a pool without a mandate is what gap 7's foreign key now forbids
— and then announces it on `agentiam:mandates`, so every PEP can lease from the pool before the
mandate's first request arrives.

**Persist, then best-effort publish**, as revocation does (spec 07 §5.1): Redis is not the
record. A PEP that misses the event creates the pool when the mandate's first token arrives
(ADR-078), so a lost event costs that request one retryable `LEASE_UNAVAILABLE`.

`ensure_row` is for code that creates budgets directly — seed scripts, harnesses, tests — and
needs the mandate row the foreign key requires without issuing anything.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from agentiam_controlplane.db.models import BudgetRow, MandateRow

if TYPE_CHECKING:
    from datetime import datetime

    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = [
    "CHANNEL",
    "MandateExistsError",
    "MandatePublisher",
    "RedisMandatePublisher",
    "create",
    "ensure_row",
]

#: Where a new mandate is announced. PEPs subscribe; the payload names the mandate only.
CHANNEL: Final = "agentiam:mandates"


class MandatePublisher(Protocol):
    """Sends one message on one channel."""

    async def publish(self, channel: str, message: str) -> object:
        """Send `message` on `channel`."""
        ...


class RedisMandatePublisher:
    """`MandatePublisher` over a shared Redis client, as `RedisRevocationPublisher` is."""

    def __init__(self, client: Redis) -> None:
        """Hold a shared client; nothing here owns its lifecycle."""
        self._client = client

    async def publish(self, channel: str, message: str) -> object:
        """Send `message` on `channel`. Raises on a Redis error; `create()` catches it."""
        return await self._client.publish(channel, message)


class MandateExistsError(Exception):
    """A mandate with this id was already issued; nothing was written."""


async def create(
    session: AsyncSession,
    *,
    mandate_id: uuid.UUID,
    task_id: uuid.UUID,
    principal_id: str,
    spend_total: Decimal,
    expires_at: datetime,
    now: datetime,
    publisher: MandatePublisher | None = None,
) -> MandateRow:
    """Issue a mandate with a spend pool of `spend_total`, and announce it.

    Raises:
        ValueError: `spend_total` is not positive — a pool with nothing in it is a mandate
            every PEP would refuse.
        MandateExistsError: `mandate_id` was already issued.
    """
    if spend_total <= 0:
        raise ValueError(f"a mandate's spend ceiling must be positive, got {spend_total}")
    row = MandateRow(
        mandate_id=mandate_id,
        task_id=task_id,
        principal_id=principal_id,
        created_at=now,
        expires_at=expires_at,
    )
    try:
        async with session.begin():
            session.add(row)
            await session.flush()
            session.add(BudgetRow(mandate_id=mandate_id, dimension="spend_bdt", total=spend_total))
    except IntegrityError as exc:
        raise MandateExistsError(f"mandate {mandate_id} was already issued") from exc
    if publisher is not None:
        try:
            await publisher.publish(
                CHANNEL, json.dumps({"event": "mandate:created", "mandate_id": str(mandate_id)})
            )
        except Exception:
            logger.warning(
                "mandate %s issued but not announced; PEPs will create its pool on first use",
                mandate_id,
                exc_info=True,
            )
    return row


async def ensure_row(session: AsyncSession, mandate_id: uuid.UUID, *, now: datetime) -> None:
    """Make sure `mandate_id` has a row, inside the caller's transaction. Idempotent."""
    await session.execute(
        insert(MandateRow)
        .values(mandate_id=mandate_id, created_at=now)
        .on_conflict_do_nothing(index_elements=["mandate_id"])
    )
