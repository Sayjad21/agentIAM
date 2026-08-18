<!--
  GENERATED FILE — do not edit.

  Regenerate with:  uv run python scripts/generate_benchmark_results.py
  Sources:          docs/benchmarks/pb2-breakdown.json   (uv run pytest -m perf)
                    docs/benchmarks/nfr2-load.json       (uv run python scripts/run_load_test.py)
  Verify in CI:     uv run python scripts/generate_benchmark_results.py --check
-->

# Performance

**NFR-1 and NFR-2 are different measurements and are never added together.** NFR-1 is the
authorization decision, in process, excluding all network I/O. NFR-2 is the proxy's
end-to-end overhead under load. `PLAN.md` §1.5: *"Python cannot proxy a request in under a
millisecond. It can evaluate an authorization decision in well under a millisecond. Always
report the two numbers separately, and label them."*

No averages appear below. Every figure is a percentile or a median, per `PLAN.md` §13.1.


## NFR-1 — the authorization decision, in process

Budget: **p99 < 1 ms**. Measured 2026-08-18T14:58:32.111067+00:00.

Every step is the real implementation: a real biscuit chain, the real Cedar engine, real Datalog evaluation. The ledger and the audit sink are absent because they are off the hot path by construction — including them would measure the thing the design removed.

| Step | median µs | p99 µs |
|---|---|---|
| step 1 — extract (route, args, digest) | 20.3 | 55.0 |
| step 2 — verify (biscuit signature) | 212.5 | 415.7 |
| step 4 — caveats (4 clauses, Datalog) | 3.6 | 8.7 |
| step 5 — policy (Cedar) | 143.1 | 298.2 |
| step 10 — audit record hash | 16.1 | 58.5 |
| **`decide()` total — NFR-1** | 151.1 | 373.7 |

**NFR-1 holds**: `decide()` p99 is 373.7 µs against a 1000 µs budget, and `PLAN.md` §17's R-2 trigger (2 ms, port to Rust) is not approached.

Two things the breakdown says that a single number cannot:

- **Policy evaluation is nearly the whole decision** — 143.1 µs of a 151.1 µs median. Everything else inside `decide()` is single-digit microseconds.
- **`verify()` is the most expensive step and sits *outside* `decide()`** at 212.5 µs median. Per-request in-process cost is verify + decide + extract, so roughly 384 µs — that, not NFR-1 alone, is what bounds throughput.

> An earlier figure of **~5 µs** was published for NFR-1 in `STATUS.md` and the T-019 commit. It is a real measurement of `decide()` with `FakePolicy`, a stub that returns one fixed verdict, and it therefore excludes the most expensive thing in the path. It is superseded here. `tests/unit/test_decision.py::TestNfr1` still measures the stubbed path and is still useful — it isolates everything *except* policy — but the number to quote is the one above.


## NFR-2 — proxy overhead under load

Budget: **p99 < 8 ms at 500 RPS, single instance**. Measured 2026-08-18T15:14:51.543885+00:00.

Three tiers at every rate, so each subtraction compares like with like:

1. the stub upstream alone, no PEP in the path;
2. the same request through the PEP with **no pipeline attached** (T-018 transport mode) — this is what a proxy hop costs on this host, and is not AgentIAM's doing;
3. through the enforcing PEP.

**(3) - (2) is what authorization costs.** (2) - (1) is TCP and Python's HTTP stack.

| Target RPS | Runs | Achieved | ① upstream p50/p99 ms | ② +proxy p50/p99 ms | enforcement p50, median of runs | enforcement p99, min - max |
|---|---|---|---|---|---|---|
| **100** | 3 | 99.0 | 4.613 / 47.849 | 6.769 / 19.439 | 2.191 | **1.753 - 74.724** |
| **500** ⚠️ | 3 | 117.9 | 335.032 / 1906.541 | 374.181 / 2240.46 | 13.33 | **128.011 - 388.83** |

The p99 column is a **range across runs**, not a single figure, because on this host it is not stable enough to quote as one. `PLAN.md` §13.1 asks for at least three runs with the variance reported; that requirement is the reason this is visible rather than a matter of which run got written down.

At **100 RPS the enforcement p99 ranged 1.753-74.724 ms across 3 runs**, which straddles NFR-2's 8.0 ms budget. The best run is comfortably inside it and the worst is nearly ten times it, so **the honest statement is that this host cannot establish the p99 either way** — the median run's p50 is the only figure here stable enough to quote. Something outside the request path is contributing tens of milliseconds intermittently: the generator, the three uvicorn processes and Postgres all share one machine, and the `generator_lag_ms` series in the JSON shows the harness itself stalling. Establishing NFR-2 properly needs the generator off-box.

⚠️ **The 500 RPS profile(s) could not be offered on this host and the latencies in those rows are queueing artefacts, not service times.** At 500 RPS the *stub upstream alone* — with no PEP in the path at all — achieved only 138.4 RPS at a p50 of 335.032 ms. The limit is the development machine running the generator and three uvicorn processes against each other on loopback, not anything in AgentIAM. Reported rather than dropped, because `PLAN.md` T-053 asks for the numbers actually obtained.

**So NFR-2 is not yet demonstrated at its stated rate.** What has been measured is the enforcement cost at rates this host can serve, and the honest reading is in the row above the warning. Establishing the 500 RPS figure needs a machine that can generate it, or the generator moved off-box.


---

## How to reproduce

```
uv run pytest -m perf --benchmark-only          # NFR-1 and PB-2
uv run python scripts/run_load_test.py          # NFR-2, brings up its own Postgres
uv run python scripts/generate_benchmark_results.py
```

The load test starts its own database on an ephemeral port rather than using `make up`'s. On the development host a native Windows PostgreSQL shares port 5432 with the compose one and wins every connection, so a run against `localhost:5432` silently addresses the wrong database.
