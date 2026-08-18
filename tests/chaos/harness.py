"""The chaos harness — the invariant sidecar, the load driver, the result file (T-052).

`ROADMAP.md` line 287 states the verification for this ticket in one clause: *invariant
checker running throughout every run; commit the results.* Both halves live here, because
both are things a scenario must not be able to forget.

**The sidecar records three outcomes, not two.** A sweep either held, found violations, or
could not run at all — and the third is not a detail. CH-1 stops Postgres, so for thirty
seconds the checker cannot read the very rows it is checking. A harness that folded
"unreachable" into "holds" would report a green run for a database that was not there, and
a chaos suite whose green means nothing is worse than no chaos suite. `unavailable` is
counted separately, printed separately, and the scenario asserts on the samples that did
run plus the one taken after recovery.

**A violation is latched, never averaged.** `violations` accumulates every distinct
`Violation` the sweep ever saw. One bad sample in three hundred is a failed run: the pool
invariant is not a statistic.

Results land as one JSON file per scenario under `docs/benchmarks/chaos/`, which
`scripts/generate_chaos_results.py` folds into `docs/benchmarks/chaos-results.md`. The JSON
is the artifact and the Markdown is a view of it — writing the table by hand is how a
results table stops matching the runs it claims to describe.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentiam_controlplane.db.invariants import check_invariants

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "ChaosRun",
    "InvariantSidecar",
    "LoadReport",
    "chaos_run",
    "drive_load",
    "results_dir",
]

#: Where a scenario's JSON result goes. Overridable so a throwaway run can be pointed at a
#: scratch directory rather than at the committed evidence.
_RESULTS_ENV = "AGENTIAM_CHAOS_RESULTS_DIR"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def results_dir() -> Path:
    """The directory chaos results are written to, created if absent."""
    configured = os.environ.get(_RESULTS_ENV)
    path = Path(configured) if configured else _REPO_ROOT / "docs" / "benchmarks" / "chaos"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------------------
# the sidecar
# ---------------------------------------------------------------------------------------


@dataclass
class InvariantSidecar:
    """Sweeps the ledger's invariants on a timer for the length of a scenario.

    Read-only and lock-free (T-016), which is the property that makes running it against a
    ledger under fault injection safe rather than an extra source of contention.
    """

    engine: AsyncEngine
    interval_s: float = 0.25

    samples_held: int = 0
    samples_violated: int = 0
    samples_unavailable: int = 0
    violations: list[str] = field(default_factory=list)
    unavailable_reasons: list[str] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event | None = None

    @property
    def samples(self) -> int:
        """Every sweep attempted, whatever it found."""
        return self.samples_held + self.samples_violated + self.samples_unavailable

    @property
    def clean(self) -> bool:
        """True when no sweep ever found a violation.

        Deliberately silent about `samples_unavailable`: a scenario that deliberately takes
        the database away is not thereby failing, and it is the scenario's job — not the
        sidecar's — to say how many blind samples it will accept.
        """
        return self.samples_violated == 0

    async def sweep(self) -> bool:
        """Take one sample now. Returns True if the invariants held and could be read."""
        try:
            report = await check_invariants(self.engine)
        except Exception as exc:
            self.samples_unavailable += 1
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            if reason not in self.unavailable_reasons:
                self.unavailable_reasons.append(reason)
            return False
        if report.holds:
            self.samples_held += 1
            return True
        self.samples_violated += 1
        for violation in report.violations:
            text = str(violation)
            if text not in self.violations:
                self.violations.append(text)
        return False

    async def start(self) -> None:
        """Begin sweeping in the background."""
        self._stop = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        """Stop sweeping."""
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            await self.sweep()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                continue

    def as_dict(self) -> dict[str, Any]:
        """The sidecar's contribution to the result file."""
        return {
            "interval_s": self.interval_s,
            "samples": self.samples,
            "held": self.samples_held,
            "violated": self.samples_violated,
            "unavailable": self.samples_unavailable,
            "violations": self.violations,
            "unavailable_reasons": self.unavailable_reasons,
        }


