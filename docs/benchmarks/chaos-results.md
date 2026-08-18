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
| CH-1 | Kill Postgres for 30 s | PEPs spend leases, then fail closed; recovery is clean; invariant holds | 4 held / 0 violated / 12 unavailable | held |
| CH-2 | Kill Redis for 30 s | revocation falls back to pull; leases unaffected | — | not run — deferred (`PLAN.md` §21) |
| CH-3 | SIGKILL one PEP of three | its lease strands <= TTL then reclaims; others unaffected | 14 held / 0 violated | held |
| CH-4 | Partition PEP<->ledger | bounded spend, then fail closed | 4 held / 0 violated | held |
| CH-5 | 500 ms latency on the ledger | top-ups slow, decisions unaffected | — | not run — deferred (`PLAN.md` §21) |
| CH-6 | Packet loss 10% | retries with backoff; no double-spend | — | not run — deferred (`PLAN.md` §21) |
| CH-7 | Clock skew +60 s on one PEP | tolerance honoured; no spurious denials or expiries | — | not run — deferred (`PLAN.md` §21) |
| CH-8 | Ollama down | template fallback; no hot-path impact | 1 held / 0 violated | held |
| CH-9 | Embedding service down | strict scopes escalate, log-only allows | — | not run — deferred (`PLAN.md` §21) |
| CH-10 | Rolling restart under load | zero dropped requests; invariant holds | 72 held / 0 violated | held |
| CH-11 | Postgres connection-pool exhaustion | graceful 503; fail closed | — | not run — deferred (`PLAN.md` §21) |
| CH-12 | Disk full on the audit ledger | requests denied; alert raised | — | not run — deferred (`PLAN.md` §21) |

Sub-scenarios — additional runs that probe one aspect of a scenario above:

| Run | Title | Invariant sweeps | Verdict |
|---|---|---|---|
| CH-01-audit | Kill Postgres for 30 s — the audit path | 4 held / 0 violated / 10 unavailable | held |
| CH-04-shutdown | Partition PEP <-> ledger — graceful shutdown | 22 held / 0 violated | held |
| CH-08-blackhole | Ollama black-holed (accepts, never answers) | 2 held / 0 violated | held |
| CH-08-compiler | Ollama down — the NL compiler | 17 held / 0 violated | held |
| CH-10-settlement | Rolling restart under load — where the spent budget goes | 53 held / 0 violated | held |

---

## CH-01 — Kill Postgres for 30 s

**Expected:** PEPs spend leases, then fail closed; recovery is clean; invariant holds

Run `637e41a3-b1c9-4bf9-8d33-53a8a02bd3f3` at `2026-08-18T13:20:25.326181+00:00`, 32.54 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **16 sweeps** — 4 held, 0 violated, 12 unavailable.

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
| `outage_s` | `30.49` |
| `spent_during_outage` | `200.0000` |
| `served_during_outage` | `8` |
| `refused_during_outage` | `6` |
| `recovery_s` | `0.65` |
| `leased_after_recovery` | `300.0000` |
| `acquire_failures` | `1` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| healthy | 4 | 4 | 0 | `200`: 4 | — | 10.181 | 14.966 |
| during outage | 14 | 8 | 0 | `200`: 8, `429`: 6 | `LEASE_UNAVAILABLE`: 6 | 7.283 | 11.554 |
| after recovery | 4 | 4 | 0 | `200`: 4 | — | 4.787 | 5.032 |

Timeline:

- `  0.000 s`  scenario started
- `  0.077 s`  stopping postgres
- `  0.816 s`  postgres stopped
- ` 31.308 s`  starting postgres
- ` 32.256 s`  postgres accepting connections after 0.7 s
- ` 32.533 s`  scenario ended
- ` 32.533 s`  final sweep

**Notes:**

- The clock is frozen, so no lease expired during the outage. CH-1 is about an unreachable ledger; expiry is CH-3's subject.


---

## CH-01-audit — Kill Postgres for 30 s — the audit path

**Expected:** records buffered during the outage are not lost silently

