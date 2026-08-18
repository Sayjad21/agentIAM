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

Budget: **p99 < 1 ms**. Measured 2026-08-18T14:23:44.468758+00:00.

Every step is the real implementation: a real biscuit chain, the real Cedar engine, real Datalog evaluation. The ledger and the audit sink are absent because they are off the hot path by construction — including them would measure the thing the design removed.

| Step | median µs | p99 µs |
|---|---|---|
| step 1 — extract (route, args, digest) | 19.1 | 55.5 |
| step 2 — verify (biscuit signature) | 209.4 | 482.0 |
| step 4 — caveats (4 clauses, Datalog) | 3.7 | 8.8 |
| step 5 — policy (Cedar) | 138.0 | 381.0 |
| step 10 — audit record hash | 16.3 | 83.0 |
| **`decide()` total — NFR-1** | 144.6 | 278.8 |

**NFR-1 holds**: `decide()` p99 is 278.8 µs against a 1000 µs budget, and `PLAN.md` §17's R-2 trigger (2 ms, port to Rust) is not approached.

Two things the breakdown says that a single number cannot:

- **Policy evaluation is nearly the whole decision** — 138.0 µs of a 144.6 µs median. Everything else inside `decide()` is single-digit microseconds.
- **`verify()` is the most expensive step and sits *outside* `decide()`** at 209.4 µs median. Per-request in-process cost is verify + decide + extract, so roughly 373 µs — that, not NFR-1 alone, is what bounds throughput.

> An earlier figure of **~5 µs** was published for NFR-1 in `STATUS.md` and the T-019 commit. It is a real measurement of `decide()` with `FakePolicy`, a stub that returns one fixed verdict, and it therefore excludes the most expensive thing in the path. It is superseded here. `tests/unit/test_decision.py::TestNfr1` still measures the stubbed path and is still useful — it isolates everything *except* policy — but the number to quote is the one above.


## NFR-2 — proxy overhead under load

Budget: **p99 < 8 ms at 500 RPS, single instance**. Measured 2026-08-18T14:51:07.250221+00:00.

Three tiers at every rate, so each subtraction compares like with like:

1. the stub upstream alone, no PEP in the path;
2. the same request through the PEP with **no pipeline attached** (T-018 transport mode) — this is what a proxy hop costs on this host, and is not AgentIAM's doing;
3. through the enforcing PEP.

**(3) - (2) is what authorization costs.** (2) - (1) is TCP and Python's HTTP stack.

| Target RPS | Achieved | ① upstream p50/p99 ms | ② +proxy p50/p99 ms | ③ +enforcement p50/p99 ms | hop ②-① ms | enforcement ③-② ms |
|---|---|---|---|---|---|---|
| **100** | 99.2 | 4.535 / 15.112 | 7.309 / 28.435 | 8.5 / 60.899 | 2.774 / 13.323 | **1.191 / 32.464** |
| **500** ⚠️ | 115.2 | 328.794 / 2006.72 | 363.921 / 2175.099 | 389.043 / 2326.181 | 35.127 / 168.379 | **25.122 / 151.082** |

⚠️ **The 500 RPS profile(s) could not be offered on this host and the latencies in those rows are queueing artefacts, not service times.** At 500 RPS the *stub upstream alone* — with no PEP in the path at all — achieved only 136.9 RPS at a p50 of 328.794 ms. The limit is the development machine running the generator and three uvicorn processes against each other on loopback, not anything in AgentIAM. Reported rather than dropped, because `PLAN.md` T-053 asks for the numbers actually obtained.

**So NFR-2 is not yet demonstrated at its stated rate.** What has been measured is the enforcement cost at rates this host can serve, and the honest reading is in the row above the warning. Establishing the 500 RPS figure needs a machine that can generate it, or the generator moved off-box.


---

## How to reproduce

```
uv run pytest -m perf --benchmark-only          # NFR-1 and PB-2
uv run python scripts/run_load_test.py          # NFR-2, brings up its own Postgres
uv run python scripts/generate_benchmark_results.py
```

The load test starts its own database on an ephemeral port rather than using `make up`'s. On the development host a native Windows PostgreSQL shares port 5432 with the compose one and wins every connection, so a run against `localhost:5432` silently addresses the wrong database.
