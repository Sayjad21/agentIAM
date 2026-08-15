"""Buffered emission of decision records — spec 09 step 10, T-022.

Step 10 runs *after* the decision, so nothing here can change a verdict — with one deliberate
exception. When the audit buffer is full the default is to **deny** the request, because
losing an audit record is a compliance failure and a system that cannot record what it
authorized should not authorize (`PLAN.md` T-022, ADR-026).

That is the opposite of the usual instinct, so it is the path tested hardest.

Two measurements shaped this module:

* A no-op OTEL span costs **5.58 µs**, against `decide()`'s measured ~5.2 µs and NFR-1's
  1 ms budget. So a span roughly doubles the cost of a decision and consumes 0.56% of the
  latency target. Affordable, but not free, and therefore switchable.
* With no OTEL SDK installed a span's `trace_id` is all zeroes and `is_valid` is False.
  `current_trace_id()` reports that as `None` rather than handing back a correlation handle
  that correlates nothing.

`emit()` is synchronous and never awaits: it puts on a bounded queue and returns. A
background task drains the queue in batches.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Protocol

from opentelemetry import trace

from agentiam_core.errors import ReasonCode
from agentiam_pep.errors import PepError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from types import TracebackType

    from opentelemetry.trace import Span

    from agentiam_core.models import DecisionRecord

__all__ = [
    "AuditBufferFullError",
    "BackPressure",
    "DecisionEmitter",
    "EmitterSettings",
    "RecordSink",
    "current_trace_id",
]

_TRACER = trace.get_tracer("agentiam.pep")


class AuditBufferFullError(PepError):
    """The audit buffer is full and the policy is to refuse rather than lose the record.

    Carries `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`: from the caller's point of view a
    saturated audit path is the control plane being unavailable, and the response is the
    one `PLAN.md` §6.9 already defines for that — fail closed, with a name.
    """

    reason_code = ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED


@unique
class BackPressure(StrEnum):
    """What to do when the buffer is full.

    `BLOCK` is deliberately absent — see ADR-026. Blocking the ASGI event loop until an
    audit sink drains turns a slow sink into a total outage, including health checks, which
    is strictly worse than `DENY`'s per-request refusal.
    """

    DENY = "deny"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class EmitterSettings:
    """Buffer size, drain cadence, and what happens when it fills."""

    capacity: int = 1024
    back_pressure: BackPressure = BackPressure.DENY
    batch_max: int = 64
    flush_interval_s: float = 0.5
    max_retries: int = 3
    tracing: bool = True

    def __post_init__(self) -> None:
        """Reject a configuration that cannot work."""
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.batch_max <= 0:
            raise ValueError("batch_max must be positive")
        if self.flush_interval_s <= 0:
            raise ValueError("flush_interval_s must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")


class RecordSink(Protocol):
    """Where records go. The audit ledger in production, a list in tests."""

    async def write(self, batch: Sequence[DecisionRecord]) -> None:
        """Persist a batch. May raise; the emitter counts failures and keeps draining."""
        ...


def current_trace_id() -> str | None:
    """The active span's trace id as 32 hex characters, or None if there is no real span.

    **Measured:** with only `opentelemetry-api` installed the tracer is a `ProxyTracer`, its
    spans are `NonRecordingSpan`, and the context is all zeroes with `is_valid` False. Every
    decision would otherwise carry the same meaningless `trace_id`, which is worse than
    carrying none — a correlation handle that correlates everything to everything.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