# ---------------------------------------------------------------------------------------
# the load driver
# ---------------------------------------------------------------------------------------


@dataclass
class LoadReport:
    """What a burst of load did. Latencies are milliseconds."""

    label: str
    sent: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def ok(self) -> int:
        """Requests the gateway answered 2xx."""
        return sum(count for status, count in self.by_status.items() if status.startswith("2"))

    @property
    def dropped(self) -> int:
        """Requests that got no HTTP answer at all — a transport failure, not a refusal.

        The distinction CH-10 turns on. A 429 is the system working; a dropped connection
        is the system losing a request, and only one of those is allowed during a restart.
        """
        return len(self.errors)

    def percentile(self, p: float) -> float:
        """The `p`th percentile latency in ms, or 0.0 with no samples."""
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, round((p / 100) * len(ordered) + 0.5) - 1)
        return round(ordered[max(0, index)], 3)

    def as_dict(self) -> dict[str, Any]:
        """This report, for the result file."""
        return {
            "label": self.label,
            "sent": self.sent,
            "ok": self.ok,
            "dropped": self.dropped,
            "by_status": dict(sorted(self.by_status.items())),
            "by_reason": dict(sorted(self.by_reason.items())),
            "errors": self.errors[:5],
            "latency_ms": {
                "p50": self.percentile(50),
                "p99": self.percentile(99),
                "max": round(max(self.latencies_ms), 3) if self.latencies_ms else 0.0,
                "mean": round(statistics.fmean(self.latencies_ms), 3) if self.latencies_ms else 0.0,
            },
        }

    def record(self, status: int, latency_ms: float, reason: str | None) -> None:
        """Tally one answered request."""
        self.sent += 1
        key = str(status)
        self.by_status[key] = self.by_status.get(key, 0) + 1
        self.latencies_ms.append(latency_ms)
        if reason:
            self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def record_error(self, exc: BaseException) -> None:
        """Tally one request that never got an answer."""
        self.sent += 1
        self.errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")


async def drive_load(
    send: Callable[[int], Awaitable[tuple[int, str | None]]],
    *,
    label: str,
    total: int,
    concurrency: int = 4,
    stop_on_status: set[int] | None = None,
) -> LoadReport:
    """Issue `total` requests through `send`, `concurrency` at a time.

    `send` takes a sequence number and returns `(status, reason_code)`. Anything it raises
    is recorded as a *dropped* request rather than a refusal — see `LoadReport.dropped`.

    Args:
        send: The request to repeat.
        label: Names this burst in the result file.
        total: How many requests to issue.
        concurrency: How many run at once.
        stop_on_status: Statuses that end the burst early — used by scenarios that spend
            until refused and care about where the refusal landed, not about the tail.

    Returns:
        The tallied report.
    """
    report = LoadReport(label=label)
    counter = iter(range(total))
    halt = asyncio.Event()

    async def worker() -> None:
        for index in counter:
            if halt.is_set():
                return
            started = time.perf_counter()
            try:
                status, reason = await send(index)
            except Exception as exc:
                report.record_error(exc)
                continue
            report.record(status, (time.perf_counter() - started) * 1000, reason)
            if stop_on_status and status in stop_on_status:
                halt.set()
                return

    await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
    return report


async def drive_until(
    send: Callable[[int], Awaitable[tuple[int, str | None]]],
    *,
    label: str,
    stop: asyncio.Event,
    concurrency: int = 4,
    pace_s: float = 0.01,
) -> LoadReport:
    """Issue requests through `send` until `stop` is set.

    The duration-based twin of `drive_load`, for scenarios where the fault takes as long as
    it takes and the load has to outlast it. CH-10 needs this: a fixed request count would
    either finish before the rolling restart did — proving nothing — or have to be guessed
    high enough to outlast it, which is the same guess with extra steps.

    Args:
        send: The request to repeat.
        label: Names this burst in the result file.
        stop: Set it to end the burst.
        concurrency: How many requests run at once.
        pace_s: Pause between a worker's requests, so the load is traffic rather than a
            spin loop that starves whatever else the scenario is doing.

    Returns:
        The tallied report.
    """
    report = LoadReport(label=label)
    counter = 0

    async def worker() -> None:
        nonlocal counter
        while not stop.is_set():
            index = counter
            counter += 1
            started = time.perf_counter()
            try:
                status, reason = await send(index)
            except Exception as exc:
                report.record_error(exc)
            else:
                report.record(status, (time.perf_counter() - started) * 1000, reason)
            await asyncio.sleep(pace_s)

    await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
    return report


