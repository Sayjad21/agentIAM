# The Lease Contribution — Plan and Gaps

**Scope.** The lease protocol is what survives the collisions in
[`related-work.md`](related-work.md) §4. This document says what the claim is, what already
backs it, what has to be built before a reviewer sees it, and in what order.

**Grounding.** Every gap below was found by reading the running system at commit `5c2c6ee`,
not by reading the spec. Four of the eight (A, B, C, and F's false-citation half) are new to
this pass and are not in `docs/STATUS.md`'s gap register.

---

## 1. The claim

> **Quantitative mandates enforced across untrusted, partition-prone enforcement points — a
> lease protocol whose safety does not depend on enforcement-point correctness.**

Three parts, each of which has to be separately defended:

| Part | Why it survives §4's collisions |
|---|---|
| **Quantitative, multi-dimensional** | Agent Contracts has this too. **Not novel alone** — background |
| **Distributed** — independent PEPs, partition, crash, clock skew | Agent Contracts is single-process (verified against its full text); Token Budgets is compile-time single-runtime. **This is the novelty** |
| **Safety independent of the enforcement point** | Nobody else claims it. **This is the sharpest sentence available** — and GAP-A below is the reason it is not yet fully true |

---

## 2. What already backs it

Real assets, not aspirational:

- **A potential-function safety proof.** `Φ = committed + leased`; only `ACQUIRE` raises it and
  by at most `total − Φ` under G1. Spec 04 §6. This is a valid hand proof.
- **Guard ablation with measured failure modes** — G1 removed → 160 granted against a total of
  100; G3 removed → `leased` negative in 55/400 interleavings. Spec 04 §15. *The failures are
  the science.*
- **A database-level `CHECK` constraint** (`ck_budgets_invariant`,
  [`db/models.py:76`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/models.py#L76))
  enforcing `committed + leased + allocated <= total` in Postgres, not only in application code.
- **A stateful Hypothesis machine** against real Postgres (`tests/integration/test_ledger_properties.py`),
  honest in its own docstring about which operations it does and does not cover.
- **A 50-concurrent-acquire test** and five chaos scenarios that held.
- **Two enforcement modes** — shared pool (default, dynamic, `FOR UPDATE`) and proportional
  split (static). Agent Contracts has only the static shape.
- **The chaos suite found four real bugs**, including that settlement never reached the ledger.

---

## 3. Gaps, ordered by whether the paper fails without them

### GAP-A — the enforcement point holds database credentials *(new, critical)*

**The headline claim is true of the protocol and not of the deployment.**

`scripts/pep_service.py` requires `AGENTIAM_PEP_DATABASE_URL` and opens `AsyncSession`s
directly against Postgres ([`pep_service.py:494`](../scripts/pep_service.py#L494)). `LedgerClient`
([`pool.py:61`](../packages/agentiam-pep/src/agentiam_pep/pool.py#L61)) is a Protocol, and the
**only production implementation talks straight to the database**. There is no HTTP or gRPC
ledger service. Trust boundary B2 is a library call, not a boundary.

So guards G2/G3 bind only while the PEP *chooses* to go through `ledger.py`. A compromised PEP
issues its own SQL.

**What the database still saves, and what it does not.** `ck_budgets_invariant` is a real
`CHECK`, so even arbitrary SQL cannot make `committed + leased + allocated > total` — Postgres
refuses the transaction. **But `total` is an ordinary mutable column.** A compromised PEP runs
`UPDATE budgets SET total = 10000000` and the invariant holds against a number it chose. It can
equally run `UPDATE budgets SET committed = 0`, which satisfies the constraint and erases the
record of spend.

**So the precise, defensible claim today is:**

- ✅ Holds against a PEP that **follows the protocol and lies** about amounts — G2 clamps, G3
  refuses. This is the realistic threat and it is genuinely proved.
- ✅ The invariant's *shape* holds against arbitrary SQL, via the `CHECK`.
- ❌ Does **not** hold against a PEP with **arbitrary code execution**, because it can move the
  ceiling the invariant is relative to.

**Fix, cheapest first:**
1. **A restricted Postgres role for the PEP** — no `UPDATE` on `budgets.total`, no `DELETE` on
   `audit_records`. Hours of work, no architectural change, and it converts the claim from
   "protocol property" to "deployed property" for the most important case.
2. **A real ledger service** with a narrow API, so the PEP never holds credentials. This is the
   architecturally correct answer and is a ticket of its own.

Option 1 is enough for the paper if stated precisely, and is the one chosen —
[`decisions.md`](decisions.md) RD-2 carries the grant list and the privilege test that makes it
safe. **Do not claim A4-resistance until it lands.** [`threat-model.md`](threat-model.md) §5 P4
has been corrected to match.

### GAP-B — the ablation model is not in the repository *(critical for reproducibility)*

Spec 04's header says the arguments were **"model-checked."** §15 is **400 random
interleavings** with per-guard ablation. Those are different claims, and the artifact behind
even the weaker one does not exist: commit `7a9e9a3` ("T-004: Specify the lease protocol,
model-checked before writing") **added the spec and no model** — verified against the full
history. It was a scratchpad script, per the project's own probe habit.

**Consequence:** the single most scientifically valuable table in the project — the one showing
each guard is load-bearing — **cannot be reproduced by a reader.** A reviewer will ask for it.

**Two options, and I recommend doing both:**

| | Effort | What it buys |
|---|---|---|
| **B1 — commit a deterministic randomized simulator** with seeded interleavings + the ablation matrix, wired to a `make` target | Hours to a day | Makes §15 reproducible immediately. Do this regardless |
| **B2 — write a TLA+/PlusCal spec and run TLC** over small bounds (2–3 PEPs, 2 leases, crash/reap/late-commit) | ~1 week including learning, if nobody knows TLA+ | Makes "model-checked" **true**, exhaustively, rather than needing a reword. Converts the weakest claim into the strongest |

The protocol is small — seven operations, a four-state lease machine. This is a tractable TLA+
target, not a research project. **Both accepted — see [`decisions.md`](decisions.md) RD-1**, which
also sets what to model and why the model must come before GAP-C's fix.

### GAP-C — clock skew is assumed, never verified *(critical: an unchecked assumption)*

Spec 04 §9.3 states plainly that safety depends on skew staying within `S`, and §17 **open
question 1** — *"Should the ledger refuse a lease to a PEP whose reported clock is skewed
beyond S?"* — is **still open**, owned by T-013, which shipped long ago.

**Measured against the running system:** `expires_at = now + ttl` where `now` is the **PEP's own
clock** ([`pep_service.py:502`](../scripts/pep_service.py#L502)); the reaper later compares
against the **control plane's** clock ([`app.py:209`](../packages/agentiam-controlplane/src/agentiam_controlplane/app.py#L209)).
Two genuinely different clocks, bridged only by the `2S` margin. `SKEW_ALLOWANCE` appears
**only** in `reap()`'s cutoff — nothing anywhere checks that a PEP's clock is within tolerance.

Working the algebra for a PEP whose clock lags by Δ: the PEP stops using the lease at true time
`t_acquire + ttl − S`, while the reaper reclaims at `t_acquire + ttl + S − Δ`. Safety requires
the reclaim to come after the stop, i.e. **Δ < 2S** — 10 s with the shipped defaults
(`ttl = 60 s`, `S = 5 s`). Beyond that, the same budget is issued twice. **Nothing detects or
prevents it.**

**And the test that would have caught it never ran.** CH-7 (clock skew, +60 s on one PEP) is
`not run — deferred` in `docs/benchmarks/chaos-results.md`, and no `test_ch07*` file exists — yet
`docs/threat-model.md` cites CH-7 as TM-22's coverage. The mechanism *is* unit-tested at both
ends (`test_pep_pool.py:324`, `:342`; `test_ledger.py:233`, `:260`); what was never exercised is
the two-clock interaction end to end.

**Fix:** have `acquire()` take the PEP's reported clock and refuse a lease beyond `S` — this
closes §17 Q1 with the answer the spec itself already suggests — then write CH-7 to prove both
the refusal and the double-issue it prevents. **This is the highest-value single ticket on this
list**: it closes a spec open question, removes an unchecked assumption from the safety
argument, corrects a false coverage citation, and produces a measured result for the paper.

### GAP-D — no baselines *(the paper reads as a system description without them)*

Every number is AgentIAM against AgentIAM. The fix is now cheap because the comparisons can be
built in the existing harness — you do not need anyone else's code:

| Baseline | Shape | What it demonstrates |
|---|---|---|
| **B0 — no enforcement** | forward everything | the overspend that motivates the work |
| **B1 — synchronous central check** | one ledger round-trip per call, OAP-shaped | correctness at the cost of latency; OAP reports 53 ms median against your ~430 µs decision |
| **B2 — static split only** | `split_budget()`, no shared pool | Agent Contracts' shape. Shows the waste when one child needs more than its share |
| **B3 — AgentIAM leases** | shared pool + `FOR UPDATE` | the contribution |

Run all four through the chaos harness under CH-1/CH-3/CH-4/CH-7 and report overspend,
refusal rate, and latency. **This table is the paper's core result** and it does not exist yet.

### GAP-E — NFR-2 is not established

100 RPS enforcement p99 ranged **1.753–74.724 ms** across three runs; 500 RPS is unofferable —
the *stub upstream alone* managed 138 RPS at p50 335 ms. The generator, three uvicorn processes
and Postgres share one machine. **Needs the generator off-box.** Until then, cite NFR-1 (the
in-process decision, 430.4 µs) and state NFR-2 as unestablished, which is what
`docs/benchmarks/performance.md` already does.

### GAP-F — seven of twelve chaos scenarios never ran

CH-2, CH-5, CH-6, CH-7, CH-9, CH-11, CH-12. For the lease paper the ones that matter are
**CH-7** (GAP-C), **CH-5** (500 ms ledger latency — top-up behaviour) and **CH-6** (10% packet
loss — no double-spend under retry). CH-11 (pool exhaustion) matters for the fail-closed claim.
Three threat-model entries cite scenarios in this list as coverage.

### GAP-G / GAP-H — state as limitations, do not fix for the paper

- **GAP-G** (STATUS gap 25): a deployed PEP serves exactly one mandate — `LeasePool` binds
  `mandate_id` at construction. A deployment limitation, not a claim-breaker: the contribution
  is *several PEPs sharing one mandate's pool*, which is exactly what is supported. **State it.**
- **GAP-H** (STATUS gap 21): a partitioned PEP cannot shut down and no timeout bounds it —
  measured stuck 5 minutes against a 5 s bound. Availability, not safety. **State it**, and note
  the reaper now reclaims (gap 27 closed).

---

## 4. Sequence

**Phase 0 — lock the claim (this week, no code).** Read AIP in full with one question: does it
have any quantitative dimension? Confirm the §1 wording. Decide B1 vs B2 (§6).

**Phase 1 — close the two credibility gaps (1–2 weeks).**
1. GAP-C: skew verification at `acquire()` + CH-7. *Start here* — highest value, closes a spec
   open question, fixes a false citation.
2. GAP-B1: commit the simulator + ablation matrix behind a `make` target.
3. GAP-A fix 1: restricted Postgres role for the PEP.
4. GAP-B2 if chosen: TLA+ spec and TLC run.

**Phase 2 — the result table (1–2 weeks).** GAP-D's four baselines through the chaos harness;
CH-5 and CH-6 from GAP-F. This is the paper's core evidence.

**Phase 3 — evaluation breadth (parallel, if time).** Run APort Vault
([`related-work.md`](related-work.md) §4.4) — public, released, in-domain, and it explicitly
does not test spend caps. Extending it with a budget track is itself a contribution.

**Phase 4 — performance (blocked on hardware).** GAP-E off-box generator.

**Phase 5 — write.** Preprint before venue; three collisions appeared in the seven weeks before
this audit.

---

## 5. What to cut

- **Drift detection** — rule-based v0, half the features deferred, NFR-9 never measured.
- **The NL→policy compiler** — 90% on n=30 via hosted inference; LACE/NLAC/U-XACML are better
  evaluated. Demo asset, not a contribution.
- **Attenuation as a contribution** — background, cite AIP.
- **Chain of custody** — unless the chain gets signed; HDP signs every hop.

---

## 6. Decisions — all four resolved

Decided 2026-09-27 and recorded with their costs in [`decisions.md`](decisions.md).

| | Decision | Effect here |
|---|---|---|
| **RD-1** | **TLA+/PlusCal + TLC, accepted.** Keep the randomized simulator too | **Reorders Phase 1** — the model goes first, because GAP-C's `Δ < 2S` bound is hand-derived in this audit and a two-clock model settles it exhaustively. Model, then implement the bound it confirms |
| **RD-2** | **Restricted Postgres role now**, ledger service deferred as future work | GAP-A closes for the case that matters. Grant list in `decisions.md`; **the privilege test is mandatory**, not optional — an over-tight grant fails in the money path under load |
| **RD-3** | **Top-tier security venue**, no deadline chosen. NDSS / USENIX Security / CCS / S&P | **GAP-D and GAP-E are promoted from desirable to mandatory.** Also: do not split TM-26 into its own paper — fold it in and disclose separately |
| **RD-4** | Team allocation deferred | Phase 1's four items parallelize; noted for when it is time |

### Phase 1, reordered by RD-1

1. **The TLA+ model, two clocks included** — decides GAP-C's bound.
2. **GAP-B1, the committed simulator + ablation matrix** — hours, in parallel, de-risks RD-1.
3. **GAP-C, the skew check + CH-7** — with the bound the model confirms.
4. **RD-2, the restricted role + its privilege test.**
