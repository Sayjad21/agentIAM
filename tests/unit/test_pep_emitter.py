"""The decision-record emitter — spec 09 step 10, T-022.

Step 10 runs after the decision, so nothing here can change a verdict — except in one
direction. `PLAN.md` T-022 makes the back-pressure default **deny**: when the audit buffer is
full the request is refused, because losing an audit record is a compliance failure and a
system that cannot record what it authorized should not authorize.

That inverts the usual instinct, so the deny path is the one tested hardest here.

Two things measured before any of this was written:

* A no-op OTEL span costs **5.58 µs** — the same order as the whole decision (~5.2 µs), and
  0.56% of NFR-1's 1 ms budget. Real, affordable, and switchable.
* With no SDK installed the span's `trace_id` is all zeroes and `is_valid` is False, so
  `DecisionRecord.trace_id` cannot come from the span. It has to be supplied.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from agentiam_core.errors import ReasonCode
from agentiam_core.models import Budget, DecisionRecord, Outcome
from agentiam_pep.emitter import (
    AuditBufferFullError,
    BackPressure,
    DecisionEmitter,
    EmitterSettings,
    current_trace_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
DIGEST = "a" * 64


def a_record(**over: object) -> DecisionRecord:
    base: dict[str, object] = {
        "decision_id": uuid.uuid4(),
        "trace_id": "trace-1",
        "timestamp": NOW,
        "pep_id": "pep-1",
        "token_chain_ids": ["blk_root"],
        "principal_id": "kc:alice",
        "task_id": uuid.uuid4(),
        "agent_id": "agt-1",
        "depth": 1,
        "scope": "payment:initiate",
        "tool_id": "payment_api",
        "arg_digest": DIGEST,
        "outcome": Outcome.ALLOW,
        "reason_code": ReasonCode.OK,
        "policy_version": "bundle-1",
        "budget_before": Budget(spend_bdt=Decimal(100)),
        "budget_after": Budget(spend_bdt=Decimal(90)),
        "latency_us": 12,
    }
    return DecisionRecord(**(base | over))  # type: ignore[arg-type]


class RecordingSink:
    """Collects batches, and can be made slow or broken on demand."""

    def __init__(self) -> None:
        """Start empty, fast and working."""
        self.batches: list[list[DecisionRecord]] = []
        self.gate: asyncio.Event | None = None
        self.fail_times = 0

    async def write(self, batch: Sequence[DecisionRecord]) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("sink is down")
        self.batches.append(list(batch))

    @property
    def written(self) -> list[DecisionRecord]:
        return [record for batch in self.batches for record in batch]


@contextlib.contextmanager
def stalled(sink: RecordingSink) -> Iterator[None]:
    """Hold the sink shut for the duration of the block, then release it.

    Always releases, including when an assertion inside the block fails. Found the hard way:
    a gate left shut turns a failing test into a hung one, because `aclose()` flushes.
    """
    sink.gate = asyncio.Event()
    try:
        yield
    finally:
        sink.gate.set()


def an_emitter(sink: RecordingSink, **over: object) -> DecisionEmitter:
    base: dict[str, object] = {
        "capacity": 4,
        "back_pressure": BackPressure.DENY,
        "batch_max": 16,
        "flush_interval_s": 0.01,
    }
    return DecisionEmitter(sink, EmitterSettings(**(base | over)))  # type: ignore[arg-type]


class TestBufferedEmit:
    async def test_a_record_reaches_the_sink(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink)
        async with emitter:
            emitter.emit(a_record())
            await emitter.flush()
        assert len(sink.written) == 1

    async def test_emit_does_not_wait_for_the_sink(self) -> None:
        """The hot path enqueues and returns; the sink is somebody else's problem."""
        sink = RecordingSink()
        emitter = an_emitter(sink)
        async with emitter:
            with stalled(sink):
                emitter.emit(a_record())  # would hang if this awaited the sink
                assert sink.batches == []
            await emitter.flush()
        assert len(sink.written) == 1

    async def test_records_are_batched(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=64)
        async with emitter:
            with stalled(sink):
                for _ in range(5):
                    emitter.emit(a_record())
            await emitter.flush()
        assert len(sink.written) == 5
        assert len(sink.batches) < 5, "five records must not become five sink round trips"

    async def test_order_is_preserved(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=64)
        ids = [uuid.uuid4() for _ in range(5)]
        async with emitter:
            for decision_id in ids:
                emitter.emit(a_record(decision_id=decision_id))
            await emitter.flush()
        assert [r.decision_id for r in sink.written] == ids


