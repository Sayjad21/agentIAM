# AgentIAM — Gap Analysis: Claimed vs. Implemented

**Purpose.** Every externally-facing claim, marked binary: **implemented** (with `file:line`),
**partial**, or **aspirational**. Written so that no claim in a paper, deck, or submission
rests on something that is not there.

**Where the claims come from.** The pitch deck is not in this repository — I searched for it
and found nothing. The claim set below is therefore reconstructed from the three surfaces that
*are* here and that the deck was built against:

- **`README.md`** — the public claim surface
- **`docs/DEMO.md`** — the 8 demo beats, the "three numbers," and the four judge one-pagers
- **`docs/PLAN.md`** — NFR-1…NFR-10 and the §18.1 contribution list

If the deck makes a claim absent from all three, it is not covered here. **Check it against
this document before presenting.**

**Verification basis.** Source read at commit `5c2c6ee`. I did **not** execute the test suite
in this session, so "implemented" means *the enforcing code exists at the cited location and a
named test covers it*, not *I watched it pass today*. Where a number is quoted, it is the
number the repository's own generated evidence files report.

**Legend** — ✅ implemented · ⚠️ partial · ❌ aspirational

---

## 1. Headline product claims (`README.md`)

| # | Claim | Verdict | Evidence / shortfall |
|---|---|---|---|
| C-1 | "Every AI agent and sub-agent gets its own cryptographic identity carrying a strictly narrower set of permissions than its parent" | ✅ | [`attenuation.py:289` `attenuate()`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L289); narrowing order [`attenuation.py:144`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L144). Property-tested P-01/P-02, `tests/property/test_attenuation.py` |
| C-2 | "A bounded spend budget" | ✅ | Ledger guards G1–G4, [`ledger.py:129` `acquire()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L129), [`ledger.py:239` `ledger_commit()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L239) |
| C-3 | "Enforced at the tool boundary in under a millisecond" | ✅ | `decide()` p99 **430.4 µs** against a 1000 µs budget, `docs/benchmarks/performance.md`. **Quote this as the decision, never as the request** — full in-process cost is ~513 µs incl. `verify()`, and the proxy hop is milliseconds |
| C-4 | "A complete chain of custody for every action taken" | ⚠️ | Chain exists — [`audit.py:101` `append()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/audit.py#L101), [`audit.py:231` `custody()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/audit.py#L231). "Complete" overstates: records are emitted pre-forward, so a record exists for a call the upstream may then fail (documented, deliberate, [`pipeline.py:1-18`](../packages/agentiam-pep/src/agentiam_pep/pipeline.py#L1)) |
| C-5 | "Holder-side attenuation — no issuer round-trip" | ✅ | No I/O in the module; enforced statically by `tests/unit/test_core_purity.py`; P-05 |
| C-6 | "Quantitative mandates — spend, call count, rows read, wall clock, external emails" | ✅ | `BudgetDimension` in [`models.py`](../packages/agentiam-core/src/agentiam_core/models.py); one authority-block check per dimension, [`tokens.py:192`](../packages/agentiam-core/src/agentiam_core/tokens.py#L192) |
| C-7 | "Enforced under concurrency **and network partition**" | ⚠️ | Concurrency: ✅ (50-concurrent-acquire test; `FOR UPDATE` at [`ledger.py:176`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L176)). Partition: ✅ for *safety* (CH-4 held) but **a partitioned PEP cannot shut down** and no timeout bounds it — measured stuck 5 min against a 5 s bound (gap 21) |
| C-8 | **"Every dependency is free and open source… no proprietary black-box API anywhere in this system, and all model weights are open-weight and self-hosted. That is a design constraint, not a budget compromise."** | ❌ | **Flatly contradicted by shipped code.** [`llm.py:47`](../packages/agentiam-controlplane/src/agentiam_controlplane/nl_compiler/llm.py#L47) calls `api.groq.com`; `GeminiClient` calls Google. ADR-040 supersedes ADR-032's no-egress guarantee. Mitigation: `client_from_env()` falls back to local Ollama with no key set ([`llm.py:495`](../packages/agentiam-controlplane/src/agentiam_controlplane/nl_compiler/llm.py#L495)), so the *default* is local — but **the headline 90% compiler figure was measured on hosted Gemini**. See C-27 and G-5 |
| C-9 | "The hot path never blocks on the network" | ⚠️ | True by construction for revocation/policy/budget — all local, [`decision.py:1-22`](../packages/agentiam-core/src/agentiam_core/decision.py#L1). **Except drift**: `EmbeddingClient` is a *synchronous* `httpx.Client` on the event loop, and a hung Ollama stalls it (~3 timeouts/request, measured, gap 22, open) |
| C-10 | "Fail closed. No lease, stale policy bundle, unreachable control plane → deny" | ⚠️ | Lease ✅ [`decision.py:393`](../packages/agentiam-core/src/agentiam_core/decision.py#L393); unreachable control plane ✅ [`decision.py:272`](../packages/agentiam-core/src/agentiam_core/decision.py#L272), [`:340`](../packages/agentiam-core/src/agentiam_core/decision.py#L340). **Stale policy bundle ❌ — `POLICY_BUNDLE_STALE` is unreachable in a deployed PEP** (gap 26): `pep_service.py` loads a bundle once from disk and bypasses `PolicyCache` |
| C-11 | "Every deny is explainable — names the exact caveat, policy statement, or budget" | ✅ | First-failing-step-wins, [`decision.py:228`](../packages/agentiam-core/src/agentiam_core/decision.py#L228); `explain()` in `decisions_api.py` names the failing caveat inline |
| C-12 | "The ledger is the only authority on budget. Enforcement points hold leases, not truth" | ✅ | Guards G2/G3 hold the invariant **even against a lying PEP** — `docs/specs/04-lease-protocol.md` §6. This is the strongest claim in the system and is under-sold in the README |
| C-13 | "Tokens are immutable. Elevation issues a new token" | ✅ | Biscuit append-only; `attenuate()` returns a new base64 string |
| C-14 | README **Status**: "Milestone 1 — Foundation, in progress. The workspace, tooling and CI are in place (T-001); the protocol specs and domain models are next" | ❌ | **Badly stale.** M1–M11 are complete, 46+ tickets done. The public-facing README understates the project by ten milestones. Cheapest high-value fix in the repository |

---

## 2. The eight demo beats (`docs/DEMO.md`)

| Beat | Claim | Verdict | Evidence / shortfall |
|---|---|---|---|
| 1 | Human approves a task; root token minted; intent hash shown | ✅ | [`tokens.py:131` `mint_root()`](../packages/agentiam-core/src/agentiam_core/tokens.py#L131); `make demo-seed` (`Makefile:32`) |
| 2 | Root spawns 3 sub-agents; each node's scopes visibly smaller; block ids shown | ✅ | `db/tree.py`, `tree_api.py`, `console/identity-tree.html` |
| 3 | Doc-reader attempts payment, **denied in 0.4 ms**, naming the caveat | ✅ | Consistent with the measured 430.4 µs p99. Honest as stated — it says *denied*, not *proxied* |
| 4 | Judge sets ৳50,000; spend across 3 concurrent sub-agents hard-stops; invariant checker green | ✅ | `scripts/demo_concurrency.py` + `make demo-conc` (`Makefile:40`); `run_invariant_checker.py`. Note: **individual grants are not deterministic** — spec 04 §13 says only `Σ granted = min(Σ requested, available)`. Do not promise a 100/50/0 split on stage |
| 5 | Judge writes English → Cedar → generated tests → diff → activate → enforced | ⚠️ | Pipeline ✅ (`nl_compiler/`, T-030 dual-gating). **But 27/30 = 90%**: 1 wrong policy and 2 unparseable out of 30. Roughly a 1-in-10 chance of visible failure on a judge's unrehearsed sentence, and it needs hosted inference for that rate (C-8) |
| 6 | Drift score crosses the band; escalation raised to a human | ⚠️ | Wiring ✅ ([`decision.py:355`](../packages/agentiam-core/src/agentiam_core/decision.py#L355), `escalation.py`, queue UI). **The oracle is a v0 rule-based heuristic**; f3/f4/f6 deferred, classifier and labelled dataset deferred, NFR-9 never measured. Demo-safe because scripted; not a calibrated detector |
| 7 | Revoke root → 12 agents across 3 PEPs die; timer shows propagation | ✅ | `tests/integration/test_subtree_revocation.py`, `test_revocation_nfr4.py` (3 PEPs, < 2 s p99) |
| 8 | Custody to the human + permitting caveat; verify chain live; tamper and watch it caught | ✅ | [`audit.py:173` `verify_chain()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/audit.py#L173); `make demo-tamper` / `demo-untamper` (`Makefile:49`) |
| — | "`make demo-reset` returns to a known state in under 10 seconds" | ⚠️ | Target exists (`Makefile:61`). The **< 10 s** figure is not measured anywhere I can find; `demo-up` cold start is measured at 20 s/19 s against NFR-8's 90 s. Time it before relying on it |

---

## 3. The "three numbers" for the close

| # | Number | Verdict | Shortfall |
|---|---|---|---|
| N-1 | Decision latency p99 (in-process) | ✅ | **430.4 µs**, breakdown published. Solid — lead with it |
| N-2 | Budget invariant held across N chaos runs | ⚠️ | Held in every scenario that ran — but **only 5 of 12 chaos scenarios ran**. CH-2, CH-5, CH-6, CH-7, CH-9, CH-11, CH-12 are all "not run — deferred." Say "across the five scenarios we ran," not "under failure" |
| N-3 | Revocation propagation p99 | ✅ | NFR-4 test exists against 3 real PEP instances |

---

## 4. NFR compliance

| NFR | Target | Verdict | Evidence |
|---|---|---|---|
| NFR-1 | Decision p99 < 1 ms | ✅ | 430.4 µs. **Note the correction**: an earlier published ~5 µs used `FakePolicy` and excluded the most expensive step. The repo superseded it itself |
| NFR-2 | PEP overhead p99 < 8 ms @ 500 RPS | ❌ | **Not demonstrated, and the repo says so.** 100 RPS enforcement p99 ranged **1.753–74.724 ms** across 3 runs — straddling the budget ~10× both ways. 500 RPS unofferable: the *stub upstream alone* managed 138 RPS at p50 335 ms. Needs the generator off-box |
| NFR-3 | `Σ spend ≤ mandate` under all tested concurrency/partition | ⚠️ | Holds in all scenarios run; see N-2 on coverage |
| NFR-4 | Revocation < 2 s p99 to all PEPs | ✅ | `test_revocation_nfr4.py` |
| NFR-5 | Zero plaintext secret in any log line | ✅ | `tests/security/test_secret_scanning.py` — AST walk + `caplog` regex + detector self-tests. **Found a real leak on first run** (`compiler.py` logged the operator's full NL policy at INFO); fixed |
| NFR-6 | Audit chain detects single-record tampering | ✅ | `verify_chain()` + A-30 |
| NFR-7 | Fail closed by default, fail-open per-policy and audited | ⚠️ | Fail-closed ✅. The **per-scope opt-in fail-open** path is claimed in `README.md` "Design commitments"; I found no configuration surface for it. Treat as ❌ until located |
| NFR-8 | Cold start < 90 s | ✅ | Measured 20 s / 19 s; `make up` 23 s |
| NFR-9 | Drift FPR < 5% on benign corpus | ❌ | **Never measured.** T-034 (dataset) and T-035 (classifier) deferred. No model card exists — `generate_evidence_pack.py` states this plainly rather than substituting something |
| NFR-10 | 100% development in Bangladesh; all model weights open-weight and self-hosted | ⚠️ | Development provenance not auditable from the tree (out of scope here). **The second half is contradicted** — see C-8. The compiler's headline number required a hosted API |

---

## 5. Research-grade claims

These are the ones a reviewer will test hardest.

| # | Claim | Verdict | Detail |
|---|---|---|---|
| G-1 | "10 formal invariants (INV-1…INV-10), all property-tested with Hypothesis" (`DEMO.md` §3.3) | ⚠️ | The invariants are stated formally in `docs/specs/03-attenuation.md`. Property tests cover **P-01…P-09, P-17** in `tests/property/test_attenuation.py`. The mapping is not 1:1 — INV-5 (budget subadditivity) is a *ledger* property tested at integration level, not in the property suite. "All ten are property-tested" is not accurate; "all ten are stated formally and each has named test coverage" is |
| G-2 | "The lease protocol has a written safety proof" and was **"model-checked"** | ⚠️ | The **proof is real and good** — potential function Φ = committed + leased, `docs/specs/04-lease-protocol.md` §6. But **"model-checked" is the wrong word**: §15 is **400 random interleavings** with per-guard ablation, not exhaustive state-space exploration. There is no TLA+/Spin artifact, and **the model script is not in the repository** — I searched. So the ablation table is *not reproducible by a reader*. Two fixes, both cheap: reword to "hand-proved and validated by randomized differential testing with guard ablation," and commit the model |
| G-3 | Guard ablation shows each guard is load-bearing | ✅ (as a finding) ⚠️ (as evidence) | The results are the most scientifically valuable thing in the repo — G1 removed → 160 granted against 100; G3 removed → `leased` negative in 55/400. Undermined only by G-2's missing artifact |
| G-4 | "Partition behaviour: CP choice, liveness bound" | ⚠️ | Declared in spec 04 §8. The liveness bound was **false in the deployed system** until recently (nothing scheduled `reap()`). **Now closed** — [`app.py:175` `_reaper_lifespan()`](../packages/agentiam-controlplane/src/agentiam_controlplane/app.py#L175), wired at [`app.py:969`](../packages/agentiam-controlplane/src/agentiam_controlplane/app.py#L969), default 15 s = TTL/4. Verified by grep: no other production caller exists |
| G-5 | NL→policy compiler "90%" | ⚠️ | Real and honestly earned (0% → 43% local → 90%), and the instrument is self-validating (`--validate` scores reference policies with no model, must hit 30/30). Caveats to state: **n = 30**, hosted `gemini-flash-lite-latest` resolving to `gemini-3.5-flash-lite`, and the related-work survey notes LACE/NLAC/U-XACML are evaluated on larger corpora |
| G-6 | "22 red-team attacks, all rejected" | ⚠️ | 32 unit + 4 integration tests, real and including two novel findings (TM-19, TM-20). **But this is not the field's currency** — attack-success-rate on a shared benchmark is, and AgentIAM has no number on one. **Now fixable**: `APort Vault` (arXiv:2609.22076) is public and released (`huggingface.co/datasets/aporthq/vault-benchmark-v1`), replays 4,371 human-written CTF attacks against agent *payment* authorization, and **explicitly does not test spend caps** — AgentIAM's exact contribution. `MasDrift` (arXiv:2608.07556) already baselines "carrying an attenuated policy along the delegation chain." See [`related-work.md`](related-work.md) §4.4 |
| G-7 | Drift detection as a contribution | ❌ | Cut it. Rule-based v0, half the features deferred, no dataset, no classifier, FPR unmeasured |
| G-8 | Attenuation as contribution #1 (`PLAN.md` §18.1) | ❌ | **Pre-empted.** AIP (arXiv:2603.24775, IETF `draft-prakash-aip-00`) uses the same library, same technique, same transport. See `related-work.md` §4.1 |
| G-9 | Quantitative mandates as the novel gap | ⚠️ | **Narrowed since the August survey.** Agent Contracts (arXiv:2601.08815, COINE 2026 @ AAMAS) has multi-dimensional constraints and conservation laws under delegation; Token Budgets (arXiv:2606.04056) has no-double-spend under delegation fanout. Both predate that survey and were missed. What survives is the **distributed** case — see `related-work.md` §5.1 |
| G-10 | Empirical evaluation as a contribution | ⚠️ | Substantial and unusually honest, but **every number is AgentIAM against AgentIAM**. No baselines anywhere. **Now the cheapest blocker to clear** — two public in-domain harnesses exist (G-6) |
| G-12 | "Safety does not depend on PEP correctness" (spec 04 §6) | ⚠️ | **True of the protocol, not of the deployment.** `scripts/pep_service.py` holds `AGENTIAM_PEP_DATABASE_URL` and opens Postgres sessions directly ([`pep_service.py:494`](../scripts/pep_service.py#L494)); the only production `LedgerClient` is that direct-DB one, so trust boundary B2 is a library call. `ck_budgets_invariant` still stops arbitrary SQL from breaking the invariant's shape — but **`total` is a mutable column**. Holds against a PEP that lies about amounts (the realistic threat); not against arbitrary code execution. Fix: restricted DB role. See [`lease-plan.md`](lease-plan.md) GAP-A |
| G-13 | Clock-skew safety (spec 04 §9, TM-22) | ⚠️ | **An unchecked assumption.** `expires_at` is computed from the **PEP's** clock ([`pep_service.py:502`](../scripts/pep_service.py#L502)); the reaper compares against the **control plane's** ([`app.py:209`](../packages/agentiam-controlplane/src/agentiam_controlplane/app.py#L209)). `SKEW_ALLOWANCE` appears only in `reap()`'s cutoff — **nothing verifies a PEP's clock**. Safety needs Δ < 2S (10 s on defaults); beyond it the same budget is issued twice. Spec 04 §17 **open question 1 is still open**, owned by a ticket that shipped long ago, and CH-7 — cited as TM-22's coverage — never ran |
| G-11 | Chain of custody as a contribution | ⚠️ | **HDP** (arXiv:2604.04522, Apr 2026) records each delegation hop as an **Ed25519-signed** entry in an append-only chain, offline-verifiable from the issuer's public key. AgentIAM's chain is hashed but **unsigned and unanchored**. Either sign it, or drop custody from the contribution list and cite HDP — do not claim non-repudiation beside it |

---

## 6. Coverage claims that cite tests which did not run

Found by cross-referencing `docs/threat-model.md`'s Tests column against
`docs/benchmarks/chaos-results.md`. This is the same defect class as TM-27 (a check nothing
evaluates) applied to *evidence*.

| Threat | Cited coverage | Reality |
|---|---|---|
| **TM-22** clock skew | "CH-7, EC-T08 (PEP side)" | **CH-7 never ran** — "not run — deferred," and no `test_ch07*` file exists. The mechanism *is* covered at unit/integration level both sides (`test_pep_pool.py:324`, `:342`; `test_ledger.py:233`, `:260`), so the guard is tested — but **the end-to-end skewed-PEP-against-real-ledger scenario was never exercised**. Reword the citation |
| **TM-08** revocation lag | "A-29, CH-2, …" | **CH-2 never ran** (Redis kill). A-29 and the NFR-4 test do cover it; drop the CH-2 citation |
| **TM-14** control-plane DoS | "…CH-1, CH-11, NFR-7" | **CH-11 never ran** (connection-pool exhaustion) |

None of these means the property is false. All three mean a **citation is stronger than its
evidence**, which is exactly what a reviewer checks first.

---

## 7. Fix list, ordered by value per hour

1. **Reword "model-checked" → "hand-proved + randomized differential testing with guard
   ablation," and commit the model script** (G-2). Highest risk of a reviewer calling it
   overclaiming; cheapest to fix.
2. **Fix `README.md` C-8 and C-14.** The OSS/no-black-box claim is false as operated, and the
   status line understates the project by ten milestones. Both are the first things an outside
   reader sees.
3. **Rename "non-repudiation" → "tamper-evidence"** throughout (`threat-model.md` §5, P6). The
   chain is unsigned and unanchored; at A5 it is recomputable.
4. **Correct the three stale coverage citations** (§6).
5. **Run APort Vault** (G-6). Public, released, adversarial, in-domain, and it leaves the
   money dimension open. Converts the largest review blocker into a results table. Do this
   before writing prose.
6. **Read AIP in full** before writing any paper (G-8). Agent Contracts is already read: it is
   single-process, so the distributed claim survives (G-9).
7. **Automate assumption A1** (biscuit block-fact scoping). One library behaviour holds up the
   entire attenuation design and it is checked by hand. `docs/STATUS.md` §4.1 already proposes
   this.
8. **Get NFR-2 off-box**, or stop citing 500 RPS at all.
9. ~~Recover `docs/research.md` from the git stash~~ — **done**, preserved verbatim at
   `research/prior-survey-2026-08-21.md`. The stash is untouched.

---

## 8. What is genuinely strong

Stated because a gap analysis that lists only gaps misrepresents the project.

- **The budget invariant holds against a compromised enforcement point.** Guards G2/G3, proved
  in spec 04 §6 and ablation-tested. This is unusual, defensible, and under-sold everywhere.
- **The chaos suite found four real product bugs**, including that spent budget never reached
  the ledger — so the headline correctness claim had been false, and the invariant checker
  could not see it because the books were consistent about a number that had stopped
  describing reality. That is a better story than a clean pass.
- **Negative results are reported.** NFR-2's failure, the compiler's 0%, the ~5 µs correction,
  the missing drift model card. The repo corrects its own published numbers. For an academic
  audience this is the most credible thing about it.
- **`agentiam-core` is 100% statement-covered and statically proved I/O-free**, so every
  correctness claim is a claim about a package that cannot touch a network.
- **Two novel findings worth publishing on their own**: the parser differential (TM-26) and the
  library-default-timeout hazard (TM-25) — the latter inherited by anyone building on biscuit,
  AIP included.
