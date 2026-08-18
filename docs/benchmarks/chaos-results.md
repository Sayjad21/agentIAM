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
| CH-1 | Kill Postgres for 30 s | PEPs spend leases, then fail closed; recovery is clean; invariant holds | 3 held / 0 violated / 11 unavailable | held |
| CH-2 | Kill Redis for 30 s | revocation falls back to pull; leases unaffected | — | not run — deferred (`PLAN.md` §21) |
| CH-3 | SIGKILL one PEP of three | its lease strands <= TTL then reclaims; others unaffected | 11 held / 0 violated | held |
| CH-4 | Partition PEP<->ledger | bounded spend, then fail closed | 3 held / 0 violated | held |
| CH-5 | 500 ms latency on the ledger | top-ups slow, decisions unaffected | — | not run — deferred (`PLAN.md` §21) |
| CH-6 | Packet loss 10% | retries with backoff; no double-spend | — | not run — deferred (`PLAN.md` §21) |
| CH-7 | Clock skew +60 s on one PEP | tolerance honoured; no spurious denials or expiries | — | not run — deferred (`PLAN.md` §21) |
| CH-8 | Ollama down | template fallback; no hot-path impact | 1 held / 0 violated | held |
| CH-9 | Embedding service down | strict scopes escalate, log-only allows | — | not run — deferred (`PLAN.md` §21) |
| CH-10 | Rolling restart under load | zero dropped requests; invariant holds | 16 held / 0 violated | held |
| CH-11 | Postgres connection-pool exhaustion | graceful 503; fail closed | — | not run — deferred (`PLAN.md` §21) |
| CH-12 | Disk full on the audit ledger | requests denied; alert raised | — | not run — deferred (`PLAN.md` §21) |

Sub-scenarios — additional runs that probe one aspect of a scenario above:

| Run | Title | Invariant sweeps | Verdict |
|---|---|---|---|
| CH-01-audit | Kill Postgres for 30 s — the audit path | 4 held / 0 violated / 9 unavailable | held |
| CH-04-shutdown | Partition PEP <-> ledger — graceful shutdown | 21 held / 0 violated | held |
| CH-08-blackhole | Ollama black-holed (accepts, never answers) | 1 held / 0 violated | held |
| CH-08-compiler | Ollama down — the NL compiler | 17 held / 0 violated | held |
| CH-10-settlement | Rolling restart under load — where the spent budget goes | 11 held / 0 violated | held |

---

## CH-01 — Kill Postgres for 30 s

**Expected:** PEPs spend leases, then fail closed; recovery is clean; invariant holds

Run `757cecd5-ff81-4e1b-999c-cc254b8587cd` at `2026-08-18T15:47:16.309315+00:00`, 31.922 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **14 sweeps** — 3 held, 0 violated, 11 unavailable.

Sweeps that could not run:

- `InterfaceError: (sqlalchemy.dialects.postgresql.asyncpg.InterfaceError) <class 'asyncpg.exceptions._base.InterfaceError'>: connection is closed
[SQL: 
    SELECT
        b.id  `
- `ConnectionError: unexpected connection_lost() call`
- `ConnectionRefusedError: [WinError 1225] The remote computer refused the network connection`

| Measurement | Value |
|---|---|
| `leased_before_outage` | `300.0000` |
| `local_remaining_at_cut` | `200.0000` |
| `committed_before_outage` | `0.0000` |
| `outage_s` | `30.39` |
| `spent_during_outage` | `200.0000` |
| `served_during_outage` | `8` |
| `refused_during_outage` | `6` |
| `recovery_s` | `0.58` |
| `leased_after_recovery` | `300.0000` |
| `acquire_failures` | `1` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| healthy | 4 | 4 | 0 | `200`: 4 | — | 7.294 | 14.372 |
| during outage | 14 | 8 | 0 | `200`: 8, `429`: 6 | `LEASE_UNAVAILABLE`: 6 | 4.536 | 8.859 |
| after recovery | 4 | 4 | 0 | `200`: 4 | — | 2.899 | 3.313 |

Timeline:

- `  0.000 s`  scenario started
- `  0.068 s`  stopping postgres
- `  0.562 s`  postgres stopped
- ` 30.955 s`  starting postgres
- ` 31.791 s`  postgres accepting connections after 0.6 s
- ` 31.918 s`  scenario ended
- ` 31.918 s`  final sweep

**Notes:**

- The clock is frozen, so no lease expired during the outage. CH-1 is about an unreachable ledger; expiry is CH-3's subject.


---

