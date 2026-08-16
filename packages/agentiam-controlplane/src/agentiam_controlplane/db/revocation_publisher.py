"""The `RevocationPublisher` that connects `revoke()` to the fast path — T-038, spec 07 §5.1.

Satisfies `agentiam_controlplane.db.revocations.RevocationPublisher` structurally. One Redis
client per process, shared across calls — the same "one pool for the process" reasoning
`PepSettings.build_client()` already applies to the upstream `httpx` client.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["CHANNEL", "RedisRevocationPublisher"]

#: `PLAN.md` §8. Every revocation is announced here; `pull()` is the correctness backstop
#: for anyone who is not listening or misses a message (spec 07 §5.2).
CHANNEL = "agentiam:revocations"


class RedisRevocationPublisher:
    """Publishes `{block_id, seq}` to `CHANNEL` on every revoke."""

    def __init__(self, client: Redis) -> None:
        """Hold a shared client; nothing here owns its lifecycle."""
        self._client = client

    async def publish(self, *, block_id: str, seq: int) -> None:
        """Announce one revocation. Raises on a Redis error; `revoke()` catches it."""
        await self._client.publish(CHANNEL, json.dumps({"block_id": block_id, "seq": seq}))