class DecisionEmitter:
    """A bounded queue, a drain task, and a policy for what happens when it fills."""

    def __init__(self, sink: RecordSink, settings: EmitterSettings | None = None) -> None:
        """Build an emitter. Nothing drains until `start()`."""
        self._sink = sink
        self._settings = settings or EmitterSettings()
        self._queue: asyncio.Queue[DecisionRecord] = asyncio.Queue(maxsize=self._settings.capacity)
        self._drain: asyncio.Task[None] | None = None
        self._closed = False
        self._pending: list[DecisionRecord] | None = None
        self._attempts = 0
        self.dropped = 0
        self.failed_batches = 0
        self.lost_records = 0

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin draining. Idempotent."""
        if self._drain is None:
            self._drain = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        """Flush what is buffered, then stop draining. Idempotent.

        Shutting down with unwritten records is losing them quietly, which is the failure
        this whole module exists to avoid — so the flush happens before the task is
        cancelled, not after.
        """
        if self._closed:
            return
        self._closed = True
        await self.flush()
        if self._drain is not None:
            self._drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain
            self._drain = None

    async def __aenter__(self) -> DecisionEmitter:
        """Start draining."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Flush and stop."""
        await self.aclose()

    # -- the hot path ---------------------------------------------------------------

    def emit(self, record: DecisionRecord) -> None:
        """Enqueue a record. Synchronous, and never waits on the sink.

        Raises:
            AuditBufferFullError: The buffer is full and the policy is `DENY`, or the
                emitter is closed. Either way the caller must refuse the request.
        """
        if self._closed:
            raise AuditBufferFullError("the audit emitter is closed; refusing to proceed")
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            if self._settings.back_pressure is BackPressure.DROP:
                # Counted, never silent: a dropped audit record nothing counts is
                # indistinguishable from one that was never made.
                self.dropped += 1
                return
            raise AuditBufferFullError(
                f"the audit buffer is full ({self._settings.capacity} records) and "
                f"back-pressure is 'deny'; refusing rather than losing the record"
            ) from None

    @contextlib.contextmanager
    def decision_span(self, scope: str) -> Iterator[Span | None]:
        """Open a span for one decision, or nothing when tracing is off.

        Measured at 5.58 µs per span with no SDK attached — 0.56% of NFR-1's budget, but the
        same order as the decision it wraps. `tracing=False` is there so T-053's load
        profile can measure both.
        """
        if not self._settings.tracing:
            yield None
            return
        with _TRACER.start_as_current_span("agentiam.decision") as span:
            span.set_attribute("agentiam.scope", scope)
            yield span

    # -- draining -------------------------------------------------------------------

    async def flush(self) -> None:
        """Write everything currently buffered, giving each batch **one** attempt.

        A failed batch is left pending and not retried here. Retries are paced by the drain
        loop's `flush_interval_s`, because retrying in a tight loop is not a retry — it
        spends the whole `max_retries` budget in microseconds, against a sink that has had
        no time to recover. Found by a test that expected a wedged sink to stay wedged and
        watched `flush()` hammer through a thousand attempts instead.
        """
        while not self._queue.empty() or self._pending is not None:
            pending_before = self._pending
            size_before = self._queue.qsize()
            await self._write_batch()
            no_progress = self._pending is not None and (
                self._pending is pending_before or self._queue.qsize() == size_before
            )
            if no_progress:
                return

    async def _write_batch(self) -> None:
        """Write one batch, retrying a failed one before giving up on it.

        A failed batch is **retried, not discarded** — up to `max_retries`. Discarding it
        would lose exactly the records this module refuses requests to protect, which would
        make the deny policy an argument the code did not honour.

        While a batch is being retried the queue keeps filling behind it, so a persistently
        failing sink drives the buffer to capacity and `DENY` starts refusing requests. That
        is the intended chain: a broken audit path stops authorization, the same way a full
        one does.

        `max_retries` is the escape hatch for a *poison* batch — one the sink will never
        accept. After that the records are dropped and counted in `lost_records`, because a
        single bad record must not wedge the pipeline forever.
        """
        if self._pending is None:
            batch: list[DecisionRecord] = []
            while len(batch) < self._settings.batch_max:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                return
            self._pending = batch
            self._attempts = 0

        try:
            await self._sink.write(self._pending)
        except Exception:  # the sink is somebody else's code; a bad write must not stop the drain
            self.failed_batches += 1
            self._attempts += 1
            if self._attempts > self._settings.max_retries:
                self.lost_records += len(self._pending)
                self._finish_pending()
            return
        self._finish_pending()

    def _finish_pending(self) -> None:
        assert self._pending is not None  # noqa: S101 - only called with a batch in hand
        for _ in self._pending:
            self._queue.task_done()
        self._pending = None
        self._attempts = 0

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._settings.flush_interval_s)
            await self._write_batch()