## CH-01-audit — Kill Postgres for 30 s — the audit path

**Expected:** every record buffered during the outage reaches the chain afterwards

Run `52c145b5-b9cf-4a89-abf8-322f864cf72f` at `2026-08-18T15:47:48.908915+00:00`, 31.607 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **13 sweeps** — 4 held, 0 violated, 9 unavailable.

Sweeps that could not run:

- `InterfaceError: (sqlalchemy.dialects.postgresql.asyncpg.InterfaceError) <class 'asyncpg.exceptions._base.InterfaceError'>: connection is closed
[SQL: 
    SELECT
        b.id  `
- `ConnectionError: unexpected connection_lost() call`
- `ConnectionRefusedError: [WinError 1225] The remote computer refused the network connection`

| Measurement | Value |
|---|---|
| `records_before_outage` | `1` |
| `allowed_while_unrecordable` | `141` |
| `records_landed` | `141` |
| `emitter_failed_batches` | `7` |
| `emitter_lost_records` | `0` |
| `emitter_dropped` | `0` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.065 s`  stopping postgres
- ` 30.710 s`  starting postgres
- ` 31.603 s`  scenario ended
- ` 31.603 s`  final sweep

**Notes:**

- Every one of the 141 records written while Postgres was stopped reached the chain after it came back (141 landed, 0 lost, 7 failed write attempts along the way). Before the fix this scenario measured the opposite: the emitter bounded every failure by max_retries and discarded the batch, so an outage was indistinguishable from a poison batch and the PEP kept authorizing requests it could no longer record. STATUS.md gap 20, now closed.


---

## CH-03 — SIGKILL one PEP of three

**Expected:** its lease strands <= TTL then reclaims; others unaffected

Run `032febdd-bf50-4149-aab3-2867b78b5fab` at `2026-08-18T15:48:24.341122+00:00`, 2.324 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **11 sweeps** — 11 held, 0 violated, 0 unavailable.

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
- `  1.005 s`  three leases held
- `  1.020 s`  killing pep-doomed
- `  1.022 s`  pep-doomed exited 1
- `  1.810 s`  reaped one second inside the skew margin
- `  1.834 s`  reaped past the skew margin
- `  2.320 s`  scenario ended
- `  2.320 s`  final sweep

**Notes:**

- Reclamation is observed by advancing an injected `now` into `reap()`, not by sleeping for the TTL. The bound under test is the ledger's `expires_at + S`, which is a value, not a wall-clock wait.


---

## CH-04 — Partition PEP <-> ledger

**Expected:** bounded spend, then fail closed

Run `6e63cb6b-3f84-4d3f-b094-79274b036eca` at `2026-08-18T15:48:31.804163+00:00`, 0.22 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **3 sweeps** — 3 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `leased_at_cut` | `200.0000` |
| `committed_at_cut` | `0.0000` |
| `local_remaining_at_cut` | `120.0000` |
| `served_during_partition` | `6` |
| `spent_during_partition` | `120.0000` |
| `hot_path_p99_ms_partitioned` | `6.861` |
| `hot_path_p99_ms_healthy` | `7.74` |
| `committed_during_partition` | `0.0000` |
| `invariant_held_during_partition` | yes |
| `blackholed_connections` | `1` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| healthy | 4 | 4 | 0 | `200`: 4 | — | 6.333 | 7.74 |
| during partition | 11 | 6 | 0 | `200`: 6, `429`: 5 | `LEASE_UNAVAILABLE`: 5 | 2.9 | 6.861 |
| after heal | 4 | 4 | 0 | `200`: 4 | — | 3.786 | 4.288 |

Timeline:

- `  0.000 s`  scenario started
- `  0.078 s`  cutting PEP -> ledger (blackhole)
- `  0.123 s`  healing the partition
- `  0.216 s`  scenario ended
- `  0.216 s`  final sweep


---

## CH-04-shutdown — Partition PEP <-> ledger — graceful shutdown

**Expected:** a partitioned PEP cannot complete `LeasePool.aclose()`

Run `ef3f7c6b-a552-41d9-8485-cb952bf1b4ae` at `2026-08-18T15:48:32.726183+00:00`, 5.197 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **21 sweeps** — 21 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `graceful_close_completed_within_5s` | no |
| `graceful_close_completed_after_heal` | yes |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.156 s`  cutting, then asking the pool to close
- `  5.166 s`  healing; the pending shutdown should now complete
- `  5.195 s`  scenario ended
- `  5.195 s`  final sweep

**Notes:**