Run `391c038f-1fca-4307-bde6-0d7dc3009ff7` at `2026-08-18T13:20:58.715522+00:00`, 31.802 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **14 sweeps** — 4 held, 0 violated, 10 unavailable.

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
| `records_landed` | `139` |
| `emitter_failed_batches` | `7` |
| `emitter_lost_records` | `2` |
| `emitter_dropped` | `0` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.099 s`  stopping postgres
- ` 30.738 s`  starting postgres
- ` 31.796 s`  scenario ended
- ` 31.796 s`  final sweep

**Notes:**

- 2 audit records were discarded during the outage while 141 requests were still authorized. The emitter's max_retries path cannot tell a poison batch from an unreachable sink, so ADR-026's deny-on-full back-pressure never engaged. Counted, not silent — but a gap. See STATUS.md gap 20.


---

## CH-03 — SIGKILL one PEP of three

**Expected:** its lease strands <= TTL then reclaims; others unaffected

Run `03a3cb4b-56eb-4753-a091-cf22832749f8` at `2026-08-18T13:21:36.413553+00:00`, 3.044 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **14 sweeps** — 14 held, 0 violated, 0 unavailable.

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
- `  1.711 s`  three leases held
- `  1.735 s`  killing pep-doomed
- `  1.738 s`  pep-doomed exited 1
- `  2.530 s`  reaped one second inside the skew margin
- `  2.566 s`  reaped past the skew margin
- `  3.039 s`  scenario ended
- `  3.040 s`  final sweep

**Notes:**

- Reclamation is observed by advancing an injected `now` into `reap()`, not by sleeping for the TTL. The bound under test is the ledger's `expires_at + S`, which is a value, not a wall-clock wait.


---

## CH-04 — Partition PEP <-> ledger

**Expected:** bounded spend, then fail closed

Run `e93bde39-b227-457e-bf81-5a19ff8ef1de` at `2026-08-18T13:21:45.473136+00:00`, 0.571 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **4 sweeps** — 4 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `leased_at_cut` | `200.0000` |
| `committed_at_cut` | `0.0000` |
| `local_remaining_at_cut` | `120.0000` |
| `served_during_partition` | `6` |
| `spent_during_partition` | `120.0000` |
| `hot_path_p99_ms_partitioned` | `10.16` |
| `hot_path_p99_ms_healthy` | `16.265` |
| `committed_during_partition` | `0.0000` |
| `invariant_held_during_partition` | yes |
| `blackholed_connections` | `1` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| healthy | 4 | 4 | 0 | `200`: 4 | — | 14.36 | 16.265 |
| during partition | 11 | 6 | 0 | `200`: 6, `429`: 5 | `LEASE_UNAVAILABLE`: 5 | 6.17 | 10.16 |
| after heal | 4 | 4 | 0 | `200`: 4 | — | 6.313 | 9.873 |

Timeline:

- `  0.000 s`  scenario started
- `  0.137 s`  cutting PEP -> ledger (blackhole)
- `  0.217 s`  healing the partition
- `  0.561 s`  scenario ended
- `  0.561 s`  final sweep


---

## CH-04-shutdown — Partition PEP <-> ledger — graceful shutdown

**Expected:** a partitioned PEP cannot complete `LeasePool.aclose()`

Run `76087fd0-192b-4b1e-afae-5d4dad1cad28` at `2026-08-18T13:21:47.133235+00:00`, 5.505 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **22 sweeps** — 22 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `graceful_close_completed_within_5s` | no |
| `graceful_close_completed_after_heal` | yes |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  0.438 s`  cutting, then asking the pool to close
- `  5.464 s`  healing; the pending shutdown should now complete
- `  5.500 s`  scenario ended
- `  5.500 s`  final sweep

**Notes:**

- A partitioned PEP cannot complete a graceful shutdown: `aclose()` drains in-flight top-ups and then RELEASEs, and both need the ledger. Worse, the timeout meant to bound it does not: `asyncio.wait_for` cancels into SQLAlchemy's greenlet bridge while asyncpg is blocked on the dead socket, and the driver's own rollback/close needs that same socket — measured stuck for 5 minutes against a 5 s bound. Healing the partition is what releases it. The stranded lease is bounded by TTL + S (CH-3), so this costs availability on restart, not correctness. See STATUS.md gap 21.


---

## CH-08 — Ollama down

**Expected:** template fallback; no hot-path impact

