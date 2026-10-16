"""Keeping the PEP's policy current — APICTA-01 Part C, spec 05 §5.7.4.

`PolicyCache` holds the bundle being enforced and decides whether a candidate may replace it.
This module is the background work that finds candidates, in the shape `RedisRevocationSet`
already uses for revocations (spec 07 §5):

* **Push.** The control plane publishes `{"event": "policy:reload", "serial": n}` on
  `agentiam:policy` after a bundle commits. On any such event the PEP fetches
  `GET /v1/policy/bundles/current` — *current*, not *n*, so events that arrive out of order or
  are coalesced still converge on the newest.
* **Pull.** Every `pull_interval_s` the PEP fetches regardless. Redis pub/sub is at-most-once:
  a PEP that was restarting or partitioned when the event fired would otherwise miss it.
* **Confirm.** A fetch that returns the held bundle — same serial, verified signature, same
  content — calls `PolicyCache.confirm()`, which is what keeps an unchanged policy from going
  stale (spec 05 §5.3 as amended).

Nothing here is on the request path. The hot path reads the cache; this only ever writes it,
under one lock so a push and a pull cannot race each other into a load.

Each fetch ends in one of: `loaded`, `confirmed`, `behind` (the control plane's current is
older than what the PEP holds — not evidence of anything, so not a confirmation), `mismatch`
(a validly signed but different bundle under the held serial), `rejected` (malformed, forged,
or unparseable — counted by the cache too), `absent` (nothing published), `unavailable`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import Counter
from typing import TYPE_CHECKING, Final

import httpx

from agentiam_core.bundles import (
    BundleSignatureError,
    BundleWireError,
    bundle_from_wire,
    verify_bundle,
)
from agentiam_pep.policy_cache import BundleRejected

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from agentiam_pep.policy_cache import PolicyCache

logger = logging.getLogger(__name__)

__all__ = ["CHANNEL", "PolicySync"]

#: Where the control plane announces new serials (spec 05 §5.7.4).
CHANNEL: Final = "agentiam:policy"

_CURRENT: Final = "/v1/policy/bundles/current"


class PolicySync:
    """Fetches the control plane's current bundle on push, on a timer, and once at boot."""

    def __init__(
        self,
        *,
        cache: PolicyCache,
        control_plane_client: httpx.AsyncClient,
        redis_client: Redis | None,
        pull_interval_s: float = 30.0,
        boot_timeout_s: float = 5.0,
        fetch_timeout_s: float = 5.0,
        resubscribe_delay_s: float = 1.0,
    ) -> None:
        """Wire to a cache, a control-plane client and, optionally, Redis.

        Args:
            cache: The cache the pipeline reads. Only this class writes it after boot.
            control_plane_client: Based at the control plane. Not owned.
            redis_client: Subscribes to `CHANNEL`. `None` runs pull-only. Not owned.
            pull_interval_s: The backstop's period (spec 05 §5.7.4, default 30 s).
            boot_timeout_s: How long `start()` waits for the first fetch before serving the
                boot bundle anyway.
            fetch_timeout_s: Bound on one fetch.
            resubscribe_delay_s: Backoff after a dropped push connection.
        """
        self._cache = cache
        self._client = control_plane_client
        self._redis = redis_client
        self._pull_interval_s = pull_interval_s
        self._boot_timeout_s = boot_timeout_s
        self._fetch_timeout_s = fetch_timeout_s
        self._resubscribe_delay_s = resubscribe_delay_s
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self.outcomes: Counter[str] = Counter()
        self.last_outcome: str | None = None

    # -- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        """Try one fetch, bounded, then start the push and pull loops.

        Never raises for an unreachable control plane: the PEP serves its boot bundle, which
        goes stale after `max_staleness` unless a later sync confirms or replaces it — fail
        closed on age, as spec 05 §5.3 intends.
        """
        try:
            await asyncio.wait_for(self.sync_once(), timeout=self._boot_timeout_s)
        except TimeoutError:
            logger.warning(
                "control plane did not answer the boot policy sync within %.1fs; serving the "
                "boot bundle",
                self._boot_timeout_s,
            )
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._pull_loop()))
        if self._redis is not None:
            self._tasks.append(loop.create_task(self._subscribe_loop(self._redis)))

    async def aclose(self) -> None:
        """Stop both loops."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    def status(self) -> dict[str, object]:
        """What `/readyz` reports beside the cache's own status."""
        return {
            "last_sync": self.last_outcome,
            "outcomes": dict(self.outcomes),
            **self._cache.status(),
        }

    # -- one sync -----------------------------------------------------------------

    async def sync_once(self) -> str:
        """Fetch the current bundle and apply it. Returns the outcome; see the module doc."""
        async with self._lock:
            outcome = await self._sync()
        self.outcomes[outcome] += 1
        self.last_outcome = outcome
        return outcome

    async def _sync(self) -> str:
        try:
            response = await self._client.get(_CURRENT, timeout=self._fetch_timeout_s)
        except httpx.HTTPError as exc:
            logger.info("policy sync could not reach the control plane: %r", exc)
            return "unavailable"
        if response.status_code == 404:
            return "absent"
        if response.status_code != 200:
            logger.info("policy sync got HTTP %d from the control plane", response.status_code)
            return "unavailable"

        try:
            bundle, signature = bundle_from_wire(response.json())
        except (BundleWireError, ValueError) as exc:
            self._cache.rejected += 1
            logger.warning("policy sync refused a malformed bundle: %s", exc)
            return "rejected"

        held = self._cache.serial
        if held is not None and bundle.serial < held:
            logger.warning(
                "control plane's current bundle (serial %d) is older than the one held (%d)",
                bundle.serial,
                held,
            )
            return "behind"

        if held is not None and bundle.serial == held:
            # A confirmation restarts the staleness clock, so it needs the same evidence a
            # load does — and the content must be the held bundle's, not merely its serial.
            try:
                verify_bundle(bundle, signature, self._cache.public_key)
            except BundleSignatureError as exc:
                self._cache.rejected += 1
                logger.warning("policy sync refused an unverifiable confirmation: %s", exc)
                return "rejected"
            if bundle != self._cache.bundle:
                logger.warning("a different bundle is being served under held serial %d", held)
                return "mismatch"
            self._cache.confirm(held)
            return "confirmed"

        try:
            self._cache.load(bundle, signature)
        except BundleRejected as exc:
            logger.warning("policy sync refused bundle serial %d: %s", bundle.serial, exc)
            return "rejected"
        logger.info("policy bundle serial %d (%s) loaded", bundle.serial, bundle.version)
        return "loaded"

    # -- push ---------------------------------------------------------------------

    async def handle_push(self, raw: bytes | str) -> bool:
        """Act on one pub/sub message. Returns whether it was a reload event."""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            logger.warning("malformed policy push message ignored")
            return False
        if not isinstance(payload, dict) or payload.get("event") != "policy:reload":
            return False
        await self.sync_once()
        return True

    async def _subscribe_loop(self, redis: Redis) -> None:
        """Reconnect on any failure; correctness never depends on this, latency does."""
        while True:
            try:
                await self._listen_once(redis)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("policy push subscription dropped; retrying", exc_info=True)
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

    # -- pull ---------------------------------------------------------------------

    async def _pull_loop(self) -> None:
        while True:
            await asyncio.sleep(self._pull_interval_s)
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("policy pull failed; retrying in %.1fs", self._pull_interval_s)