- A partitioned PEP cannot complete a graceful shutdown: `aclose()` drains in-flight top-ups and then RELEASEs, and both need the ledger. Worse, the timeout meant to bound it does not: `asyncio.wait_for` cancels into SQLAlchemy's greenlet bridge while asyncpg is blocked on the dead socket, and the driver's own rollback/close needs that same socket — measured stuck for 5 minutes against a 5 s bound. Healing the partition is what releases it. The stranded lease is bounded by TTL + S (CH-3), so this costs availability on restart, not correctness. See STATUS.md gap 21.


---

## CH-08 — Ollama down

**Expected:** template fallback; no hot-path impact

Run `029abf34-0eac-40b9-9ea2-56feb319dfeb` at `2026-08-18T15:48:42.416401+00:00`, 24.185 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **1 sweeps** — 1 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `served_with_ollama_down` | `12` |
| `hot_path_p99_ms` | `6052.738` |
| `hot_path_p50_ms` | `6032.885` |
| `embedding_timeout_s` | `2.0` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| ollama refusing connections | 12 | 12 | 0 | `200`: 12 | — | 6032.885 | 6052.738 |

Timeline:

- `  0.000 s`  scenario started
- ` 24.156 s`  scenario ended
- ` 24.156 s`  final sweep

**Notes:**

- The template fallback PLAN.md §13.2 expects for CH-8 does not exist: T-031 is deferred (PLAN.md §21). CH-8 is therefore PARTIAL on both halves — the fallback is unimplemented, and 'no hot-path impact' is false.
- An unreachable Ollama costs the hot path p50 6032.885 ms / p99 6052.738 ms against a 2.0 s embedding timeout — roughly three timeouts, because scoring one request embeds the task, the action template and the rendered action, and `EmbeddingClient` is a *synchronous* httpx.Client called from the event loop (ADR-037 noted the shape; this puts a number on it). Outcomes stay correct: all 12 requests were allowed, because spec 09 §5 fails drift open. See STATUS.md gap 22.
- Measured on the development host, and it invalidated this scenario's first premise: a connect to a *closed* 127.0.0.1 port does not refuse, it raises ConnectTimeout after ~3 s — port 9 included. There is no cheap-failure mode for an unreachable local service on Windows.


---

## CH-08-blackhole — Ollama black-holed (accepts, never answers)

**Expected:** requests still allowed; the latency cost is measured, not assumed

Run `d96bcf95-f2a0-4215-9af2-9278562c107a` at `2026-08-18T15:49:08.038610+00:00`, 6.174 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **1 sweeps** — 1 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `embedding_timeout_s` | `2.0` |
| `baseline_p99_ms` | `13.436` |
| `blackholed_p99_ms` | `6062.045` |
| `three_concurrent_requests_wall_s` | `6.06` |
| `blackholed_connections` | `2` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| no intent headers (oracle not consulted) | 6 | 6 | 0 | `200`: 6 | — | 12.985 | 13.436 |
| ollama black-holed | 3 | 3 | 0 | `200`: 3 | — | 4042.389 | 6062.045 |

Timeline:

- `  0.000 s`  scenario started
- `  6.091 s`  scenario ended
- `  6.092 s`  final sweep

**Notes:**

- A black-holed Ollama costs every uncached drift scoring call its full 2.0 s timeout, on the event loop, because `EmbeddingClient` holds a synchronous httpx.Client (ADR-037 already noted the shape). Three concurrent requests took 6.1 s wall against a baseline p99 of 13.436 ms. Outcomes stay correct — spec 09 §5 fails open — but 'no hot-path impact' is only true for a *refused* Ollama, not a hung one. See STATUS.md gap 22.


---

## CH-08-compiler — Ollama down — the NL compiler

**Expected:** typed error inside the timeout; template fallback is unimplemented

