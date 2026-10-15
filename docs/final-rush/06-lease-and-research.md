# 06 — The lease contribution, made unattackable

**Context.** `research/` already holds the plan for this workstream: `lease-plan.md` (gaps
GAP-A…GAP-H), `decisions.md` (RD-1…RD-4) and `related-work.md` (why the *distributed* half of
the budget contribution is what survives). This file turns that plan into tickets. Where a
ticket and `research/` disagree, `research/decisions.md` wins, and the disagreement is a bug in
this file.

**The claim being defended** (`research/lease-plan.md` §1):

> Quantitative mandates enforced across untrusted, partition-prone enforcement points — a
> lease protocol whose safety does not depend on enforcement-point correctness.

**Order** (RD-1 reorders it): T-100 (model) and T-101 (simulator) in parallel → T-102 (skew
check, using the bound the model confirms) → T-103 (restricted role) → T-104 (chaos) → T-105
(baselines) → T-060 (paper). T-100 can run unattended during November.

---

### T-100 — TLA+ model of the lease protocol, two clocks

**Size** L · **Lane** research · **GPU** — · —

**Why.** RD-1. Spec 04's header says "model-checked"; it was 400 random interleavings in an
uncommitted script (GAP-B). An exhaustive model makes the claim true, and it settles GAP-C's
skew bound (`Δ < 2S`, hand-derived) before T-102 implements it.

**Read.** `research/decisions.md` RD-1 (the "What to model" table is the spec for this ticket);
`docs/specs/04-lease-protocol.md` §4, §6, §9, §15, §17; `research/lease-plan.md` GAP-B, GAP-C.

**Probe first.** Install the TLA+ tools in a container (Java, `tla2tools.jar`, pinned version),
model only `ACQUIRE` and `RELEASE` with two PEPs, and confirm TLC finds the G1-removed
counterexample (granted 160 against a total of 100 is the shape spec 04 §15 recorded). This
validates the toolchain before the full model.

**Deliverables.**
- `models/lease/Lease.tla` (PlusCal + translation), `Lease.cfg`, and one config per ablation
  (`NoG1.cfg` … `NoSkewMargin.cfg`).
- State, operations, invariants and bounds exactly as RD-1's table lists, with **two clocks**
  and skew as a model parameter.
- `make lease-model` running TLC in a container and writing `models/lease/results/*.txt`.
- `docs/specs/04-lease-protocol.md` §15 rewritten around the model's results; header wording
  made true.
- ADR for the development-only toolchain (not in the SBOM, not in the runtime image).

**Acceptance criteria.**
- [ ] The full model passes all invariants at the stated bounds; state count and run time recorded.
- [ ] **Each ablation config produces a counterexample**, committed as a trace. A guard whose
      removal passes is reported as such, not hidden.
- [ ] The skew bound is confirmed or refuted, with the trace if refuted. T-102 uses the result.
- [ ] Runs from a clean checkout with Docker only.

**Out of scope.** Liveness beyond what the model states; refinement proofs.

---

### T-101 — Committed lease simulator and guard-ablation matrix

**Size** M · **Lane** research · **GPU** — · —

**Why.** GAP-B1. Makes spec 04 §15's ablation table reproducible immediately, and is the
fallback if T-100 overruns.

**Read.** `research/lease-plan.md` GAP-B; spec 04 §15; `tests/integration/test_ledger_properties.py`.

**Deliverables.**
- `scripts/sim/lease_sim.py`: a deterministic, seeded, randomized interleaving simulator over
  the spec 04 operations, with switches to disable each guard.
- `make lease-sim` writing `docs/benchmarks/lease-ablation.md` with `--check` mode: per guard
  removed, interleavings run, violations found, first violating seed.
- Reproduces the two recorded findings: G1 removed → over-grant; G3 removed → `leased` goes
  negative (spec 04 §15 recorded 55 of 400).

**Acceptance criteria.**
- [ ] Same seed → same output (test).
- [ ] Every guard's removal produces at least one violation within the default run count.

---

### T-102 — Clock-skew check at `acquire()` and CH-7

**Size** M · **Lane** ledger and leases · **GPU** — · —

**Why.** GAP-C: `expires_at` is computed from the PEP's clock (`scripts/pep_service.py`), and the
reaper compares against the control plane's (`app.py`). Nothing verifies the two are within the
skew allowance `S`; beyond `2S` (10 s on defaults) the same budget can be issued twice. Spec 04
§17 open question 1 is still open. CH-7 is cited as TM-22's coverage and never ran.
`research/lease-plan.md` calls this "the highest-value single ticket on this list".