Run `e412b0fb-7303-42fe-a91b-391d77fbea4a` at `2026-08-18T13:21:59.416820+00:00`, 24.274 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **1 sweeps** — 1 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `served_with_ollama_down` | `12` |
| `hot_path_p99_ms` | `6066.837` |
| `hot_path_p50_ms` | `6057.622` |
| `embedding_timeout_s` | `2.0` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| ollama refusing connections | 12 | 12 | 0 | `200`: 12 | — | 6057.622 | 6066.837 |

Timeline:

- `  0.000 s`  scenario started
- ` 24.232 s`  scenario ended
- ` 24.232 s`  final sweep

**Notes:**

- The template fallback PLAN.md §13.2 expects for CH-8 does not exist: T-031 is deferred (PLAN.md §21). CH-8 is therefore PARTIAL on both halves — the fallback is unimplemented, and 'no hot-path impact' is false.
- An unreachable Ollama costs the hot path p50 6057.622 ms / p99 6066.837 ms against a 2.0 s embedding timeout — roughly three timeouts, because scoring one request embeds the task, the action template and the rendered action, and `EmbeddingClient` is a *synchronous* httpx.Client called from the event loop (ADR-037 noted the shape; this puts a number on it). Outcomes stay correct: all 12 requests were allowed, because spec 09 §5 fails drift open. See STATUS.md gap 22.
- Measured on the development host, and it invalidated this scenario's first premise: a connect to a *closed* 127.0.0.1 port does not refuse, it raises ConnectTimeout after ~3 s — port 9 included. There is no cheap-failure mode for an unreachable local service on Windows.


---

## CH-08-blackhole — Ollama black-holed (accepts, never answers)

**Expected:** requests still allowed; the latency cost is measured, not assumed

Run `e0ed223c-aac6-4d1d-bed3-505514b63976` at `2026-08-18T13:22:25.805624+00:00`, 6.192 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **2 sweeps** — 2 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `embedding_timeout_s` | `2.0` |
| `baseline_p99_ms` | `28.79` |
| `blackholed_p99_ms` | `6086.662` |
| `three_concurrent_requests_wall_s` | `6.09` |
| `blackholed_connections` | `2` |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| no intent headers (oracle not consulted) | 6 | 6 | 0 | `200`: 6 | — | 21.441 | 28.79 |
| ollama black-holed | 3 | 3 | 0 | `200`: 3 | — | 4070.502 | 6086.662 |

Timeline:

- `  0.000 s`  scenario started
- `  6.141 s`  scenario ended
- `  6.144 s`  final sweep

**Notes:**

- A black-holed Ollama costs every uncached drift scoring call its full 2.0 s timeout, on the event loop, because `EmbeddingClient` holds a synchronous httpx.Client (ADR-037 already noted the shape). Three concurrent requests took 6.1 s wall against a baseline p99 of 28.79 ms. Outcomes stay correct — spec 09 §5 fails open — but 'no hot-path impact' is only true for a *refused* Ollama, not a hung one. See STATUS.md gap 22.


---

## CH-08-compiler — Ollama down — the NL compiler

**Expected:** typed error inside the timeout; template fallback is unimplemented

Run `f6159bb0-3e1b-4911-bece-db8ff6c04462` at `2026-08-18T13:22:32.914509+00:00`, 5.945 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **17 sweeps** — 17 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `warm_returned` | no |
| `warm_s` | `3.186` |
| `generate_failed_after_s` | `2.754` |
| `generate_error` | `OllamaError` |
| `final_sweep_readable` | yes |

Timeline:

- `  0.000 s`  scenario started
- `  5.941 s`  scenario ended
- `  5.941 s`  final sweep

**Notes:**

- No template fallback exists to exercise: T-031 is deferred (PLAN.md §21), so demo beat 5 has no F-2 recovery while that stays true. This is the resumption trigger STATUS.md records for T-031.


---

## CH-10 — Rolling restart under load

**Expected:** zero dropped requests; invariant holds

Run `39ba268c-2b7c-4f9a-acd0-b713e96e588a` at `2026-08-18T13:22:44.617162+00:00`, 18.9 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **72 sweeps** — 72 held, 0 violated, 0 unavailable.

