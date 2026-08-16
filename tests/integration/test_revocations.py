"""Integration tests for revocation persistence and the fast-path publisher — T-038.

Real Postgres via testcontainers (idempotency, pull ordering) and a real Redis testcontainer
(publish delivery, and EC-R07: revoke must still succeed and persist when Redis is
unreachable — a mock could not prove that, only a Redis actually taken down can).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as redis_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.revocation_publisher import CHANNEL, RedisRevocationPublisher
from agentiam_controlplane.db.revocations import pull, revoke

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
_EXPIRES = _NOW + timedelta(hours=1)


async def test_revoke_persists_a_row(migrated_engine: AsyncEngine) -> None:
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        record = await revoke(
            s,
            block_id="a" * 128,
            scope="token",
            reason="stolen",
            revoked_by="kc:manager",
            expires_at=_EXPIRES,
            now=_NOW,
        )
    assert record.block_id == "a" * 128
    assert record.scope == "token"
    assert record.seq >= 1


async def test_revoking_the_same_block_id_twice_is_idempotent(
    migrated_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        first = await revoke(
            s,
            block_id="b" * 128,
            scope="token",
            reason="stolen",
            revoked_by="kc:manager",
            expires_at=_EXPIRES,
            now=_NOW,
        )
    async with factory() as s:
        second = await revoke(
            s,
            block_id="b" * 128,
            scope="mandate",  # different label; the first call's row wins (spec 07 §2, §9)
            reason="different reason",
            revoked_by="kc:cfo",
            expires_at=_EXPIRES,
            now=_NOW + timedelta(minutes=5),
        )
    assert second.id == first.id
    assert second.seq == first.seq
    assert second.scope == "token"
    assert second.revoked_by == "kc:manager"


async def test_pull_returns_entries_after_the_cursor_in_order(
    migrated_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        first = await revoke(
            s,
            block_id="c" * 128,
            scope="token",
            reason="r1",
            revoked_by="kc:manager",
            expires_at=_EXPIRES,
            now=_NOW,
        )
    async with factory() as s:
        second = await revoke(
            s,
            block_id="d" * 128,
            scope="subtree",
            reason="r2",
            revoked_by="kc:manager",
            expires_at=_EXPIRES,
            now=_NOW,
        )

    async with factory() as s:
        everything = await pull(s, since_seq=0)
    ids = [r.block_id for r in everything]
    assert ids.index("c" * 128) < ids.index("d" * 128)

    async with factory() as s:
        only_new = await pull(s, since_seq=first.seq)
    assert [r.id for r in only_new] == [second.id]

    async with factory() as s:
        nothing = await pull(s, since_seq=second.seq)
    assert nothing == []


async def test_revoke_with_no_publisher_still_persists(migrated_engine: AsyncEngine) -> None:
    """`publisher=None` is the pull-only deployment shape (spec 07 §5.2) — must not error."""
    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        record = await revoke(
            s,
            block_id="e" * 128,
            scope="token",
            reason="no redis wired up",
            revoked_by="kc:manager",
            expires_at=_EXPIRES,
            now=_NOW,
            publisher=None,
        )
    assert record.block_id == "e" * 128


async def test_revoke_publishes_to_the_channel(
    migrated_engine: AsyncEngine, redis_url: str
) -> None:
    factory = make_session_factory(migrated_engine)
    client = redis_asyncio.Redis.from_url(redis_url)
    subscriber = client.pubsub()
    await subscriber.subscribe(CHANNEL)
    await subscriber.get_message(timeout=2)  # the subscribe confirmation

    publisher = RedisRevocationPublisher(client)
    async with factory() as s:
        record = await revoke(
            s,
            block_id="f" * 128,
            scope="token",
            reason="published",
            revoked_by="kc:manager",
            expires_at=_EXPIRES,
            now=_NOW,
            publisher=publisher,
        )

    message = await subscriber.get_message(timeout=2)
    assert message is not None
    payload = json.loads(message["data"])
    assert payload == {"block_id": "f" * 128, "seq": record.seq}

    await subscriber.aclose()  # type: ignore[no-untyped-call]  # see agentiam_pep.revocation
    await client.aclose()


async def test_revoke_persists_even_when_redis_is_unreachable(
    migrated_engine: AsyncEngine, redis_container: object
) -> None:
    """EC-R07: Redis down during revocation.

    The row must still land, and revoke() must not raise — a mock publisher could not prove
    this; the container is actually stopped.
    """
    from testcontainers.community.redis import RedisContainer

    assert isinstance(redis_container, RedisContainer)
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(redis_container.port)
    client = redis_asyncio.Redis.from_url(
        f"redis://{host}:{port}", socket_connect_timeout=1, socket_timeout=1
    )
    publisher = RedisRevocationPublisher(client)

    # A real docker stop, not `RedisContainer.stop()` — that method also *removes* the
    # container, which would make the fixture's own teardown fail trying to remove it again.
    redis_container.get_wrapped_container().stop()

    factory = make_session_factory(migrated_engine)
    async with factory() as s:
        record = await revoke(
            s,
            block_id="9" * 128,
            scope="token",
            reason="redis is down",
            revoked_by="kc:manager",
            expires_at=_EXPIRES,
            now=_NOW,
            publisher=publisher,
        )
    assert record.block_id == "9" * 128

    async with factory() as s:
        fetched = await pull(s, since_seq=record.seq - 1)
    assert [r.block_id for r in fetched] == ["9" * 128]