**Read.** `research/lease-plan.md` GAP-C; spec 04 §9.3, §17; `db/ledger.py` `acquire()`, `reap()`;
`agentiam_pep/pool.py`; T-100's result on the bound.

**Depends on.** T-100 (for the bound; may proceed with `2S` if T-100 is blocked, stated in the ADR).

**Probe first.** In an integration test, give one PEP a clock 60 s behind and drive acquire →
expire → reap → acquire. Record whether the same budget is issued twice today. That failing test
becomes the regression test.

**Deliverables.**
- `acquire()` takes the PEP's reported clock; the ledger refuses a lease when
  `|pep_now − ledger_now| > S`, with a named reason code (spec 09 updated).
- `expires_at` computed from the **ledger's** clock, returned to the PEP, which converts it to a
  local deadline with its measured offset. (Decide in the ADR; state why if not chosen.)
- The PEP surfaces a skew refusal in `/readyz` and metrics.
- `tests/chaos/test_ch07_clock_skew.py`: +60 s on one PEP, under load, invariant checker
  running; expected per `PLAN.md` §13.2: tolerance honoured, no double issue, no spurious
  expiries for PEPs within tolerance.
- Spec 04 §17 Q1 closed; TM-22's coverage citation now true; `research/threat-model.md` N6 and
  A4 updated.

**Acceptance criteria.**
- [ ] The probe's double-issue test fails before and passes after.
- [ ] A PEP within `S` is never refused (no false positives at `S − ε`).
- [ ] CH-7 passes under `make chaos`; `chaos-results.md` regenerated.

---

### T-103 — Restricted Postgres role for the PEP

**Size** M · **Lane** ledger and leases · **GPU** — · —

**Why.** RD-2 / GAP-A. The PEP connects as a role that can `UPDATE budgets SET total = …`, so
"safety does not depend on the enforcement point" is a protocol property, not a deployed one.

**Read.** `research/decisions.md` RD-2 — **the grant list there is the spec for this ticket**;
`scripts/pep_service.py` (the four direct-database sinks); `db/models.py`; migrations.

**Probe first.** Create the role with RD-2's grants in a scratch database and run the integration
suite's ledger, audit, settlement and escalation tests connected as it. Record every privilege
error. Fix the grant list from the evidence, not by widening blindly.

**Deliverables.**
- Migration creating `agentiam_pep` with column-level grants exactly as RD-2's table (amended only
  with recorded evidence).
- `docker-compose.demo.yml`, `deploy/k3s/` and `bootstrap_demo_secrets.py` give the PEP this role's
  credentials; the control plane keeps its own.
- **The privilege test RD-2 calls mandatory**: every PEP database operation executed as the
  restricted role in CI.
- Negative tests: as the PEP role, `UPDATE budgets SET total`, `UPDATE audit_records`,
  `DELETE FROM audit_records`, `UPDATE escalations` all fail with a permission error.
- `research/threat-model.md` P4 boundary note updated to what now holds.

**Acceptance criteria.**
- [ ] `make test-integration`, `make test-e2e`, `make chaos` green with the restricted role.
- [ ] Each negative test proven to bite (it passes as the owner role, fails as the PEP role).

---

### T-104 — Remaining chaos scenarios CH-2, CH-5, CH-6, CH-11, CH-12

**Size** L · **Lane** research / chaos · **GPU** — · —

**Why.** Seven of twelve `PLAN.md` §13.2 scenarios never ran (GAP-F). CH-7 is T-102; CH-9 is
T-092. These five remain, and three threat-model entries cite them.

**Read.** `docs/PLAN.md` §13.2; `tests/chaos/` (harness, `faultproxy.py`, `pepstack.py`);
ADR-049, ADR-050; the hazards on asyncpg cancellation, `Server.wait_closed()` and
testcontainers port reassignment.

**Deliverables.** One test file per scenario, each with the invariant sidecar running and a JSON
result:

| Id | Scenario | Expected (`PLAN.md` §13.2) |
|---|---|---|
| CH-2 | Kill Redis for 30 s | Revocation falls back to pull; leases unaffected |
| CH-5 | 500 ms latency on the ledger | Top-ups slow; decisions unaffected |
| CH-6 | 10% packet loss | Retries with backoff; no double spend |
| CH-11 | Postgres connection-pool exhaustion | Graceful 503; fail closed |
| CH-12 | Disk full on the audit ledger | Requests denied (audit loss unacceptable); alert raised |

