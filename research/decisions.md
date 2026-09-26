# Research-Track Decisions

Decisions for the lease paper, in the form `docs/DECISIONS.md` uses: what was decided, why, and
**what it costs**. Numbered `RD-n` so they never collide with the project's ADR sequence.

Raised in [`lease-plan.md`](lease-plan.md) §6, decided 2026-09-27.

---

## RD-1 — Write a TLA+/PlusCal model of the lease protocol and check it with TLC

**Status:** accepted · **Closes:** [`lease-plan.md`](lease-plan.md) GAP-B2

### Decision

Model spec 04 §4's operations in PlusCal and check the pool invariant exhaustively with TLC
over small bounds. Commit the model, the configuration, and the TLC output to the repository.
Keep the randomized simulator (GAP-B1) as well — it is hours of work and it makes §15
reproducible immediately, before the model lands.

### Why

The phrase **"model-checked"** already appears in spec 04's header. Today it describes 400
random interleavings, and the script behind them was never committed — `7a9e9a3` added the spec
and no model. So the project's most valuable table, the guard ablation, is not reproducible by a
reader. There were two ways out: reword the claim down, or make it true. The protocol is seven
operations and a four-state lease machine — small enough that making it true is the cheaper
correction, and it converts the weakest claim in the paper into the one a reviewer cannot argue
with.

### What to model — and the part that changes the plan

| Element | Detail |
|---|---|
| **State** | `total`, `committed`, `leased`, `allocated`; a set of leases (`granted`, `settled`, `state`, `expires_at`); the set of used `reservation_id`s for G4 |
| **Operations** | `ACQUIRE`, `LEDGER_COMMIT`, `RELEASE`, `REAP`, `REVOKE`, `SPLIT`. `RESERVE`/`COMMIT`/`REFUND` are PEP-local and touch no shared state — abstract them to the `LEDGER_COMMIT` they eventually produce, which is what `tests/integration/test_ledger_properties.py` already does and says so |
| **Clocks** | **Two, explicitly** — a PEP clock and a ledger clock, with skew bounded by a model parameter. This is the point (see below) |
| **Invariants** | `committed + leased + allocated ≤ total`; `leased ≥ 0`; `leased = Σ over active leases (granted − settled)` |
| **Bounds** | 2–3 PEPs, 2–3 leases, small integer amounts. Enough for every interleaving class, small enough for TLC |
| **Ablation** | One configuration per guard removed (G1, G2, G3, G4, skew margin). **TLC must find a counterexample for each.** A guard whose removal leaves the model correct is not protecting anything — the same test spec 04 §15 already applies, made exhaustive |

**The part that reorders Phase 1.** GAP-C's safety bound — that a lagging PEP is safe only while
skew `Δ < 2S` — is currently **hand-derived, by me, in this audit**. A two-clock model settles it
exhaustively: it either confirms `2S` or produces the interleaving that refutes it. So **the
model comes before the skew check is implemented**, not after, because the model tells you which
bound to implement. This is the same ordering the project already uses everywhere else: probe
first, then write the thing the probe justified.

### Cost

- Roughly a week including learning TLA+ from nothing, and that estimate is soft — nobody here
  has used it. The risk is the learning curve, not the model.
- A new toolchain (TLA+ Toolbox or the `tla2tools` jar) that nothing else in the project needs.
  It is a *development* dependency, not a runtime one, so it does not touch the shipped
  dependency set or the SBOM.
- If the week overruns, GAP-B1's committed simulator is already the fallback and the claim gets
  reworded instead. **Nothing downstream blocks on RD-1** — treat it as high-value and
  interruptible.

### Consequence for the paper

"Hand-proved via a potential function, and machine-checked exhaustively over bounded
configurations, with each guard shown load-bearing by ablation" is a materially stronger
sentence than anything available today, and it is the kind of claim that survives a hostile
review unchanged.

---

## RD-2 — Give the PEP a restricted database role now; defer the ledger service

**Status:** accepted · **Closes:** [`lease-plan.md`](lease-plan.md) GAP-A, partially

### Decision

Create a dedicated Postgres role for the PEP with column-level privileges, and use it in
`docker-compose.demo.yml` and `deploy/k3s/`. **Do not** build the ledger service for this paper;
record it as the architecturally correct answer and as future work.

### Why

`scripts/pep_service.py` requires `AGENTIAM_PEP_DATABASE_URL` and opens Postgres sessions
directly. Verified this pass: it wires **four** direct-database sinks — the ledger client
(`ACQUIRE`/`RELEASE`), `LedgerAuditSink`, `LedgerSettlementSink` and `LedgerEscalationSink` —
all sharing **one credential**. `LedgerClient` is a Protocol with no other production
implementation. So trust boundary B2 is a library call, and spec 04 §6's "safety does not depend
on PEP correctness" is a property of the *protocol*, not of the *deployment*.

A restricted role is hours of work and converts the claim for the case that matters. The ledger
service is the right answer and is a ticket of its own — it changes the hot path, and the paper
does not need it if the boundary is stated precisely.

### The grant list

Derived from what the four sinks actually do, not from a guess. Postgres supports column-level
`GRANT`, which is what makes this work at all.

