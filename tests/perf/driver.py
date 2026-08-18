"""A coordinated-omission-aware load driver — T-053, `PLAN.md` §13.1.

*"HDR histograms, no averages in any reported number, coordinated-omission-aware load
generation, ≥ 3 runs with variance reported. Averages in a latency table are a red flag to
any infrastructure engineer on the panel."*

**Coordinated omission is the part most load generators get wrong, and it flatters them.**
The naive loop sends a request, waits for the response, then sends the next. When the
server stalls for a second, that loop simply issues fewer requests — and every one of them
is measured from the moment it was *sent*, which is after the stall ended. The stall
disappears from the histogram entirely. The p99 you report is then the p99 of the requests
the server was healthy enough to accept.

So latency here is measured from the time each request was **scheduled**, not from the time
it was sent. A request that should have gone out at t=3.000 s and could not start until
t=3.400 s has already accumulated 400 ms of latency before a byte moved, and it is recorded
that way. Under a healthy server the two are identical and this costs nothing; under a
stalled one it is the difference between a benchmark and an advertisement.

Requests are issued from a pool of workers rather than one at a time, because the schedule
must be kept even while earlier requests are outstanding — a single sequential caller
cannot offer 500 RPS to a server whose responses take longer than 2 ms, and would silently
measure its own pacing instead of the server's latency.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["Sample", "drive"]

#: How many concurrent in-flight requests the driver allows. Generous relative to the rates
#: T-053 asks for: at 500 RPS and a 10 ms response this needs 5, and the headroom is what
#: keeps the *schedule* rather than the concurrency limit as the thing being measured.
_WORKERS = 64

#: How often the pacer wakes. Below the ~15.6 ms Windows timer granularity on purpose —
#: the loop then wakes as fast as the platform allows and releases whatever is due, which
#: is the best a pure-Python generator can do without spinning a core.
_TICK_S = 0.002


@dataclass
class Sample:
    """One profile's measured latencies, in milliseconds.

    Three series, because one would be a lie in either direction:

    * `service_ms` — send to response. What the server took. The overhead subtraction uses
      this, since the driver's own costs appear identically in the baseline and cancel.
    * `latencies_ms` — *scheduled* time to response, the coordinated-omission-aware number.
      Never smaller than `service_ms`, and the honest one to quote for "what a client
      experienced at this offered rate".
    * `lag_ms` — scheduled to send: how late the generator was. It is a measure of the
      *harness*, not the server, and it is reported so a reader can tell whether the run
      was valid. A p99 lag of the same order as the latency means the generator, not the
      server, is what was measured.
    """

    rps: int
    latencies_ms: list[float] = field(default_factory=list)
    service_ms: list[float] = field(default_factory=list)
    lag_ms: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def sent(self) -> int:
        """Every request issued, answered or not."""
        return sum(self.statuses.values()) + len(self.errors)

    @property
    def ok(self) -> int:
        """Requests answered 2xx."""
        return sum(count for status, count in self.statuses.items() if 200 <= status < 300)

    @property
    def achieved_rps(self) -> float:
        """What the generator actually managed, which is rarely what it was asked for."""
        return round(self.sent / self.duration_s, 1) if self.duration_s else 0.0

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round(p / 100 * len(ordered) + 0.5) - 1))
        return round(ordered[index], 3)

    def percentile(self, p: float) -> float:
        """The `p`th **service** percentile in ms — send to response.

        Service rather than scheduled, because this is what the NFR-2 subtraction uses and
        a subtraction of two scheduled figures would carry the generator's lag twice.
        """
        return self._percentile(self.service_ms, p)

    def scheduled_percentile(self, p: float) -> float:
        """The `p`th coordinated-omission-aware percentile in ms — due to response."""
        return self._percentile(self.latencies_ms, p)

    def _series(self, values: list[float]) -> dict[str, float]:
        return {
            "p50": self._percentile(values, 50),
            "p95": self._percentile(values, 95),
            "p99": self._percentile(values, 99),
            "max": round(max(values), 3) if values else 0.0,
            "median": round(statistics.median(values), 3) if values else 0.0,
        }

    def as_dict(self) -> dict[str, Any]:
        """This profile, for the result file. No mean — `PLAN.md` §13.1 bans averages."""
        return {
            "target_rps": self.rps,
            "achieved_rps": self.achieved_rps,
            "sent": self.sent,
            "ok": self.ok,
            "duration_s": round(self.duration_s, 2),
            "statuses": {str(k): v for k, v in sorted(self.statuses.items())},
            "errors": self.errors[:5],
            "service_ms": self._series(self.service_ms),
            "scheduled_ms": self._series(self.latencies_ms),
            "generator_lag_ms": self._series(self.lag_ms),
        }


async def _run(url: str, token: str | None, rps: int, seconds: float) -> Sample:
    sample = Sample(rps=rps)
    headers = {"authorization": f"Bearer {token}"} if token else {}
    interval = 1.0 / rps
    total = int(rps * seconds)
    slots: asyncio.Queue[float] = asyncio.Queue()

    finished = asyncio.Event()
    limits = httpx.Limits(max_connections=_WORKERS, max_keepalive_connections=_WORKERS)
    async with httpx.AsyncClient(timeout=30.0, limits=limits, headers=headers) as client:
        started = time.perf_counter()

        async def worker() -> None:
            while True:
                try:
                    scheduled = await asyncio.wait_for(slots.get(), timeout=0.1)
                except TimeoutError:
                    if finished.is_set() and slots.empty():
                        return
                    continue
                sent = time.perf_counter()
                try:
                    response = await client.get(url)
                except Exception as exc:
                    if len(sample.errors) < 100:
                        sample.errors.append(f"{type(exc).__name__}: {str(exc)[:80]}")
                    else:
                        sample.errors.append("")
                    continue
                done = time.perf_counter()
                sample.service_ms.append((done - sent) * 1000)
                sample.latencies_ms.append((done - scheduled) * 1000)
                sample.lag_ms.append((sent - scheduled) * 1000)
                sample.statuses[response.status_code] = (
                    sample.statuses.get(response.status_code, 0) + 1
                )

        async def pacer() -> None:
            """Release slots in batches, sleeping only in units the platform can honour.

            **`asyncio.sleep(interval)` per request does not work on Windows.** The event
            loop's timer resolution is about 15.6 ms, so a 10 ms sleep takes ~15 ms and a
            2 ms sleep — what 500 RPS needs — takes the same. The pacer falls behind
            immediately, and because latency is measured from the *scheduled* time, it
            reports its own timer granularity as server latency. Measured: a 100 RPS
            baseline against a trivial stub read p50 10 ms and p99 40 ms, none of it real.

            So the schedule stays exact arithmetic and the *release* is batched: wake on a
            coarse tick, enqueue every slot now due, and let the workers take them. What
            the driver cannot control is then visible as `lag_ms` rather than hidden inside
            the latency figure.
            """
            index = 0
            while index < total:
                now = time.perf_counter()
                elapsed = now - started
                due_by_now = min(total, int(elapsed / interval) + 1)
                while index < due_by_now:
                    await slots.put(started + index * interval)
                    index += 1
                if index < total:
                    next_due = started + index * interval
                    await asyncio.sleep(max(0.0, min(_TICK_S, next_due - time.perf_counter())))
            finished.set()

        # Pacer and workers run together. Running the pacer to completion first would fill
        # the queue in a burst and then drain it as fast as the client could go, which
        # measures the client's throughput rather than the server's latency at `rps`.
        await asyncio.gather(pacer(), *(worker() for _ in range(_WORKERS)))
        sample.duration_s = time.perf_counter() - started

    return sample


def drive(url: str, *, token: str | None, rps: int, seconds: float) -> Sample:
    """Offer `rps` requests a second to `url` for `seconds`, and report what came back."""
    return asyncio.run(_run(url, token, rps, seconds))
