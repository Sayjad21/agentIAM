<!--
  GENERATED FILE — do not edit.

  Regenerate with:  uv run python scripts/generate_chaos_results.py
  Source of truth:  docs/benchmarks/chaos/*.json, written by tests/chaos/ (T-052)
  Verify in CI:     uv run python scripts/generate_chaos_results.py --check
-->

# Chaos results

`PLAN.md` §13.2 defines twelve scenarios. **T-052 is scoped to five** — CH-1, CH-3, CH-4,
CH-8 and CH-10 (`ROADMAP.md` line 287); the other seven are deferred in `PLAN.md` §21 item
10, with "nightly CI after submission" as their resumption trigger. They are listed below as
*not run* rather than omitted, because a table showing five rows and no mention of the rest
reads as twelve passes to anyone skimming it.

The **invariant checker (T-016) runs as a sidecar throughout every scenario**, sweeping the
ledger on a timer. It records three outcomes per sweep, not two: *held*, *violated*, and
*unavailable* — the last being a sweep that could not run at all, which is the honest state
during CH-1, when Postgres is genuinely stopped. Folding "unavailable" into "held" would
report a green run for a database that was not there.

Every scenario runs against real Postgres in a container, a real biscuit chain, the real
Cedar engine, the real lease ledger and the real audit chain. Where a mechanism stands in
for the one `PLAN.md` names, or an expectation is not met, it says so in that scenario's
notes rather than in a footnote.


## Summary

| ID | Scenario | Expected | Invariant sweeps | Verdict |
|---|---|---|---|---|
| CH-1 | Kill Postgres for 30 s | PEPs spend leases, then fail closed; recovery is clean; invariant holds | 2 held / 0 violated / 122 unavailable | held |
| CH-2 | Kill Redis for 30 s | revocation falls back to pull; leases unaffected | — | not run — deferred (`PLAN.md` §21) |
| CH-3 | SIGKILL one PEP of three | its lease strands <= TTL then reclaims; others unaffected | 9 held / 0 violated | held |
| CH-4 | Partition PEP<->ledger | bounded spend, then fail closed | 3 held / 0 violated | held |
| CH-5 | 500 ms latency on the ledger | top-ups slow, decisions unaffected | — | not run — deferred (`PLAN.md` §21) |
| CH-6 | Packet loss 10% | retries with backoff; no double-spend | — | not run — deferred (`PLAN.md` §21) |
| CH-7 | Clock skew +60 s on one PEP | tolerance honoured; no spurious denials or expiries | — | not run — deferred (`PLAN.md` §21) |
| CH-8 | Ollama down | template fallback; no hot-path impact | 2 held / 0 violated | held |
| CH-9 | Embedding service down | strict scopes escalate, log-only allows | — | not run — deferred (`PLAN.md` §21) |
| CH-10 | Rolling restart under load | zero dropped requests; invariant holds | 15 held / 0 violated | held |
| CH-11 | Postgres connection-pool exhaustion | graceful 503; fail closed | — | not run — deferred (`PLAN.md` §21) |
| CH-12 | Disk full on the audit ledger | requests denied; alert raised | — | not run — deferred (`PLAN.md` §21) |

Sub-scenarios — additional runs that probe one aspect of a scenario above:

| Run | Title | Invariant sweeps | Verdict |
|---|---|---|---|
| CH-01-audit | Kill Postgres for 30 s — the audit path | 3 held / 0 violated / 122 unavailable | held |
| CH-04-shutdown | Partition PEP <-> ledger — graceful shutdown | 22 held / 0 violated | held |
| CH-08-blackhole | Ollama black-holed (accepts, never answers) | 3 held / 0 violated | held |
| CH-08-compiler | Ollama down — the NL compiler | 1 held / 0 violated | held |
| CH-10-settlement | Rolling restart under load — where the spent budget goes | 9 held / 0 violated | held |

---

## CH-01 — Kill Postgres for 30 s

**Expected:** PEPs spend leases, then fail closed; recovery is clean; invariant holds

Run `a3e8b4d5-d8fc-46ef-9ab0-8a9cba40c29c` at `2026-10-17T16:50:28.591033+00:00`, 31.599 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **124 sweeps** — 2 held, 0 violated, 122 unavailable.

Sweeps that could not run:

- `InterfaceError: (sqlalchemy.dialects.postgresql.asyncpg.InterfaceError) <class 'asyncpg.exceptions._base.InterfaceError'>: connection is closed
[SQL: 
    SELECT
        b.id  `
- `OSError: Multiple exceptions: [Errno 111] Connect call failed ('::1', 35605, 0, 0), [Errno 111] Connect call failed ('127.0.0.1', 35605)`

| Measurement | Value |
|---|---|
| `leased_before_outage` | `300.0000` |
| `local_remaining_at_cut` | `200.0000` |
| `committed_before_outage` | `0.0000` |
| `outage_s` | `30.33` |
| `spent_during_outage` | `200.0000` |
| `served_during_outage` | `8` |
| `refused_during_outage` | `6` |
| `recovery_s` | `0.28` |
| `leased_after_recovery` | `300.0000` |
| `acquire_failures` | `1` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| healthy | 4 | 4 | 0 | `200`: 4 | — | 5.23 | 9.71 |
| during outage | 14 | 8 | 0 | `200`: 8, `429`: 6 | `LEASE_UNAVAILABLE`: 6 | 2.841 | 4.495 |
| after recovery | 4 | 4 | 0 | `200`: 4 | — | 1.727 | 4.445 |

Timeline:

- `  0.000 s`  scenario started
- `  0.049 s`  stopping postgres
- `  0.662 s`  postgres stopped
- ` 30.988 s`  starting postgres
- ` 31.504 s`  postgres accepting connections after 0.3 s
- ` 31.595 s`  scenario ended
- ` 31.595 s`  final sweep

**Notes:**

- The clock is frozen, so no lease expired during the outage. CH-1 is about an unreachable ledger; expiry is CH-3's subject.


---

## CH-01-audit — Kill Postgres for 30 s — the audit path

**Expected:** every record buffered during the outage reaches the chain afterwards

Run `54aabc6f-876c-490e-90d6-c3a637980a51` at `2026-10-17T16:51:00.767863+00:00`, 31.386 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **125 sweeps** — 3 held, 0 violated, 122 unavailable.

Sweeps that could not run:

- `InterfaceError: (sqlalchemy.dialects.postgresql.asyncpg.InterfaceError) <class 'asyncpg.exceptions._base.InterfaceError'>: connection is closed
[SQL: 
    SELECT
        b.id  `
- `OSError: Multiple exceptions: [Errno 111] Connect call failed ('::1', 35605, 0, 0), [Errno 111] Connect call failed ('127.0.0.1', 35605)`
- `ConnectionResetError: [Errno 104] Connection reset by peer`

| Measurement | Value |
|---|---|
| `records_before_outage` | `1` |
| `allowed_while_unrecordable` | `147` |
| `records_landed` | `147` |
| `emitter_failed_batches` | `60` |
| `emitter_lost_records` | `0` |
| `emitter_dropped` | `0` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.042 s`  stopping postgres
- ` 30.737 s`  starting postgres
- ` 31.384 s`  scenario ended
- ` 31.384 s`  final sweep

**Notes:**

- Every one of the 147 records written while Postgres was stopped reached the chain after it came back (147 landed, 0 lost, 60 failed write attempts along the way). Before the fix this scenario measured the opposite: the emitter bounded every failure by max_retries and discarded the batch, so an outage was indistinguishable from a poison batch and the PEP kept authorizing requests it could no longer record. STATUS.md gap 20, now closed.


---

## CH-03 — SIGKILL one PEP of three

**Expected:** its lease strands <= TTL then reclaims; others unaffected

Run `a35c473b-90e9-4df2-9294-d00da6e1b811` at `2026-10-17T16:51:39.739360+00:00`, 1.76 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **9 sweeps** — 9 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `lease_size` | `200.0000` |
| `leased_with_three_holders` | `600.0000` |
| `survivor_heartbeats_after_kill` | `8` |
| `leased_while_stranded` | `600.0000` |
| `invariant_held_while_stranded` | yes |
| `leased_after_reap` | `400.0000` |
| `documented_worst_case_strand_s` | `80.0` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.495 s`  three leases held
- `  0.502 s`  killing pep-doomed
- `  0.509 s`  pep-doomed exited -9
- `  1.259 s`  reaped one second inside the skew margin
- `  1.291 s`  reaped past the skew margin
- `  1.758 s`  scenario ended
- `  1.758 s`  final sweep

**Notes:**

- Reclamation is observed by advancing an injected `now` into `reap()`, not by sleeping for the TTL. The bound under test is the ledger's `expires_at + S`, which is a value, not a wall-clock wait.


---

## CH-04 — Partition PEP <-> ledger

**Expected:** bounded spend, then fail closed

Run `8ffeb394-8319-4992-94c8-2bd59f3cb176` at `2026-10-17T16:51:49.218606+00:00`, 0.146 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **3 sweeps** — 3 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `leased_at_cut` | `200.0000` |
| `committed_at_cut` | `0.0000` |
| `local_remaining_at_cut` | `120.0000` |
| `served_during_partition` | `6` |
| `spent_during_partition` | `120.0000` |
| `hot_path_p99_ms_partitioned` | `3.761` |
| `hot_path_p99_ms_healthy` | `6.07` |
| `committed_during_partition` | `0.0000` |
| `invariant_held_during_partition` | yes |
| `blackholed_connections` | `2` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| healthy | 4 | 4 | 0 | `200`: 4 | — | 4.579 | 6.07 |
| during partition | 11 | 6 | 0 | `200`: 6, `429`: 5 | `LEASE_UNAVAILABLE`: 5 | 1.986 | 3.761 |
| after heal | 4 | 4 | 0 | `200`: 4 | — | 2.112 | 2.741 |

Timeline:

- `  0.000 s`  scenario started
- `  0.041 s`  cutting PEP -> ledger (blackhole)
- `  0.071 s`  healing the partition
- `  0.144 s`  scenario ended
- `  0.144 s`  final sweep


---

## CH-04-shutdown — Partition PEP <-> ledger — graceful shutdown

**Expected:** a partitioned PEP cannot complete `LeasePool.aclose()`

Run `ac550262-473f-4242-b943-f70ae40d44e2` at `2026-10-17T16:51:49.844655+00:00`, 5.158 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **22 sweeps** — 22 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `graceful_close_completed_within_5s` | no |
| `graceful_close_completed_after_heal` | yes |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.107 s`  cutting, then asking the pool to close
- `  5.111 s`  healing; the pending shutdown should now complete
- `  5.156 s`  scenario ended
- `  5.156 s`  final sweep

**Notes:**

- A partitioned PEP cannot complete a graceful shutdown: `aclose()` drains in-flight top-ups and then RELEASEs, and both need the ledger. Worse, the timeout meant to bound it does not: `asyncio.wait_for` cancels into SQLAlchemy's greenlet bridge while asyncpg is blocked on the dead socket, and the driver's own rollback/close needs that same socket — measured stuck for 5 minutes against a 5 s bound. Healing the partition is what releases it. The stranded lease is bounded by TTL + S (CH-3), so this costs availability on restart, not correctness. See STATUS.md gap 21.


---

## CH-08 — Ollama down

**Expected:** template fallback; no hot-path impact

Run `03c3194b-7729-4526-a6fa-c3f59722b881` at `2026-10-17T16:52:02.752805+00:00`, 0.058 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **2 sweeps** — 2 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `served_with_ollama_down` | `12` |
| `hot_path_p99_ms` | `14.202` |
| `hot_path_p50_ms` | `13.303` |
| `embedding_timeout_s` | `2.0` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| ollama refusing connections | 12 | 12 | 0 | `200`: 12 | — | 13.303 | 14.202 |

Timeline:

- `  0.000 s`  scenario started
- `  0.055 s`  scenario ended
- `  0.055 s`  final sweep

**Notes:**

- Both halves of the expectation hold: the template fallback (T-031) answers the compiler when the model is down (CH-08-compiler), and the hot path is unaffected.
- An unreachable Ollama costs the hot path p50 13.303 ms / p99 14.202 ms: each request waits at most 120 ms for the model and then goes on with drift unassessed (spec 09 §5 fails it open). Before APICTA-07 the same scenario measured p99 6,066 ms, because the embedding client was synchronous and called from the event loop (STATUS gap 22, closed). All 12 requests were allowed.
- Measured on the Windows development host, and it invalidated this scenario's first premise: a connect to a *closed* 127.0.0.1 port does not refuse, it raises ConnectTimeout after ~3 s — port 9 included. There is no cheap-failure mode for an unreachable local service on Windows.


---

## CH-08-blackhole — Ollama black-holed (accepts, never answers)

**Expected:** requests still allowed; each waits at most the assess bound

Run `e4ba34c0-834a-49cb-84d5-645742ea0536` at `2026-10-17T16:52:03.371721+00:00`, 0.406 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **3 sweeps** — 3 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `embedding_timeout_s` | `2.0` |
| `assess_timeout_ms` | `120.0` |
| `baseline_p99_ms` | `8.211` |
| `blackholed_p99_ms` | `131.669` |
| `nine_requests_three_at_a_time_wall_s` | `0.39` |
| `blackholed_connections` | `10` |
| `assessments_timed_out` | `9` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| no intent headers (oracle not consulted) | 6 | 6 | 0 | `200`: 6 | — | 7.405 | 8.211 |
| ollama black-holed | 9 | 9 | 0 | `200`: 9 | — | 125.051 | 131.669 |

Timeline:

- `  0.000 s`  scenario started
- `  0.404 s`  scenario ended
- `  0.404 s`  final sweep

**Notes:**

- A black-holed Ollama now costs a request at most the 120 ms assess bound: p99 131.669 ms against a baseline p99 of 8.211 ms, nine requests three at a time in 0.4 s. Before APICTA-07 each uncached request paid the full 2.0 s embedding timeout on the event loop, and three concurrent requests took over 6 s. Outcomes are unchanged — spec 09 §5 fails drift open.


---

## CH-08-compiler — Ollama down — the NL compiler

**Expected:** typed error inside the timeout; the template compiler answers

Run `c50bc423-46e7-43b7-b2c2-a02791205d6c` at `2026-10-17T16:52:04.194356+00:00`, 0.128 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **1 sweeps** — 1 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `warm_returned` | no |
| `warm_s` | `0.014` |
| `generate_failed_after_s` | `0.007` |
| `generate_error` | `OllamaError` |
| `compiled_by` | `template` |
| `fallback_compile_s` | `0.05` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.070 s`  scenario ended
- `  0.071 s`  final sweep

**Notes:**

- With Ollama unreachable the compiler fails over to the offline template compiler (APICTA-01 Part D, T-031): a quarantine sentence compiled in 0.05 s, appended to the current policy. DEMO.md drill F-2 now has a recovery for the demo's policy shapes; other sentences get the list of shapes the offline compiler understands.


---

## CH-10 — Rolling restart under load

**Expected:** zero dropped requests; invariant holds

Run `7f1d2253-cf94-4b08-b720-691f120a584b` at `2026-10-17T16:52:12.113401+00:00`, 3.576 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **15 sweeps** — 15 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `leased_with_three_instances` | `15000.0000` |
| `restarts` | `3` |
| `requests_sent` | `182` |
| `requests_dropped` | `0` |
| `sweeps_during_restarts` | `12` |
| `settlement_backlog_when_load_stopped` | `1` |
| `settlement_counters` | `applied`: 94, `declined`: 0, `dropped`: 0, `failed_attempts`: 0, `still_pending`: 0, `shutdown_timeouts`: 0 |
| `leased_after_restarts` | `14530.0000` |
| `committed_after_restarts` | `910.0000` |
| `spent_locally` | `910.0000` |
| `pool_available_after_restarts` | `184560.0000` |
| `lease_rows_by_state` | `active`: 3, `released`: 3 |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| continuous load across the restart | 182 | 182 | 0 | `200`: 182 | — | 5.15 | 92.405 |

Timeline:

- `  0.000 s`  scenario started
- `  0.508 s`  draining pep-roll-0
- `  0.519 s`  stopping pep-roll-0 (RELEASE)
- `  1.300 s`  starting pep-roll-0 (ACQUIRE)
- `  1.366 s`  pep-roll-0 back in rotation
- `  1.366 s`  draining pep-roll-1
- `  1.366 s`  stopping pep-roll-1 (RELEASE)
- `  2.131 s`  starting pep-roll-1 (ACQUIRE)
- `  2.194 s`  pep-roll-1 back in rotation
- `  2.194 s`  draining pep-roll-2
- `  2.194 s`  stopping pep-roll-2 (RELEASE)
- `  2.971 s`  starting pep-roll-2 (ACQUIRE)
- `  3.035 s`  pep-roll-2 back in rotation
- `  3.567 s`  settlement queues drained
- `  3.575 s`  scenario ended
- `  3.575 s`  final sweep

**Notes:**

- An instance is the whole PEP object graph rebuilt, with a real RELEASE and a real ACQUIRE against Postgres, but not a new OS process. CH-3 is the real-process scenario. The risk a rolling restart carries is in the ledger, which is fully exercised here.
- Two throughput observations from tuning this scenario, both unrelated to restarts and both the PEP correctly failing closed. (1) With the lease at 500 and four workers, the low-water mark leaves 25 payments of headroom and the load spent it before the top-up's ACQUIRE landed — 14 LEASE_UNAVAILABLE in 10,434 requests; that is the case T-015 (adaptive lease sizing, deferred) exists for. (2) Settlement cost one LEDGER_COMMIT per reservation, each taking FOR UPDATE on the same pool row, so three instances serialized; since `LeasePool._release` drains the queue before retiring a lease, a backlog larger than a lease's spending stalled the top-up behind it — 7,053 refusals in 21,006 requests at concurrency=4, pace=5 ms.
- (2) is fixed. T-053 batches settlements sharing a lease into one transaction (`ledger_commit_batch`), so N lock acquisitions became one. Re-measured at the same concurrency=4, pace=5 ms that produced 7,053 refusals in 21,006 requests (33.6%): **22 in 1,083 (2.0%)**, a 17x reduction in refusal rate. At this scenario's committed paced load the settlement backlog when traffic stopped fell from 442 to 10. What remains is (1), which is lease sizing and belongs to T-015.
- 182 requests spent 910.0000, and the ledger agrees: committed=910.0000 across 3 restarts. Before T-052 this read committed=0 — no production caller reached LEDGER_COMMIT, so every RELEASE returned spent budget to the pool. Closed by `agentiam_pep.settlement`.


---

## CH-10-settlement — Rolling restart under load — where the spent budget goes

**Expected:** a RELEASE returns only the unspent remainder of the lease

Run `7a338fbd-e7fc-476d-a329-77756ec25cce` at `2026-10-17T16:52:16.223681+00:00`, 2.03 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **9 sweeps** — 9 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `pool_available_before` | `185000.0000` |
| `spent_by_instance_0` | `3000.0000` |
| `committed_after_release` | `3000.0000` |
| `pool_available_after` | `187000.0000` |
| `returned_to_pool` | `2000.0000` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  1.993 s`  stopping pep-roll-0 gracefully (settle, then RELEASE)
- `  2.028 s`  scenario ended
- `  2.028 s`  final sweep

**Notes:**

- Instance 0 spent 3000.0000 of a 5000.0000 lease and then released it. The pool got back 2000.0000 — the unspent remainder only — and `committed` moved to 3000.0000. Before T-052 the pool got the whole 5000.0000 back and `committed` stayed at 0, so the 3000.0000 was spendable twice.