| Table | Grant | Withheld, and why |
|---|---|---|
| `budgets` | `SELECT`; `UPDATE (leased, committed)` | **No `UPDATE` on `total`** — this is the whole point; `total` is the ceiling the invariant is relative to. No `UPDATE` on `allocated` either: only `split_budget()` changes it and that is a control-plane operation |
| `leases` | `SELECT`, `INSERT`, `UPDATE (settled, state)` | No `UPDATE` on `granted` — a lease's size is fixed at issue |
| `reservations` | `SELECT`, `INSERT` | No `UPDATE`/`DELETE` — G4's dedup record must be immutable or idempotency is defeated |
| `audit_records` | `SELECT`, `INSERT` | **No `UPDATE`, no `DELETE`** — a compromised PEP must not be able to rewrite or erase history |
| `audit_chain_head` | `SELECT`, `UPDATE (last_seq, last_hash)` | Required — `append()` locks and advances the head |
| `escalations` | `SELECT`, `INSERT` | **No `UPDATE`** — the PEP *opens* escalations; approving one is the console's job and requires a real session |
| `revocations` | `SELECT` | No write — publishing a revocation is a control-plane operation |
| everything else | `SELECT` as needed | No write |

### What this does and does not buy — state it exactly

- ✅ A compromised PEP **cannot raise `total`**, so the invariant now binds against a ceiling it
  does not control. This is what makes the A4 claim defensible.
- ✅ It **cannot rewrite or delete audit records**. It *can* still corrupt `audit_chain_head`,
  which **breaks** the chain — and a broken chain is exactly what `verify_chain()` detects. The
  precise guarantee is that it can make tampering *evident*, never *invisible*. That is the
  right trade and it should be written that way.
- ✅ It cannot approve its own escalations at the database level.
- ❌ It can still under-report spend within its lease (TM-23, an accepted risk, unchanged) and
  exhaust its own lease. Both are bounded by the lease, which is the design.
- ❌ It still reads every row it can `SELECT`. **Confidentiality against a compromised PEP is
  not addressed by this and should not be claimed.**

### Cost and the real risk

The risk is **over-tightening**: a grant that is one column short fails a legitimate operation
at runtime, under load, in the money path. Mitigation is mandatory, not optional — **a test that
exercises every ledger, audit, settlement and escalation operation while connected as the
restricted role**, so a missing privilege fails in CI rather than in a demo. Without that test
this change is a liability rather than an improvement.

---

## RD-3 — Target a top-tier security venue; let that raise the evaluation bar

**Status:** accepted · **Supersedes:** nothing — no venue had been chosen

### Decision

Aim for a top-tier venue rather than a workshop, with **no deadline chosen yet**. Preprint
first, as [`related-work.md`](related-work.md) §6 argues. Do not fragment the work into several
small papers.

### Candidate venues

All four are top-tier and in scope for this work. Deadlines move annually — **check the current
call before committing to one; nothing here should be read as a date.**

| Venue | Why it fits |
|---|---|
| **NDSS** | Where Macaroons was published (2014). Direct intellectual lineage, and reviewers who already know caveat-based delegation — so the "caveats cannot express aggregate consumption" argument lands without setup. Strongest narrative fit |
| **USENIX Security** | Broad, values a working system with real measurement, and has a strong artifact-evaluation culture the repository is unusually well-prepared for |
| **ACM CCS** | Strong on protocols with formal analysis. RD-1's TLC results would be read as a contribution here rather than as supporting material |
| **IEEE S&P** | Highest bar of the four; wants a formal result *and* a system. Viable only with RD-1 finished and GAP-D/GAP-E closed |

If the work is later reframed as distributed systems rather than security, **NSDI** or
**EuroSys** become candidates — but the adversary model is central to the contribution, so a
security venue is the better home.

### What "big" costs — the honest part

Aiming high is not free, and it promotes three items from optional to mandatory:

1. **GAP-D (baselines) becomes mandatory.** A top-tier paper with no baseline is desk-rejected.
   The four-way comparison — no enforcement / synchronous central check / static split /
   AgentIAM leases — is the core result, and it does not exist yet.
2. **GAP-E (NFR-2 off-box) becomes mandatory.** "This host cannot establish the p99 either way"
   is admirable in a repository and fatal in a submission. It needs a second machine.
3. **Artifact quality matters more**, which raises RD-1 and GAP-B1's value again.

And one thing it rules out: **do not split TM-26 (the parser differential) into its own
paper.** It is sharp enough to tempt, but fragmenting weakens both halves. Fold it in as the
measurement that motivates byte-exact enforcement — it is the strongest available answer to "why
isn't reasoning about the parsed tool call enough?" — and disclose it separately to upstream and
to the affected projects, which gets the credit without spending the contribution.

### Deferred

Choosing the specific venue and deadline. Revisit once RD-1 lands and GAP-D has a result table;
the evidence available then should pick the venue, not the other way round.

---

## RD-4 — Do not assign work across the team yet

**Status:** deferred by decision

Phase 1's items are independent and parallelize cleanly — RD-1's model, RD-2's role and grants,
and GAP-B1's simulator touch nothing in common. Recorded so the parallelism is not forgotten
when it is time to split the work. No allocation now.

---

## What these decisions change in the plan

[`lease-plan.md`](lease-plan.md) §4's Phase 1 is reordered by RD-1:

1. **RD-1 — the TLA+ model, two clocks included.** First, because it decides GAP-C's bound
   rather than assuming the one derived by hand in this audit.
2. **GAP-B1 — commit the randomized simulator + ablation matrix.** In parallel; hours, and it
   de-risks RD-1 overrunning.
3. **GAP-C — implement the skew check at `acquire()` with the bound the model confirms, then
   write CH-7.** Closes spec 04 §17 open question 1 and corrects TM-22's false coverage
   citation.
4. **RD-2 — the restricted role, with the privilege test that makes it safe.**

Phase 2 (GAP-D's baselines) and Phase 4 (GAP-E off-box) are both **mandatory** now rather than
desirable, per RD-3.