class TestBackPressureDeny:
    """The default, and the one that can refuse a request. `PLAN.md` T-022."""

    async def test_a_full_buffer_denies(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=2)
        async with emitter:
            with stalled(sink):
                emitter.emit(a_record())
                emitter.emit(a_record())
                with pytest.raises(AuditBufferFullError) as caught:
                    emitter.emit(a_record())
        assert caught.value.reason_code is ReasonCode.CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED

    async def test_the_denial_names_the_audit_buffer(self) -> None:
        """An unactionable page is a bug — spec 09's whole argument about reason codes."""
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=1)
        async with emitter:
            with stalled(sink):
                emitter.emit(a_record())
                with pytest.raises(AuditBufferFullError, match="audit"):
                    emitter.emit(a_record())

    async def test_nothing_is_lost_when_it_denies(self) -> None:
        """Denying is only defensible if the records already buffered still arrive."""
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=2)
        async with emitter:
            with stalled(sink):
                emitter.emit(a_record())
                emitter.emit(a_record())
                with pytest.raises(AuditBufferFullError):
                    emitter.emit(a_record())
            await emitter.flush()
        assert len(sink.written) == 2

    async def test_capacity_frees_up_after_a_flush(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=2)
        async with emitter:
            emitter.emit(a_record())
            emitter.emit(a_record())
            await emitter.flush()
            emitter.emit(a_record())  # must not raise
            await emitter.flush()
        assert len(sink.written) == 3


class TestBackPressureDrop:
    """Opt-in, for a deployment that would rather serve than record. Not the default."""

    async def test_a_full_buffer_drops_and_counts(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=2, back_pressure=BackPressure.DROP)
        async with emitter:
            with stalled(sink):
                emitter.emit(a_record())
                emitter.emit(a_record())
                emitter.emit(a_record())  # dropped, no exception
            await emitter.flush()
        assert emitter.dropped == 1
        assert len(sink.written) == 2

    async def test_dropping_is_never_silent(self) -> None:
        """A dropped audit record that nothing counts is indistinguishable from one never made."""
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=1, back_pressure=BackPressure.DROP)
        async with emitter:
            with stalled(sink):
                for _ in range(4):
                    emitter.emit(a_record())
        assert emitter.dropped == 3


class TestNoPii:
    """NFR-5, TM-13 — a decision record is replicated into the audit ledger and the console."""

    async def test_the_emitted_payload_carries_no_argument_values(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink)
        async with emitter:
            emitter.emit(a_record())
            await emitter.flush()

        payload = json.dumps(sink.written[0].model_dump(mode="json"))
        assert DIGEST in payload, "the digest is the correlation handle and must survive"
        for leak in ("acct_", "@", "amount", "recipient"):
            assert leak not in payload, f"{leak!r} looks like an argument value"

    def test_a_record_carrying_arguments_cannot_be_built(self) -> None:
        """The model refuses it, so the emitter never has to (T-005's validator)."""
        with pytest.raises(ValueError, match="arg_digest"):
            a_record(arg_digest='{"amount": 5000}')


class TestTracing:
    async def test_a_span_is_opened_per_decision(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink)
        async with emitter:
            with emitter.decision_span("payment:initiate") as span:
                assert span is not None

    async def test_tracing_can_be_switched_off(self) -> None:
        """Measured at 5.58 us per span — 0.56% of NFR-1, but not nothing (ADR-026)."""
        sink = RecordingSink()
        emitter = an_emitter(sink, tracing=False)
        async with emitter:
            with emitter.decision_span("payment:initiate") as span:
                assert span is None

    def test_the_trace_id_helper_reports_absence_rather_than_zeroes(self) -> None:
        """With no SDK the span context is all zeroes and invalid — measured.

        Returning that as a `trace_id` would put a correlation handle that correlates
        nothing into every audit record.
        """
        assert current_trace_id() is None

    async def test_decision_span_works_with_no_scope_yet(self) -> None:
        """T-049: `Pipeline.request_span` opens this before extraction has run.

        The scope is not known at open time; omitting it must not raise or change
        whether a span is produced.
        """
        sink = RecordingSink()
        emitter = an_emitter(sink)
        async with emitter:
            with emitter.decision_span() as span:
                assert span is not None

    async def test_a_named_span_is_the_generalization_decision_span_now_uses(self) -> None:
        """T-049 adds `span(name)`, and `decision_span` is now one call to it.

        Both must honour the same on/off switch.
        """
        sink = RecordingSink()
        emitter = an_emitter(sink)
        async with emitter:
            with emitter.span("agentiam.upstream_call") as span:
                assert span is not None

    async def test_a_named_span_is_also_switched_off_by_tracing_false(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink, tracing=False)
        async with emitter:
            with emitter.span("agentiam.upstream_call") as span:
                assert span is None