Run `8baf6cd0-ef19-4bac-8cf0-01a773439d31` at `2026-08-18T15:49:14.703411+00:00`, 5.328 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **17 sweeps** — 17 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `warm_returned` | no |
| `warm_s` | `2.576` |
| `generate_failed_after_s` | `2.749` |
| `generate_error` | `OllamaError` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  5.325 s`  scenario ended
- `  5.325 s`  final sweep

**Notes:**

- No template fallback exists to exercise: T-031 is deferred (PLAN.md §21), so demo beat 5 has no F-2 recovery while that stays true. This is the resumption trigger STATUS.md records for T-031.


---

## CH-10 — Rolling restart under load

**Expected:** zero dropped requests; invariant holds

Run `8f326514-a911-4793-8033-e77f688d8cdb` at `2026-08-18T15:51:39.810593+00:00`, 3.857 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **16 sweeps** — 16 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `leased_with_three_instances` | `15000.0000` |
| `restarts` | `3` |
| `requests_sent` | `301` |
| `requests_dropped` | `0` |
| `sweeps_during_restarts` | `12` |
| `settlement_backlog_when_load_stopped` | `7` |
| `settlement_counters` | `applied`: 153, `declined`: 0, `dropped`: 0, `failed_attempts`: 0, `still_pending`: 0, `shutdown_timeouts`: 0 |
| `leased_after_restarts` | `14235.0000` |
| `committed_after_restarts` | `1505.0000` |
| `spent_locally` | `1505.0000` |
| `pool_available_after_restarts` | `184260.0000` |
| `lease_rows_by_state` | `active`: 3, `released`: 3 |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| continuous load across the restart | 301 | 301 | 0 | `200`: 301 | — | 6.826 | 34.05 |

Timeline:

- `  0.000 s`  scenario started
- `  0.509 s`  draining pep-roll-0
- `  0.509 s`  stopping pep-roll-0 (RELEASE)
- `  1.360 s`  starting pep-roll-0 (ACQUIRE)
- `  1.450 s`  pep-roll-0 back in rotation
- `  1.450 s`  draining pep-roll-1
- `  1.452 s`  stopping pep-roll-1 (RELEASE)
- `  2.233 s`  starting pep-roll-1 (ACQUIRE)
- `  2.316 s`  pep-roll-1 back in rotation
- `  2.316 s`  draining pep-roll-2
- `  2.316 s`  stopping pep-roll-2 (RELEASE)
- `  3.182 s`  starting pep-roll-2 (ACQUIRE)
- `  3.297 s`  pep-roll-2 back in rotation
- `  3.845 s`  settlement queues drained
- `  3.854 s`  scenario ended
- `  3.854 s`  final sweep

**Notes:**

- An instance is the whole PEP object graph rebuilt, with a real RELEASE and a real ACQUIRE against Postgres, but not a new OS process. CH-3 is the real-process scenario. The risk a rolling restart carries is in the ledger, which is fully exercised here.
- Two throughput observations from tuning this scenario, both unrelated to restarts and both the PEP correctly failing closed. (1) With the lease at 500 and four workers, the low-water mark leaves 25 payments of headroom and the load spent it before the top-up's ACQUIRE landed — 14 LEASE_UNAVAILABLE in 10,434 requests; that is the case T-015 (adaptive lease sizing, deferred) exists for. (2) Settlement cost one LEDGER_COMMIT per reservation, each taking FOR UPDATE on the same pool row, so three instances serialized; since `LeasePool._release` drains the queue before retiring a lease, a backlog larger than a lease's spending stalled the top-up behind it — 7,053 refusals in 21,006 requests at concurrency=4, pace=5 ms.
- (2) is fixed. T-053 batches settlements sharing a lease into one transaction (`ledger_commit_batch`), so N lock acquisitions became one. Re-measured at the same concurrency=4, pace=5 ms that produced 7,053 refusals in 21,006 requests (33.6%): **22 in 1,083 (2.0%)**, a 17x reduction in refusal rate. At this scenario's committed paced load the settlement backlog when traffic stopped fell from 442 to 10. What remains is (1), which is lease sizing and belongs to T-015.
- 301 requests spent 1505.0000, and the ledger agrees: committed=1505.0000 across 3 restarts. Before T-052 this read committed=0 — no production caller reached LEDGER_COMMIT, so every RELEASE returned spent budget to the pool. Closed by `agentiam_pep.settlement`.


---

## CH-10-settlement — Rolling restart under load — where the spent budget goes

**Expected:** a RELEASE returns only the unspent remainder of the lease

Run `ba1e72c9-47fc-45a8-b56e-1dd9a4b66d04` at `2026-08-18T15:51:44.356508+00:00`, 2.489 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **11 sweeps** — 11 held, 0 violated, 0 unavailable.

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
- `  2.451 s`  stopping pep-roll-0 gracefully (settle, then RELEASE)
- `  2.486 s`  scenario ended
- `  2.486 s`  final sweep

**Notes:**

- Instance 0 spent 3000.0000 of a 5000.0000 lease and then released it. The pool got back 2000.0000 — the unspent remainder only — and `committed` moved to 3000.0000. Before T-052 the pool got the whole 5000.0000 back and `committed` stayed at 0, so the 3000.0000 was spendable twice.

