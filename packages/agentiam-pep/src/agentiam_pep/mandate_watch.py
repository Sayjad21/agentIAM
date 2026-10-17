"""Noting a mandate the control plane has issued — APICTA-02 Part B, ADR-081.

The control plane announces each issued mandate on `agentiam:mandates`
(`agentiam_controlplane.db.mandates`). On each announcement this tells the PEP's
`LeasePoolRegistry` that the mandate exists, which forgets any cached refusal for it so the
next request asks the ledger instead of waiting out `unknown_ttl`.

**It does not lease.** The first draft called `ensure()`, priming a pool with a real
`ACQUIRE`, so every PEP on the channel took `lease_size` from every mandate as it was minted
— starving the one PEP that actually served it on any mandate smaller than their combined
lease size. `note_created` is the whole of what an announcement may do; the lease waits for a
token. See `LeasePoolRegistry.note_created`.

**Latency only, never correctness.** Redis pub/sub is at-most-once, and a PEP that was down
or partitioned misses the announcement; the registry creates the pool when the mandate's first
token arrives regardless (ADR-078). So, unlike `PolicySync`, there is no pull loop: the token
is the backstop. The subscription loop is `PolicySync`'s, reconnecting on any failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

__all__ = ["CHANNEL", "MandateWatch"]

#: The control plane's announcement channel (`agentiam_controlplane.db.mandates.CHANNEL`).
CHANNEL: Final = "agentiam:mandates"


class _Registry(Protocol):
    def note_created(self, mandate_id: uuid.UUID) -> None: ...


class MandateWatch:
    """Subscribes to `agentiam:mandates` and notes each announced mandate."""

    def __init__(
        self,
        *,
        registry: _Registry,
        redis_client: Redis | None,
        resubscribe_delay_s: float = 1.0,
    ) -> None:
        """Watch `redis_client` for announcements; `None` watches nothing."""
        self._registry = registry
        self._redis = redis_client
        self._resubscribe_delay_s = resubscribe_delay_s
        self._task: asyncio.Task[None] | None = None
        #: Announcements acted on, for `/readyz` and tests.
        self.received = 0

    async def start(self) -> None:
        """Begin listening in the background. Never raises; a down Redis is retried."""
        if self._redis is not None and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._subscribe_loop(self._redis))

    async def aclose(self) -> None:
        """Stop listening."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def handle_push(self, raw: bytes | str) -> bool:
        """Act on one message. Returns whether it named a mandate to note."""
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("event") != "mandate:created":
                return False
            mandate_id = uuid.UUID(str(payload["mandate_id"]))
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError, KeyError, ValueError):
            logger.warning("malformed mandate announcement ignored")
            return False
        self.received += 1
        self._registry.note_created(mandate_id)
        return True

    async def _subscribe_loop(self, redis: Redis) -> None:
        while True:
            try:
                await self._listen_once(redis)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("mandate announcement subscription dropped; retrying", exc_info=True)
                await asyncio.sleep(self._resubscribe_delay_s)

    async def _listen_once(self, redis: Redis) -> None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    await self.handle_push(message["data"])
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await pubsub.aclose()  # type: ignore[no-untyped-call]
