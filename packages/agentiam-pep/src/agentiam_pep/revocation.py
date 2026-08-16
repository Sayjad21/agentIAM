"""The revocation set the PEP consults at step 3 — T-023/T-038, `PLAN.md` §6.7, spec 07.

**`InMemoryRevocationSet` is not a stub, and the distinction matters.** An empty revocation
set is not a pretend answer: before T-038, nothing in this system could revoke anything, so
*no token is revoked* was the true state of the world, and reporting it was honest.

Compare with what T-023 deliberately did **not** ship: an allow-all `PolicyEngine` would have
reported that policy was evaluated when no policy existed (ADR-027). The difference is whether
the component is telling the truth about work it did, and this one is.

`RedisRevocationSet` is what T-038 replaces the *source* with: Redis pub/sub for the fast
path plus a periodic full-set pull as a correctness backstop (spec 07 §5), feeding the same
shape `InMemoryRevocationSet` already has. The `RevocationOracle` protocol `decide()` is
written against does not change — `is_revoked(revocation_id) -> bool`, synchronous, in
memory, no network — which is why a background consumer can replace the source without
`decision.py` changing at all.

**T-039** adds a counting Bloom filter as the *first* check inside `RedisRevocationSet
.is_revoked()` (ADR-044): a negative answer returns `False` immediately, skipping the exact
set entirely — the O(1)-at-scale win spec 07 §3.2 describes. A positive answer is never
trusted on its own (a Bloom filter's positives include false positives by construction) —
it falls through to the same exact-set membership check this class already had, which is
authoritative. This is purely additive: nothing is revoked that would not have been
revoked before, and every id the exact set alone would report is still reported, since a
Bloom filter has zero false *negatives* by construction, and `_bloom` is updated everywhere
`_revoked` is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastbloom_rs import FilterBuilder

from agentiam_core.decision import OracleUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import httpx
    from fastbloom_rs import CountingBloomFilter
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

__all__ = ["InMemoryRevocationSet", "RedisRevocationSet"]

#: `PLAN.md` §8, spec 07 §5.1. Not imported from `agentiam_controlplane` — the two packages
#: are separate deployables that agree on this literal via the spec, the way they already
#: agree on route configs and reason codes without a shared runtime dependency between them.
CHANNEL = "agentiam:revocations"


class InMemoryRevocationSet:
    """A `RevocationOracle` over a set held in this process.

    Consulted on every decision, so membership is a set lookup and nothing else. INV-10 (no
    resurrection) is enforced by `decide()`, which checks every id in the chain and treats a
    revoked *ancestor* as fatal — this class only answers about one id at a time.
    """

    def __init__(self, revoked: Iterable[str] = (), *, unavailable: str | None = None) -> None:
        """Build the set. Empty is the normal state until T-038.

        Args:
            revoked: Block revocation ids already known to be revoked.
            unavailable: If set, every lookup raises `OracleUnavailable` with this reason —
                the shape T-038 needs when its data is too stale to answer from.
        """
        self._revoked = set(revoked)
        self._unavailable = unavailable

    def is_revoked(self, revocation_id: str) -> bool:
        """Whether this block id has been revoked.

        Raises:
            OracleUnavailable: The set cannot be trusted. `decide()` fails closed on this.
        """
        if self._unavailable is not None:
            raise OracleUnavailable(self._unavailable)
        return revocation_id in self._revoked

    def revoke(self, revocation_id: str) -> None:
        """Mark a block id revoked.

        Present so T-023's slice and the demo's Beat 7 can exercise the deny path before the
        revocation service exists. T-038 replaces the *source* of these ids, not this shape.
        """
        self._revoked.add(revocation_id)

    def __len__(self) -> int:
        """How many ids are known revoked — what `/readyz` reports."""
        return len(self._revoked)


class RedisRevocationSet:
    """A `RevocationOracle` kept fresh by Redis pub/sub push plus an HTTP pull backstop.

    `is_revoked()` — the only method `decide()` calls — stays a synchronous, in-memory set
    lookup (spec 07 §4.3). Two background asyncio tasks feed it:

    * **Push** (`_subscribe_loop`): a fast path, milliseconds after `REVOKE`. If the
      connection drops it reconnects and resubscribes on a short delay rather than dying —
      but nothing about correctness depends on it succeeding at all (spec 07 §5.1).
    * **Pull** (`_pull_loop`): the correctness backstop, on a fixed `pull_interval`. This is
      what makes EC-R06 (a dropped push) and EC-R07 (Redis down) converge to the same state
      as a PEP that saw every push — bounded by `pull_interval`, not by push delivery.

    **Cold start:** `start()` performs one synchronous pull *before* returning and before
    either background task is launched (spec 07 §7) — a freshly-started PEP must not answer
    from an empty set it hasn't yet earned the right to trust.

    **Staleness → fail closed** (spec 07 §5.3): if the last successful pull is older than
    `staleness_limit_s`, `is_revoked()` raises `OracleUnavailable` rather than answer from a
    set it can no longer vouch for. `decide()` already turns that into
    `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` (rule 6).

    **Bloom filter (T-039, ADR-044):** `_bloom` is a Rust-backed counting Bloom filter
    (`fastbloom_rs`) mirroring `_revoked` — every id added to one is added to the other, in
    both `_pull_once` and `_handle_push`. `is_revoked()` checks `_bloom` first; a negative
    answer is trusted outright (a Bloom filter has zero false negatives by construction) and
    returned without touching `_revoked` at all. A positive answer is not proof — it falls
    through to the real `_revoked` membership check, which is authoritative. `bloom_capacity`
    should be sized to the expected number of live revocations (EC-R10: 10,000); past that
    size the filter's false-positive rate degrades but correctness does not, since every
    positive still falls through to `_revoked`.
    """

    def __init__(
        self,
        *,
        redis_client: Redis,
        control_plane_client: httpx.AsyncClient,
        pull_interval_s: float = 3.0,
        staleness_limit_s: float = 9.0,
        resubscribe_delay_s: float = 1.0,
        bloom_capacity: int = 10_000,
        bloom_false_positive_rate: float = 0.01,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Hold the clients; nothing runs until `start()` is awaited.

        Args:
            redis_client: Subscribes to `CHANNEL`. Not owned — the caller closes it.
            control_plane_client: An `httpx.AsyncClient` whose `base_url` is the control
                plane; `GET /v1/revocations?since=` is called against it. Not owned either.
            pull_interval_s: How often the backstop pull runs.
            staleness_limit_s: How long since the last successful pull before `is_revoked()`
                stops answering. Spec 07 §5.3 proposes 3x `pull_interval_s` — a single missed
                tick from ordinary jitter should not trip it, a stuck consumer should.
            resubscribe_delay_s: Backoff between a dropped push connection and retrying.
            bloom_capacity: Expected number of live revocations (EC-R10's 10,000, by
                default) — sizes the Bloom filter's bit array and hash count.
            bloom_false_positive_rate: Tolerable Bloom false-positive rate at
                `bloom_capacity`. Only affects how often a lookup pays the `_revoked`
                fallthrough, never correctness (§ above).
            now: Injected clock, so staleness is testable without real sleeps.
        """
        self._redis = redis_client
        self._http = control_plane_client
        self._pull_interval_s = pull_interval_s
        self._staleness_limit_s = staleness_limit_s
        self._resubscribe_delay_s = resubscribe_delay_s
        self._now = now

        self._revoked: set[str] = set()
        # `CountingBloomFilter` itself resolves to `Any` under the `fastbloom_rs.*`
        # `ignore_missing_imports` override above — the annotation still documents the
        # real type for readers, `disallow_any_unimported` is what's being silenced.
        self._bloom: CountingBloomFilter = FilterBuilder(  # type: ignore[no-any-unimported]
            bloom_capacity, bloom_false_positive_rate
        ).build_counting_bloom_filter()
        self._since_seq = 0
        self._last_pull_at: datetime | None = None

        self._subscribe_task: asyncio.Task[None] | None = None
        self._pull_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Pull once synchronously (spec 07 §7 cold start), then start both background loops.

        Idempotent: a second call while already started does nothing further.
        """
        await self._pull_once()
        loop = asyncio.get_running_loop()
        if self._subscribe_task is None:
            self._subscribe_task = loop.create_task(self._subscribe_loop())
        if self._pull_task is None:
            self._pull_task = loop.create_task(self._pull_loop())

    async def aclose(self) -> None:
        """Cancel both background loops. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for task in (self._subscribe_task, self._pull_task):
            if task is not None:
                task.cancel()
        for task in (self._subscribe_task, self._pull_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._subscribe_task = None
        self._pull_task = None

    def is_revoked(self, revocation_id: str) -> bool:
        """Whether this block id has been revoked, per the last successful pull.

        The Bloom filter (T-039) is checked first: a negative is returned immediately, an
        O(1) answer that skips `_revoked` entirely. A positive falls through to `_revoked`,
        the authoritative check, since a Bloom filter's positives are not proof.

        Raises:
            OracleUnavailable: No pull has succeeded recently enough to trust the set
                (spec 07 §5.3). `decide()` fails closed on this.
        """
        if self._last_pull_at is None:
            raise OracleUnavailable("no revocation pull has ever succeeded")
        age = self._now() - self._last_pull_at
        if age >= timedelta(seconds=self._staleness_limit_s):
            raise OracleUnavailable(
                f"revocation data is {age.total_seconds():.1f}s stale, "
                f"over the {self._staleness_limit_s}s limit"
            )
        if not self._bloom.contains_str(revocation_id):
            return False
        return revocation_id in self._revoked

    def __len__(self) -> int:
        """How many ids are known revoked — what `/readyz` reports."""
        return len(self._revoked)

    async def _pull_once(self) -> None:
        """One `GET /v1/revocations?since=` round trip, merged into the set.

        A failure is logged and left for the next tick — `_last_pull_at` is only advanced on
        success, which is what lets staleness (§5.3) detect a control plane that has been
        unreachable for too long rather than papering over one bad request.
        """
        try:
            response = await self._http.get("/v1/revocations", params={"since": self._since_seq})
            response.raise_for_status()
            body = response.json()
        except Exception:
            logger.warning("revocation pull failed", exc_info=True)
            return
        for entry in body["entries"]:
            block_id = entry["block_id"]
            self._revoked.add(block_id)
            self._bloom.add_str(block_id)
        self._since_seq = body["next_seq"]
        self._last_pull_at = self._now()

    async def _pull_loop(self) -> None:
        while True:
            await asyncio.sleep(self._pull_interval_s)
            await self._pull_once()

    async def _subscribe_loop(self) -> None:
        """Reconnect on any failure.

        Nothing about correctness depends on this succeeding, but a subscription that dies
        silently once would degrade latency to `pull_interval` forever with no visibility,
        which is worth avoiding even though it stays correct.
        """
        while True:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("revocation push subscription dropped; retrying", exc_info=True)
                await asyncio.sleep(self._resubscribe_delay_s)

    async def _listen_once(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                self._handle_push(message["data"])
        finally:
            await pubsub.unsubscribe(CHANNEL)
            # `PubSub.aclose` ships without a type annotation in redis-py's own source
            # (measured against 8.1.0, `py.typed` present) despite the package being marked
            # typed, so this one call is untyped in an otherwise strict context.
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    def _handle_push(self, raw: bytes | str) -> None:
        try:
            payload = json.loads(raw)
            block_id = str(payload["block_id"])
            self._revoked.add(block_id)
            self._bloom.add_str(block_id)
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("malformed revocation push message: %r", raw)
