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

Budget: **p99 < 1 ms**. Measured 2026-10-18T10:11:22.676987+00:00.

Every step is the real implementation: a real biscuit chain, the real Cedar engine, real Datalog evaluation. The ledger and the audit sink are absent because they are off the hot path by construction — including them would measure the thing the design removed.

| Step | median µs | p99 µs |
|---|---|---|
| step 1 — extract (route, args, digest) | 17.6 | 21.3 |
| step 2 — verify (biscuit signature) | 158.4 | 175.1 |
| step 4 — caveats (4 clauses, Datalog) | 4.1 | 4.5 |
| step 4b — token authority (biscuit authorizer, depth 1) | 73.7 | 81.6 |
| step 5 — policy (Cedar) | 87.7 | 97.9 |
| step 10 — audit record hash | 15.3 | 19.0 |
| **`decide()` total — NFR-1** | 229.1 | 260.7 |

**NFR-1 holds**: `decide()` p99 is 260.7 µs against a 1000 µs budget, and `PLAN.md` §17's R-2 trigger (2 ms, port to Rust) is not approached.

Three things the breakdown says that a single number cannot:

- **Two steps are the decision.** Cedar at 87.7 µs and the token's own Datalog at 73.7 µs together account for almost all of a 229.1 µs median. Everything else inside `decide()` is single-digit microseconds.

- **Enforcing the token costs about as much as the policy does, and buys more.** Step 4b evaluates every check in every block — the mandate's grant and every attenuation caveat — against a token that may have arrived from anywhere. It was absent until the manual test pass found that nothing on the PEP path called `authorize()`, so the 73.7 µs is new cost against a budget that still has room, not a regression.

- **`verify()` is the most expensive step and sits *outside* `decide()`** at 158.4 µs median. Per-request in-process cost is verify + decide + extract, so roughly 405 µs — that, not NFR-1 alone, is what bounds throughput.

> An earlier figure of **~5 µs** was published for NFR-1 in `STATUS.md` and the T-019 commit. It is a real measurement of `decide()` with `FakePolicy`, a stub that returns one fixed verdict, and it therefore excludes the most expensive thing in the path. It is superseded here. `tests/unit/test_decision.py::TestNfr1` still measures the stubbed path and is still useful — it isolates everything *except* policy — but the number to quote is the one above.


## NFR-2 — proxy overhead under load

Budget: **p99 < 8 ms at 500 RPS, single instance**. Measured 2026-10-18T10:44:05.850660+00:00.

Host: `Linux-7.2.11-200.fc44.x86_64-x86_64-with-glibc2.43`, Python 3.12.15, 16 CPUs. Topology: single-host.

Three tiers at every rate, so each subtraction compares like with like:

1. the stub upstream alone, no PEP in the path;
2. the same request through the PEP with **no pipeline attached** (T-018 transport mode) — this is what a proxy hop costs on this host, and is not AgentIAM's doing;
3. through the enforcing PEP.

**(3) - (2) is what authorization costs.** (2) - (1) is TCP and Python's HTTP stack.

| Target RPS | Runs | Achieved | ① upstream p50/p99 ms | ② +proxy p50/p99 ms | enforcement p50, median of runs | enforcement p99, min - max |
|---|---|---|---|---|---|---|
| **100** | 3 | 99.5 | 1.873 / 3.841 | 3.629 / 7.354 | 1.177 | **7.995 - 9.364** |
| **500** ⚠️ | 3 | 194.5 | 1.192 / 32.231 | 76.454 / 893.229 | 118.107 | **331.61 - 570.231** |

The p99 column is a **range across runs**, not a single figure, because on this host it is not stable enough to quote as one. `PLAN.md` §13.1 asks for at least three runs with the variance reported; that requirement is the reason this is visible rather than a matter of which run got written down.

Each tier of the last run, with the verdict over every run:

| Target RPS | Tier | Achieved RPS | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| 100 | ① upstream alone | 99.5 | 1.873 | 3.064 | 3.841 |
| 100 | ② PEP, no pipeline | 99.5 | 3.629 | 5.77 | 7.354 |
| 100 | ③ enforcing PEP | 99.5 | 4.806 | 7.475 | 15.349 |
| 500 | ① upstream alone | 497.5 | 1.192 | 3.972 | 32.231 |
| 500 | ② PEP, no pipeline | 342.2 | 76.454 | 524.326 | 893.229 |
| 500 | ③ enforcing PEP | 194.5 | 213.356 | 937.018 | 1463.46 |

- **100 RPS**: **FAIL** — worst run's enforcement p99 9.364 ms vs 8.0 ms
- **500 RPS**: not offered — the host could not generate this rate

At **100 RPS the enforcement p99 ranged 7.995-9.364 ms across 3 runs**, which straddles NFR-2's 8.0 ms budget. The best run is just inside it and the worst is past it, so **the honest statement is that this host cannot establish the p99 either way** — the median run's p50 is the only figure here stable enough to quote. The generator kept its schedule — `generator_lag_ms` p99 at most 1.253 ms in every run — so the spread is on the server's side of the request, not the harness's: the PEP, its audit and settlement writes and Postgres still share this machine with the stub, and the two-machine run below separates the generator only.

⚠️ **The 500 RPS profile(s) could not be offered on this host and the latencies in those rows are queueing artefacts, not service times.** At 500 RPS the stub alone kept up (497.5 RPS), but the PEP, with no pipeline attached, peaked at 342.2 RPS and the enforcing PEP at 194.5. The ceiling is the PEP's own proxy hop in one process, before any authorization runs; whether it holds with the generator on another machine is what the two-machine run measures. Reported rather than dropped, because `PLAN.md` T-053 asks for the numbers actually obtained.

**So NFR-2 is not yet demonstrated at its stated rate.** What has been measured is the enforcement cost at rates this host can serve, and the honest reading is in the row above the warning. Establishing the 500 RPS figure is what the two-machine run below is for.


## NFR-2 across two machines

**Not yet run.** The single-host figures above share one machine between the load generator, three uvicorn processes and Postgres. The tooling to separate them exists (APICTA-08); the run is a human step on two hosts on one LAN:

```
# machine A — the system under test
uv run python scripts/run_load_test.py --role server
# machine B — the generator; paste the command A prints, then:
uv run python scripts/generate_benchmark_results.py
```


---

## How to reproduce

```
uv run pytest -m perf --benchmark-only          # NFR-1 and PB-2
uv run python scripts/run_load_test.py          # NFR-2, brings up its own Postgres
uv run python scripts/run_load_test.py --role server   # two machines: A, then B
uv run python scripts/generate_benchmark_results.py
```

The load test starts its own database on an ephemeral port rather than using `make up`'s. On the development host a native Windows PostgreSQL shares port 5432 with the compose one and wins every connection, so a run against `localhost:5432` silently addresses the wrong database.