| Measurement | Value |
|---|---|
| `leased_with_three_instances` | `15000.0000` |
| `restarts` | `3` |
| `requests_sent` | `945` |
| `requests_dropped` | `0` |
| `sweeps_during_restarts` | `53` |
| `settlement_backlog_when_load_stopped` | `415` |
| `settlement_counters` | `applied`: 713, `declined`: 0, `dropped`: 0, `failed_attempts`: 0, `still_pending`: 0, `shutdown_timeouts`: 0 |
| `leased_after_restarts` | `11435.0000` |
| `committed_after_restarts` | `4725.0000` |
| `spent_locally` | `4725.0000` |
| `pool_available_after_restarts` | `183840.0000` |
| `lease_rows_by_state` | `active`: 3, `released`: 3 |
| `final_sweep_readable` | yes |

| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |
|---|---|---|---|---|---|---|---|
| continuous load across the restart | 945 | 945 | 0 | `200`: 945 | — | 14.11 | 31.919 |

Timeline:

- `  0.000 s`  scenario started
- `  0.509 s`  draining pep-roll-0
- `  0.509 s`  stopping pep-roll-0 (RELEASE)
- `  1.783 s`  starting pep-roll-0 (ACQUIRE)
- `  2.037 s`  pep-roll-0 back in rotation
- `  2.037 s`  draining pep-roll-1
- `  2.037 s`  stopping pep-roll-1 (RELEASE)
- `  5.194 s`  starting pep-roll-1 (ACQUIRE)
- `  5.473 s`  pep-roll-1 back in rotation
- `  5.473 s`  draining pep-roll-2
- `  5.477 s`  stopping pep-roll-2 (RELEASE)
- ` 13.973 s`  starting pep-roll-2 (ACQUIRE)
- ` 14.168 s`  pep-roll-2 back in rotation
- ` 18.879 s`  settlement queues drained
- ` 18.895 s`  scenario ended
- ` 18.895 s`  final sweep

**Notes:**

- An instance is the whole PEP object graph rebuilt, with a real RELEASE and a real ACQUIRE against Postgres, but not a new OS process. CH-3 is the real-process scenario. The risk a rolling restart carries is in the ledger, which is fully exercised here.
- Two throughput observations from tuning this scenario, both unrelated to restarts and both the PEP correctly failing closed. (1) With the lease at 500 and four workers, the low-water mark leaves 25 payments of headroom and the load spent it before the top-up's ACQUIRE landed — 14 LEASE_UNAVAILABLE in 10,434 requests; that is the case T-015 (adaptive lease sizing, deferred) exists for. (2) Settlement costs one LEDGER_COMMIT per reservation, each taking FOR UPDATE on the same pool row, so three instances serialize; since `LeasePool._release` drains the queue before retiring a lease, a backlog larger than a lease's spending stalls the top-up behind it — 7,053 refusals in 21,006 requests at concurrency=4, pace=5 ms. Batching settlements is the fix and belongs with T-053.
- 945 requests spent 4725.0000, and the ledger agrees: committed=4725.0000 across 3 restarts. Before T-052 this read committed=0 — no production caller reached LEDGER_COMMIT, so every RELEASE returned spent budget to the pool. Closed by `agentiam_pep.settlement`.


---

## CH-10-settlement — Rolling restart under load — where the spent budget goes

**Expected:** a RELEASE returns only the unspent remainder of the lease

Run `1f0665dd-7a52-4bc4-823a-e3eff5f94b38` at `2026-08-18T13:23:04.958370+00:00`, 13.918 s. Verdict: **held**.

Invariant sidecar, sweeping every 0.25 s: **53 sweeps** — 53 held, 0 violated, 0 unavailable.

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
- `  5.777 s`  stopping pep-roll-0 gracefully (settle, then RELEASE)
- ` 13.913 s`  scenario ended
- ` 13.913 s`  final sweep

**Notes:**

- Instance 0 spent 3000.0000 of a 5000.0000 lease and then released it. The pool got back 2000.0000 — the unspent remainder only — and `committed` moved to 3000.0000. Before T-052 the pool got the whole 5000.0000 back and `committed` stayed at 0, so the 3000.0000 was spendable twice.