# ---------------------------------------------------------------------------------------
# the result file
# ---------------------------------------------------------------------------------------


@dataclass
class ChaosRun:
    """One scenario's record: what was done, when, what was measured, what held."""

    scenario: str
    title: str
    expected: str
    sidecar: InvariantSidecar
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    timeline: list[dict[str, Any]] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    loads: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    _started = 0.0

    def event(self, what: str) -> None:
        """Stamp a moment in the scenario's timeline, relative to its start."""
        self.timeline.append({"t_s": round(time.perf_counter() - self._started, 3), "event": what})

    def measure(self, name: str, value: Any) -> None:
        """Record one measurement. `Decimal` is stringified so the JSON keeps its exactness."""
        self.measurements[name] = str(value) if _is_decimal(value) else value

    def load(self, report: LoadReport) -> None:
        """Attach a load burst's report."""
        self.loads.append(report.as_dict())

    def note(self, text: str) -> None:
        """Record something a reader of the results table needs to know.

        Used for the honest caveats — a mechanism that stands in for the one `PLAN.md`
        names, or an expectation the shipped design deliberately does not meet.
        """
        self.notes.append(text)

    def as_dict(self) -> dict[str, Any]:
        """The whole result, as it is written to disk."""
        return {
            "scenario": self.scenario,
            "title": self.title,
            "expected": self.expected,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
            "invariant": self.sidecar.as_dict(),
            "timeline": self.timeline,
            "measurements": self.measurements,
            "loads": self.loads,
            "notes": self.notes,
            "verdict": "held" if self.sidecar.clean else "VIOLATED",
        }

    def write(self) -> Path:
        """Write the result JSON and return its path."""
        path = results_dir() / f"{self.scenario}.json"
        path.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        return path


def _is_decimal(value: object) -> bool:
    from decimal import Decimal

    return isinstance(value, Decimal)


@asynccontextmanager
async def chaos_run(
    scenario: str,
    *,
    title: str,
    expected: str,
    engine: AsyncEngine,
    interval_s: float = 0.25,
) -> AsyncIterator[ChaosRun]:
    """Run one scenario with the invariant checker sweeping throughout.

    The sidecar starts before the body and takes a **final sweep after it**, outside the
    fault window, so every scenario ends with at least one sample the database was actually
    able to answer. Without it a scenario that ends while Postgres is down would write a
    result whose only honest reading is "unknown".

    The result file is written even when the body raises: a scenario that fails is exactly
    the run whose timeline someone will want to read.

    Args:
        scenario: File-safe id, e.g. `CH-01`.
        title: The scenario line from `PLAN.md` §13.2.
        expected: That table's expectation, verbatim.
        engine: An engine the sidecar sweeps with. For scenarios that fault the PEP's path
            specifically, this should be a **direct** engine, so the checker can still see
            the ledger the PEP has lost.
        interval_s: Sweep cadence.

    Yields:
        The run, to be annotated as the scenario proceeds.
    """
    sidecar = InvariantSidecar(engine=engine, interval_s=interval_s)
    run = ChaosRun(scenario=scenario, title=title, expected=expected, sidecar=sidecar)
    run._started = time.perf_counter()
    await sidecar.start()
    run.event("scenario started")
    try:
        yield run
    finally:
        run.event("scenario ended")
        await sidecar.aclose()
        # The one sample that must be readable: the ledger after everything has healed.
        run.event("final sweep")
        final = await sidecar.sweep()
        run.measurements["final_sweep_readable"] = final or sidecar.samples_violated == 0
        run.duration_s = time.perf_counter() - run._started
        path = run.write()
        print(f"\n{scenario}: {run.as_dict()['verdict']} — {path}")