- `chaos-results.md` regenerated; `docs/threat-model.md` citations made true.

**Acceptance criteria.**
- [ ] Each scenario either passes or is recorded as a found bug with its own fix ticket. A
      scenario that finds a product bug is a success of this ticket; the T-052 suite found four.
- [ ] Full chaos suite stays within `PLAN.md` §13's 20-minute budget, or the overrun is recorded.

---

### T-105 — Baselines B0–B3 through the chaos harness

**Size** L · **Lane** research · **GPU** — · —

**Why.** GAP-D, mandatory under RD-3: every number is AgentIAM against AgentIAM. The four-way
comparison is the paper's core result.

**Read.** `research/lease-plan.md` GAP-D; `tests/chaos/harness.py`; `split_budget()` in the ledger.

**Depends on.** T-104 (and T-102, so CH-7 is in the matrix).

**Deliverables.**
- Baseline enforcement modes behind a PEP configuration switch, used only by the harness:
  **B0** no enforcement; **B1** synchronous central check (one ledger round-trip per call);
  **B2** static split only (`split_budget()`, no shared pool); **B3** AgentIAM leases.
- All four run under CH-1, CH-3, CH-4, CH-7 and a no-fault load profile.
- `docs/benchmarks/baselines.md` generated with `--check`: overspend, refusal rate, wasted budget
  (B2's stranded share), decision latency p50/p99, per baseline per scenario, three runs each.

**Acceptance criteria.**
- [ ] B0 demonstrably overspends (the motivation); B1 never overspends but pays latency; B2 wastes
      budget when demand is skewed; B3's numbers stated against all three.
- [ ] Reproducible by one `make` target.

---

### T-106 — A partitioned PEP shuts down within a bound (gap 21)

**Size** M · **Lane** ledger and leases · **GPU** — · —

**Why.** `LeasePool.aclose()` drains top-ups and then releases, both of which need the ledger; a
partitioned PEP was measured stuck for five minutes against a 5 s bound, because `wait_for`'s
cancellation lands in SQLAlchemy's greenlet bridge (`docs/STATUS.md` gap 21,
`research/lease-plan.md` GAP-H).

**Read.** `docs/STATUS.md` gap 21; `agentiam_pep/pool.py` `aclose()`; the asyncpg cancellation
hazard (start the step as a task, `asyncio.wait` on it without cancelling,
`engine.dispose(close=False)`).

**Deliverables.**
- Shutdown with a hard deadline: best-effort release within the deadline, then abandon; the
  reaper reclaims what was abandoned (now scheduled, gap 27 closed). The abandoned amount is
  logged.
- A chaos test: partition, then shutdown; the process exits within the deadline + 1 s; the ledger
  invariant holds; the abandoned lease is reclaimed by the reaper within TTL + 2S.

**Acceptance criteria.**
- [ ] Exits within the bound in ten consecutive runs.
- [ ] Gap 21 closed.

---

### T-015 — Adaptive lease sizing

**Size** M · **Lane** ledger and leases · **GPU** — · —

**Why.** Deferred since M3. Fixed leases waste budget under low traffic and refuse under bursts.
The algorithm is already specified (spec 04 §12). ADR-015 also notes `ACQUIRE` does not clamp by
`max_fraction` (gap 8) because the clamp conflicts with a fixed requested amount; adaptive sizing
is where that formula belongs.

**Read.** Spec 04 §4.1, §12; ADR-015; `docs/STATUS.md` gap 8; `agentiam_pep/pool.py`.

**Depends on.** T-102.

**Deliverables.**
- Lease size computed from observed spend rate over a window, clamped to `[min, max_fraction ×
  available]`; the `max_fraction` clamp restored in `ACQUIRE` (closes gap 8).
- The T-100 model and the T-101 simulator extended with the clamp; still safe.
- Measured under CH-10 and a bursty profile: refusal rate and stranded budget vs fixed leases.

**Acceptance criteria.**
- [ ] Single-PEP crash blast radius bounded by `max_fraction` (test).
- [ ] Refusal rate under the bursty profile reported against fixed sizing.

---

### T-107 — Ledger service: the PEP holds no database credentials

**Size** L · **Lane** ledger and leases · **GPU** — · —