class TestShutdown:
    async def test_aclose_flushes_what_is_buffered(self) -> None:
        """Shutting down with unwritten audit records is losing them quietly."""
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=64)
        await emitter.start()
        for _ in range(3):
            emitter.emit(a_record())
        await emitter.aclose()
        assert len(sink.written) == 3

    async def test_emit_after_close_is_refused(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink)
        await emitter.start()
        await emitter.aclose()
        with pytest.raises(AuditBufferFullError):
            emitter.emit(a_record())

    async def test_aclose_is_idempotent(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink)
        await emitter.start()
        await emitter.aclose()
        await emitter.aclose()
        assert sink.batches == []


class TestSinkFailure:
    async def test_a_failing_sink_does_not_kill_the_drain_loop(self) -> None:
        """One bad write must not stop every later record from being recorded."""
        sink = RecordingSink()
        sink.fail_times = 1
        emitter = an_emitter(sink, capacity=64)
        async with emitter:
            emitter.emit(a_record())
            await emitter.flush()
            emitter.emit(a_record())
            await emitter.flush()
        assert len(sink.written) >= 1
        assert emitter.failed_batches == 1

    async def test_a_failed_batch_is_counted_not_hidden(self) -> None:
        sink = RecordingSink()
        sink.fail_times = 2
        emitter = an_emitter(sink, capacity=64)
        async with emitter:
            for _ in range(2):
                emitter.emit(a_record())
                await emitter.flush()
        assert emitter.failed_batches == 2