**Why.** RD-2 deferred it; "complete" includes it. With a narrow ledger API the PEP holds no
database credentials at all, and trust boundary B2 becomes a network boundary rather than a
library call. `PLAN.md` §8 already sketches the API (`/v1/leases`, `/topup`, `/release`,
`/commits`).

**Read.** `docs/PLAN.md` §8 Ledger; `agentiam_pep/pool.py` `LedgerClient` protocol; RD-2.

**Depends on.** T-103, T-077.

**Probe first.** Measure an HTTP round-trip to a local FastAPI ledger endpoint doing one `acquire`
transaction, p50 and p99 under the CH-10 load. Top-ups are off the hot path, so the cost matters
for refusal rate, not decision latency; record both.

**Deliverables.**
- `agentiam_controlplane/ledger_api.py` implementing the `PLAN.md` §8 Ledger endpoints with PEP
  authentication (mTLS from T-109, or a per-PEP credential until then).
- `HttpLedgerClient` implementing `LedgerClient`; the audit, settlement and escalation sinks move
  behind the same service.
- `scripts/pep_service.py` no longer reads `AGENTIAM_PEP_DATABASE_URL` in this mode.
- Chaos suite re-run in this mode.

**Acceptance criteria.**
- [ ] The PEP container has no database credentials in its environment (test).
- [ ] Chaos scenarios CH-1, CH-3, CH-4, CH-10 hold with the service in the path.
- [ ] Refusal rate and top-up latency compared with the direct mode and published.

---

### T-108 — Signed audit checkpoints and external anchoring

**Size** M · **Lane** research / control plane · **GPU** — · —

**Why.** `PLAN.md` §6.8 promises "periodic checkpoints signed by the audit service key"; none
exist. The chain proves integrity, not origin (spec 08 §7 limitation 3), and whoever can write the
table can forge a consistent chain (limitation 2). HDP signs every delegation hop
(`research/gap-analysis.md` G-11). Without this, "non-repudiation" cannot be claimed.

**Read.** `docs/specs/08-audit-chain.md`; `db/audit.py`; `scripts/verify_audit_chain.py`;
`research/related-work.md` on HDP.

**Deliverables.**
- Spec 08 addendum: checkpoint every N records or T seconds; a checkpoint signs
  `(last_seq, last_hash, at)` with an audit key distinct from the token and policy keys.
- `audit_checkpoints` table; `verify_chain()` verifies checkpoint signatures and reports the last
  verified checkpoint.
- External anchoring: the latest checkpoint published somewhere the operator cannot silently
  rewrite. Offline-friendly default: append to a public git repository or a signed release
  asset; decide in an ADR (spec 08 open question 1).
- Optional per-PEP record signatures, evaluated for cost against NFR-1; decide in the ADR.
- Wording across the docs moved from "tamper-evidence" to whatever is now true.

**Acceptance criteria.**
- [ ] A forged-but-consistent chain (rewritten by a superuser) is detected by checkpoint
      verification (test).
- [ ] A tampered checkpoint signature is detected.

---

### T-060 — Paper draft

**Size** L · **Lane** research · **GPU** — · —

**Why.** RD-3: a top-tier security venue, preprint first. Three collisions appeared in the seven
weeks before the research audit; a preprint stakes the claim.

**Read.** `research/` in full, in its README's order; T-100…T-105 results; `research/related-work.md`
§5.1 (the revised contribution claim) and §6.

**Depends on.** T-100, T-101, T-102, T-103, T-104, T-105 (T-091 strongly preferred).

**Probe first.** Re-read AIP, Agent Contracts, Token Budgets and HDP in full for any change since
the audit (`research/README.md` reading order). Re-run the related-work search for the last 60
days. Record new collisions before writing.

**Deliverables.**
- `research/paper/`: LaTeX source in the target venue's template; the claim from
  `lease-plan.md` §1; the model, the proof, the ablation, the baselines, the chaos results, the
  limitations (GAP-G, GAP-H, TM-23) stated as limitations.
- TM-26 (the parser differential) folded in as motivation, not split out (RD-3); disclosure to the
  affected projects handled separately.
- Every [VERIFY] citation in `research/` verified or removed.
- An artifact appendix: how to reproduce every table from `make` targets.

**Acceptance criteria.**
- [ ] Every number in the paper traces to a committed artefact and a `make` target.
- [ ] An outside reader (faculty member) has reviewed a full draft; their comments recorded.