class TestSettings:
    @pytest.mark.parametrize("capacity", [0, -1])
    def test_capacity_must_be_positive(self, capacity: int) -> None:
        with pytest.raises(ValueError, match="capacity"):
            EmitterSettings(capacity=capacity)

    @pytest.mark.parametrize("batch", [0, -3])
    def test_batch_max_must_be_positive(self, batch: int) -> None:
        with pytest.raises(ValueError, match="batch_max"):
            EmitterSettings(batch_max=batch)

    def test_flush_interval_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="flush_interval"):
            EmitterSettings(flush_interval_s=0)

    def test_max_retries_must_not_be_negative(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            EmitterSettings(max_retries=-1)

    def test_the_default_back_pressure_is_deny(self) -> None:
        """`PLAN.md` T-022, and the reason ADR-026 exists."""
        assert EmitterSettings().back_pressure is BackPressure.DENY


class TestSinkFailureIsNotSilentLoss:
    """ADR-026's argument only holds if a *failed* write does not quietly lose the record.

    Writing that ADR is what exposed the first implementation: it discarded a failed batch
    and counted it, which loses exactly the records the deny policy refuses requests to
    protect. The policy would have been an argument the code did not honour.
    """

    async def test_a_failed_batch_is_retried_not_discarded(self) -> None:
        sink = RecordingSink()
        sink.fail_times = 1
        emitter = an_emitter(sink, capacity=64)
        async with emitter:
            emitter.emit(a_record())
            await emitter.flush()  # fails
            await emitter.flush()  # retries the same batch
        assert len(sink.written) == 1, "the record must survive a transient sink failure"
        assert emitter.lost_records == 0

    async def test_a_persistently_failing_sink_fills_the_buffer_and_denies(self) -> None:
        """The chain ADR-026 claims: a broken audit path stops authorization.

        Not a slow one — a broken one. Without retry the queue would drain into nowhere and
        the gateway would happily keep authorizing with no audit trail at all.
        """
        sink = RecordingSink()
        sink.fail_times = 1000
        emitter = an_emitter(sink, capacity=2, batch_max=1, max_retries=1000)
        async with emitter:
            emitter.emit(a_record())
            await emitter.flush()
            emitter.emit(a_record())
            emitter.emit(a_record())
            with pytest.raises(AuditBufferFullError):
                emitter.emit(a_record())
        assert sink.written == []

    async def test_a_poison_batch_is_eventually_dropped_and_counted(self) -> None:
        """Retrying forever would let one unacceptable record wedge the pipeline."""
        sink = RecordingSink()
        sink.fail_times = 1000
        emitter = an_emitter(sink, capacity=64, batch_max=1, max_retries=2)
        async with emitter:
            emitter.emit(a_record())
            for _ in range(4):
                await emitter.flush()
        assert emitter.lost_records == 1
        assert emitter.failed_batches >= 3

    async def test_flush_returns_rather_than_spinning_on_a_wedged_sink(self) -> None:
        """A flush that cannot make progress must return, or aclose() never completes."""
        sink = RecordingSink()
        sink.fail_times = 1000
        emitter = an_emitter(sink, capacity=64, max_retries=1000)
        async with emitter:
            emitter.emit(a_record())
            await asyncio.wait_for(emitter.flush(), timeout=5)


class TestBackgroundDrain:
    """The drain task, exercised without an explicit flush.

    Every other test here calls `flush()`, which is the shutdown path. In production nothing
    calls it per request — records reach the sink because the background task ticks.
    """

    async def test_records_reach_the_sink_without_anyone_flushing(self) -> None:
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=64, flush_interval_s=0.01)
        await emitter.start()
        try:
            emitter.emit(a_record())
            for _ in range(200):
                await asyncio.sleep(0.01)
                if sink.written:
                    break
            await asyncio.sleep(0.05)  # let it tick again on an empty queue
        finally:
            await emitter.aclose()
        assert len(sink.written) == 1, "the background drain never wrote anything"

    async def test_start_is_idempotent(self) -> None:
        """Two drain tasks would double-write every batch."""
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=64)
        await emitter.start()
        await emitter.start()
        emitter.emit(a_record())
        await emitter.flush()
        await emitter.aclose()
        assert len(sink.written) == 1

    async def test_aclose_without_start_still_flushes(self) -> None:
        """An emitter built and shut down without ever draining must not lose its records."""
        sink = RecordingSink()
        emitter = an_emitter(sink, capacity=64)
        emitter.emit(a_record())
        await emitter.aclose()
        assert len(sink.written) == 1


class TestTraceIdWhenSdkIsPresent:
    def test_a_valid_span_context_is_rendered_as_32_hex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What `current_trace_id()` does once T-049 installs a real SDK.

        Faked rather than skipped: the branch only runs when an exporter is configured, and
        an untested branch that first executes in production is not a branch anyone has read.
        """
        from opentelemetry import trace as otel

        context = otel.SpanContext(
            trace_id=0x0123456789ABCDEF0123456789ABCDEF,
            span_id=0x0123456789ABCDEF,
            is_remote=False,
            trace_flags=otel.TraceFlags(otel.TraceFlags.SAMPLED),
        )
        monkeypatch.setattr(otel, "get_current_span", lambda: otel.NonRecordingSpan(context))
        assert current_trace_id() == "0123456789abcdef0123456789abcdef"


@pytest.mark.perf
class TestHotPathCost:
    """What step 10 adds to a request, against NFR-1's 1 ms budget.

    `decide()` itself measured ~5.2 µs (T-019). The emitter's synchronous half is a queue
    put and, when tracing is on, a span — the span alone measured 5.58 µs, so it roughly
    doubles the decision while consuming about half a percent of the budget. Both numbers
    are true and the budget is the one that matters; this pins it either way.
    """

    def test_emit_plus_span_stays_well_inside_nfr1(self, benchmark: object) -> None:
        sink = RecordingSink()
        emitter = DecisionEmitter(sink, EmitterSettings(capacity=1_000_000))
        record = a_record()

        def one_emit() -> None:
            with emitter.decision_span("payment:initiate"):
                emitter.emit(record)

        benchmark(one_emit)  # type: ignore[operator]

        timings = sorted(benchmark.stats.stats.data)  # type: ignore[attr-defined]
        p99 = timings[int(len(timings) * 0.99)]
        assert p99 < 0.0001, (
            f"p99 was {p99 * 1e6:.1f} us; step 10 must stay a rounding error against the "
            f"1 ms NFR-1 budget, not a tenth of it"
        )
