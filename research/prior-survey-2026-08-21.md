# Research Plan — publishing from AgentIAM

**Status:** analysis, not a commitment. Written 2026-08-21, against the tree at `003a06a`
(46/61 tickets, M1–M11 complete).

**What this document is.** An assessment of what in AgentIAM is publishable, what the
surrounding literature already claims, where the genuine gap is, and precisely what must be
built or measured before a submission would survive review. It is written in the same posture
as the rest of this repository: claims are checked against something, negative results are
stated, and the parts that are not yet true are marked as not yet true.

**Method.** The landscape below was mapped with two live scholarly search instruments
(OpenAlex/Semantic Scholar via the Fast Track connector, ~250M records; and Consensus,
~200M records) between 2026-08-21. Every retrieval is a *lead*, not a verdict — §3.6 states
the instruments' measured limits, including one place where they failed outright and the
survey must be done by hand.

---

## Table of contents

1. [The verdict, up front](#1-the-verdict-up-front)
2. [Asset inventory — what actually exists](#2-asset-inventory--what-actually-exists)
3. [The literature landscape, as measured](#3-the-literature-landscape-as-measured)
4. [Positioning: three candidate papers, scored](#4-positioning-three-candidate-papers-scored)
5. [The recommended contribution claim](#5-the-recommended-contribution-claim)
6. [Gap analysis — what is missing for each claim](#6-gap-analysis--what-is-missing-for-each-claim)
7. [The evaluation the paper needs](#7-the-evaluation-the-paper-needs)
8. [Reviewer objections, and the honest answer to each](#8-reviewer-objections-and-the-honest-answer-to-each)
9. [Venues, timing, and the priority problem](#9-venues-timing-and-the-priority-problem)
10. [Work packages](#10-work-packages)
11. [What not to claim](#11-what-not-to-claim)
12. [Artifact and reproducibility plan](#12-artifact-and-reproducibility-plan)
13. [Bibliography](#13-bibliography)

---

## 1. The verdict, up front

**There is a paper here. It is not the paper `PLAN.md` §18.1 describes.**

`PLAN.md` §18.1 lists four contributions in priority order: attenuable delegation,
lease-based enforcement, drift detection, empirical evaluation. Measured against the
literature as it stands in August 2026, that ordering is **exactly backwards on the first
two**, and the third is close to unpublishable in its current state.

| §18.1 claim | Measured status | Action |
|---|---|---|
| 1. Attenuable delegation with quantitative mandates | **Attenuation half is now crowded** — at least six 2026 preprints, one of them (AIP/IBCT) using the *same library, same technique, same transport* | Demote to background. Keep only the quantitative half |
| 2. Lease-based distributed enforcement | **The genuine gap.** No retrieved work enforces multi-dimensional quantitative ceilings across concurrent delegated agents with a safety argument | **Promote to contribution #1** |
| 3. Intent binding + calibrated drift detection | **Not evaluable.** NFR-9 (FPR < 5% on a benign corpus) has never been measured; T-034/T-035 deferred | Cut, or spend six weeks on a dataset |
| 4. Empirical evaluation | **Real, and the strongest asset — but has no baselines.** Every number is AgentIAM measured against AgentIAM | Keep, add baselines (§7) |

**Two findings not in §18.1 at all are among the most publishable things in the repository:**

- **TM-26, the parser differential** (`threat-model.md` §3.1). The PEP authorizes one value
  while the upstream acts on another, because two parsers read every request. Measured across
  Starlette, `parse_qsl`, Go, Java and PHP. *Every* proxy-shaped agent authorization gateway in
  §3.2's list has this hole, and none of them mention it. This is a short, sharp, mean paper.
- **TM-25, the library-default timeout** (`threat-model.md` §3.1). `biscuit-python` 0.4.0
  defaults its authorizer to `max_time = 1 ms` of *wall clock, not work*, so a legitimate
  request is refused for want of CPU scheduling — under exactly the load the 1 ms latency
  budget exists to describe. 2 of 42,014 authorize calls hit it in a real run; 10 of 10
  injected timeouts produced a false invariant-violation report. Anyone building on biscuit
  inherits this. Worth a paragraph in the main paper and a note to upstream.

**The single largest risk is not quality. It is time.** §3.2 shows the competitive field went
from effectively empty to a dozen entrants in about eighteen months, and the closest
competitor is four months old. Every month of delay materially raises the chance of being
scooped on the framing. §9 argues for a preprint before a venue.

---

## 2. Asset inventory — what actually exists

Written down plainly so §6 can be honest about what is missing. Sources: `docs/STATUS.md`,
`docs/benchmarks/`, `docs/evidence/`, and the tree itself.

### 2.1 Specification and formal content

| Artifact | Substance | Publication weight |
|---|---|---|
| `specs/01-token-format.md` | Authority/attenuation block structure, all facts and checks, scaled-integer money encoding, size limits, measured 3-level worked chain | Medium — implementation detail, mostly |
| `specs/02-caveat-language.md` | Nine caveat types, their Datalog compilation, the normative `check if` vs `reject if` rule, reason codes | **High** — §3.1's rule is a real finding (see TM-20) |
| `specs/03-attenuation.md` | `narrows()` partial order, mint-time check, **INV-1…INV-10 stated formally**, 14 counterexamples, property-test mapping | **High** — this is the formal skeleton |
| `specs/04-lease-protocol.md` | 7 ledger operations, lease state machine, **safety argument via a potential function Φ**, liveness bound, CP declaration, partition and clock-skew handling, model-check table | **Highest** — this is the paper |
| `specs/06-drift-detection.md` | Canonicalization, stateless intent assertion, f1/f2/f5, measured feature behaviour | Low-medium — under-evaluated |
| `specs/07`–`10` | Revocation, audit chain, decision record, scope extraction | Medium — §10 carries TM-26 |
| `threat-model.md` | **27 STRIDE threats**, 9 of them found while building rather than brainstormed; 3–4 stated accepted risks with bounds | **High** — the honesty is a feature |
| `DECISIONS.md` | 57 ADRs, each with the cost of the choice stated | Supporting evidence |

### 2.2 Implementation

~15.5 kLOC of source across five packages:

```
agentiam-core           4,081  pure domain logic, zero I/O, statically enforced, 100% stmt coverage
agentiam-pep            4,124  the enforcement point (hot path)
agentiam-controlplane   6,270  issuance, ledger, policy, revocation, audit, escalation, console
agentiam-sdk              828  attenuate(), spend context manager, escalate()
agentiam-demo             161  reference agent + stub tools
```

`agentiam-core`'s I/O-freedom is enforced on every CI run by `tests/unit/test_core_purity.py`.
**Every correctness claim this project makes is a claim about that package** — which is a
clean, checkable statement for a paper to make, and unusual.

### 2.3 Test corpus

~1,500 test functions:

| Suite | Files | Tests | What it buys the paper |
|---|---|---|---|
| unit | 53 | ~1,141 | Coverage, not evidence |
| integration | 26 | ~235 | Real Postgres/Redis: `FOR UPDATE` serialization, 50-concurrent-acquire, dedup races |
| property | 3 | ~42 | **P-10's stateful `hypothesis` machine over acquire/reserve/commit/refund/release/expire/crash/revoke/late-commit** — this is a real safety argument |
| security | 4 | ~63 | 22 named attacks (A-01…A-30 subset) + secret-scanning AST walk |
| chaos | 5 | ~10 | 5 of 12 scenarios + 5 sub-scenarios, invariant sidecar throughout |
| perf | 1 | ~7 | PB-1, PB-2 |
| e2e | 1 | ~13 | Full pipeline |

### 2.4 Published measurements

**NFR-1 — decision latency, in process, real Cedar and real biscuit:**

| Step | median µs | p99 µs |
|---|---|---|
| extract | 19.8 | 58.3 |
| verify (biscuit signature) | 221.2 | 417.0 |
| caveats (4 clauses, Datalog) | 3.6 | 9.8 |
| policy (Cedar) | 135.3 | 307.3 |
| audit record hash | 16.0 | 58.4 |
| **`decide()` total** | **151.3** | **350.0** |

Two things a single number cannot say, both already written down: policy evaluation is nearly
the whole decision, and `verify()` — the most expensive step — sits *outside* `decide()`, so
per-request in-process cost is ~392 µs, and that is what bounds throughput.

> A prior published figure of ~5 µs was a real measurement of `decide()` against a stub
> policy, and was superseded. That the project caught and corrected its own headline number is
> itself worth one sentence in the paper's evaluation section.

**NFR-2 — proxy overhead under load. Not established, and the paper must say so.**
Three tiers (bare upstream / proxy-no-pipeline / enforcing PEP), three runs each. At 100 RPS
the enforcement p99 ranged **1.753–74.724 ms** across three runs against an 8 ms budget —
straddling it tenfold in each direction. At 500 RPS the *stub upstream alone*, with no PEP in
the path, managed 138.4 RPS at p50 335 ms; those rows are queueing artefacts, not service
times. **The generator, three uvicorn processes and Postgres share one development machine.**

**Chaos — 5 of 12 scenarios, plus 5 sub-scenarios, all `held`, 3m05s.** The invariant sidecar
records three outcomes, not two: *held*, *violated*, and *unavailable* — the last being an
honest sweep that could not run because Postgres was genuinely stopped. Folding "unavailable"
into "held" would report a green run for a database that was not there. **This distinction is
a small methodological contribution and should be named as one.**

**Guard ablation (from a protocol model executed before spec 04 was written):**

| Scenario, 400 random interleavings | Result |
|---|---|
| Protocol as specified | invariant holds |
| G1 removed (stale `available` read) | overspend: `leased` 160 vs `total` 100 |
| G2 removed (trust the PEP's reported `actual`) | `leased` negative, 12/400 |
| G3 removed (accept late commits) | `leased` negative, 55/400 |
| G4 removed (replay) | pool safe; `committed` overstated 3× |
| Skew margin removed | budget re-issued while a lagging PEP still holds it |

**This table is the most persuasive single artifact in the repository**, and §6.4 explains why
it is currently not usable as published.

**NL→Cedar compiler: 27/30 (90%)**, `passed 27 · wrong 1 · unparseable 2 · clarification 0 ·
errors 0`, median 2.6 s, on `gemini-flash-lite-latest` resolving to `gemini-3.5-flash-lite`
(logged per call, because an alias is otherwise unattributable). The journey — **0% → 43%
(local, and inflated) → 90%** — came almost entirely from fixing the *instrument*, not the
model. §3.4 explains why this is a good methodology story and a weak results story.

### 2.5 What is deployed and reproducible

One container image with three entrypoints; `docker-compose.demo.yml` reaching all-healthy in
**20 s / 19 s** across two cold runs against a 90 s budget; `deploy/k3s/` manifests
live-deployed to a real cluster; a tag-triggered release workflow doing GHCR push, keyless
cosign signing and SBOM attestation, self-verified in the same job; a CycloneDX 1.5 SBOM of
136 components with a byte-exact CI freshness check; and `docs/evidence/evidence-pack.html`,
a single self-contained ~90 KB file with no network reference of any kind.

**For artifact evaluation this is already close to top-decile.** §12.

---

## 3. The literature landscape, as measured

### 3.1 How the searches were run

Six sweeps on the Fast Track connector (OpenAlex/S2, ~250M records) and one on Consensus
(~200M), plus one Nearest-Neighbour/Duplication Test per contribution claim and one
gap-saturation curve. Queries, and what each returned, are recorded here so the negative
results are auditable.

| Query framing | Instrument | Outcome |
|---|---|---|
| capability-based delegation + least privilege for LLM agents/sub-agents | Fast Track | **106 hits, densely on-topic** — the crowded field |
| macaroons / attenuable bearer credentials / offline verification | Fast Track | **1 irrelevant hit** — instrument failure, see §3.6 |
| MCP security / agentic IAM | Fast Track | 15,846 hits, mostly off-target (lexical drift) |
| Duplication Test: quantitative spend budgets across concurrent delegated sub-agents | Fast Track | **Pure noise** — blockchain-fog, GPU scheduling, Rwandan fiscal policy |
| Duplication Test: offline holder-side attenuation with monotonicity invariants | Fast Track | **8 direct near-neighbours** — the competitive set |
| lease-based distributed quota under partition, safety/liveness | Fast Track | **Off-field** — instrument failure, see §3.6 |
| agentic-commerce spend limits / financial guardrails | Fast Track | 52 hits; **one directly relevant SoK** |
| goal-drift / intent-alignment monitoring | Fast Track | 486 hits, overwhelmingly surveys and taxonomy papers |
| LLM generation of access-control policies | Consensus | **20 hits, all on-target, all better-evaluated than ours** |
| AgentDojo / indirect prompt injection defenses | Fast Track | 174 hits — a saturated, benchmark-disciplined subfield |
| gap saturation: quantitative budgets across agent hierarchies | Fast Track | 5 strict / ~9,721 broad — **instrument disagreement flagged by the tool itself** |

### 3.2 The crowded lane: agent authorization and prompt-injection defense

**Do not enter this lane.** It is saturated, benchmark-disciplined, and moving monthly.

Retrieved defenses, all evaluated on AgentDojo, ASB, AgentDyn or AgentLeak:
[Progent](https://doi.org/10.48550/arxiv.2504.11703) (privilege control; LLM-generated policy
narrowed under SMT-checked *monotonic confinement* — an expansion needs explicit approval),
[CaMeL / Fides](https://doi.org/10.48550/arxiv.2505.23643) (information-flow control with a
formal planner model), [AuthGraph](https://openalex.org/W7162699945) (dual-graph provenance vs
authorization alignment; ASR 40%→1% on GPT-4o),
[SecureClaw](https://openalex.org/W7164233523) (dual-boundary, PREVIEW→COMMIT, 0% ASR on ASB),
[APPA](https://openalex.org/W7171748979) (taint confinement with a two-monoid algebra and
proved parent-label preservation),
[DRIFT](https://doi.org/10.52202/085713-2791),
[IPIGuard](https://doi.org/10.18653/v1/2025.emnlp-main.53),
[Task Shield](https://doi.org/10.18653/v1/2025.acl-long.1435),
[RTBAS](https://doi.org/10.48550/arxiv.2502.08966).

Two structural observations matter more than the individual papers:

1. **This subfield has a currency, and it is attack-success-rate on a shared benchmark.**
   [AgentDojo](https://doi.org/10.48550/arxiv.2406.13352),
   [ASB](https://doi.org/10.48550/arxiv.2410.02644),
   [TAMAS](https://doi.org/10.18653/v1/2026.acl-long.1442),
   [AgentLeak](https://doi.org/10.1109/access.2026.3704541). A defense paper without a number
   on one of these does not get read. AgentIAM's 22 hand-written attacks are not that number.

2. **Every one of these defenses operates above the byte layer** — on plans, tool calls,
   arguments-as-parsed, information-flow labels. **TM-26 is beneath all of them.** A defense
   that reasons about `amount = 1` while the upstream executes `amount = 999999` is decorative,
   and the decision record it emits is *honest about what it saw*. This is the sharpest thing
   AgentIAM knows that this literature does not.

### 3.3 The near lane: capability tokens and delegation for agents

The Duplication Test on holder-side attenuation returned a real competitive set. Ranked by
threat to AgentIAM's framing:

**1. [AIP: Agent Identity Protocol for Verifiable Delegation Across MCP and A2A](https://openalex.org/W7142557907)
(Prakash, 2026, arXiv 2603.24775) — the direct collision.** Invocation-Bound Capability Tokens
fusing identity, attenuated authorization and provenance; **chained mode is a Biscuit token
with Datalog policies for multi-hop delegation** — the same library and the same technique as
spec 01; two wire formats; Python *and* Rust reference implementations with cross-language
interop; verification at 0.049 ms (Rust) / 0.189 ms (Python); 0.22 ms overhead in a real
MCP-over-HTTP deployment; 2.35 ms in a live Gemini 2.5 Flash multi-agent deployment;
**600 adversarial attempts at 100% rejection**, with delegation-depth violation and
audit-evasion uniquely caught by chained delegation.

> **Read this paper before writing a word.** It pre-empts the attenuation contribution, the
> depth-bound contribution, the offline-verification contribution and the
> adversarial-evaluation framing. What it does **not** appear to have: any quantitative
> resource dimension, any concurrency or partition analysis, any ledger, and any measured
> failure mode. That is the whole of the remaining gap, and it is exactly §5's claim.

**2. [CapChain](https://doi.org/10.3390/app16157776) (Applied Sciences, 2026).** Capability
tokens binding field-level read access in LangGraph-style shared state, plus a tamper-evident
provenance log with logarithmic-cost audit. Overlaps the audit-chain contribution. Different
enforcement locus (state-transition layer, not tool boundary).

**3. [Authorization Propagation in Multi-Agent AI Systems](https://openalex.org/W7160727562)
(Tallam, 2026).** Formalizes *authorization propagation* as a workflow-level property with
three sub-problems (transitive delegation, aggregation inference, temporal validity) and seven
structural requirements. **Critically for §5, it surveys the state of the art on quantitative
limits and names exactly one mechanism: "execution-count revocation."** A count. Not money,
not multiple dimensions, not under concurrency, not under partition. This is the single best
external corroboration that AgentIAM's gap is real — and it is a citation, not a competitor.

**4. [Before the Tool Call: Deterministic Pre-Action Authorization](https://openalex.org/W7140345986)
(OAP, 2026).** Open Agent Passport: synchronous pre-execution interception, declarative policy,
cryptographically signed audit record. **Median 53 ms (N=1,000)** — 150× AgentIAM's in-process
decision, which is a *favourable* comparison worth making explicitly. Live adversarial testbed:
74.6% social-engineering success under a permissive policy, **0% across 879 attempts under a
restrictive one**. Explicitly names "spending limits, capability scoping" as things the
infrastructure enforces — but reports no budget experiment.

**5. [Delegation Without Escalation](https://doi.org/10.5281/zenodo.20242392) (Hasbini, 2026).**
Practitioner paper; states the composed-chain problem ("each hop correct in isolation, the
composed chain authorizes what no hop intended") in almost exactly INV-2's terms. Cite as
motivation.

**6. [Digital Identity for Agentic Systems](https://openalex.org/W7161204566) (Madhira, 2026).**
Typed constraint algebra, delegation attenuation, fail-closed processing, portable across
JWT/VC/RAR profiles. Overlaps `narrows()`.

**7. [SUDP: Secret-Use Delegation Protocol](https://openalex.org/W7159547013) (2026).**
Formalizes the Agent Secret Use problem with seven required security properties. Different
problem (custodial secret use), same intellectual shape as INV-1…INV-10 — a useful model for
how to present an invariant set.

**8. [OAuth Is Not Enough](https://doi.org/10.36227/techrxiv.174952577.74018032/v1) (Chopra,
2025).** Coarse scopes, no dynamic policy, no multi-hop delegation. This is `README.md`'s
"Why" section, already published. **Cite it rather than re-arguing it** — it saves half a page
and signals field awareness.

Also relevant: [Do Coding Agents Understand Least-Privilege Authorization?](https://openalex.org/W7161452818)
(AuthBench, 2026), [The Authorization-Execution Gap](https://openalex.org/W7161204744)
(position paper — its "delegation-level incompleteness / channel-level corruption /
composition-level fragmentation" taxonomy is a *very* good frame for AgentIAM's contributions,
and TM-26 is a textbook channel-level corruption), and
[Open Challenges in Multi-Agent Security](https://doi.org/10.48550/arxiv.2505.02077)
(24-author agenda paper — good for the introduction's "the field says this is open" citation).

### 3.4 The compiler lane: NL → policy

**This is the weakest of AgentIAM's four claimed contributions and should be cut to a
subsection.** The retrieved work is more mature, better evaluated, and on larger corpora:

- [LACE](https://consensus.app/papers/details/120d2023b2d659db86142d02bc7bfc0c/?utm_source=claude_desktop) [1] —
  prompt-guided generation + retrieval-augmented reasoning + **formal validation**, a hybrid
  LLM-rule engine with deterministic enforcement, 88% decision accuracy / 0.79 F1, ablations
  *and* adversarial robustness evaluation. This is architecturally the same idea as T-029/T-030
  with a better evaluation.
- [U-XACML on-device derivation](https://consensus.app/papers/details/4a34a9468c1d5c289979197e19583679/?utm_source=claude_desktop) [4] —
  93% policy-generation accuracy, 91% on ambiguous/noisy input, 98% agreement with
  expert-defined policies, taxonomy-driven dataset across three domains.
- [NLAC + NLACBench](https://consensus.app/papers/details/e0f554ad376352a2a7fe03129933866d/?utm_source=claude_desktop) [10] —
  up to 96.9% small-network, degrading below 20% at scale, then **recovered to 98.7%** via
  embedding-similarity subgraph construction. Ships a benchmark.
- [Synthesizing Access Control Policies Using LLMs](https://consensus.app/papers/details/c2f0740d02ef5f81ae45a33fca92ea96/?utm_source=claude_desktop) [2],
  [Translating NL Specifications into Access Control Policies](https://consensus.app/papers/details/dc3307e97840557183b3197c9d133c2e/?utm_source=claude_desktop) [3],
  [Plain English to XACML](https://consensus.app/papers/details/ac2e36a80b055879be944d452b08a2d7/?utm_source=claude_desktop) [7],
  [Policy-Aware Generative AI](https://consensus.app/papers/details/e1ca6959c44d58f0aa4de3420a16256b/?utm_source=claude_desktop) [5],
  [ABAC policy mining with LLMs](https://consensus.app/papers/details/92da122efcaa5df58aaf7130d4693872/?utm_source=claude_desktop) [6].
- Evaluation-side: [ACBench](https://consensus.app/papers/details/c7f9ad819be65482899725a516c68b86/?utm_source=claude_desktop) [8]
  (adversarial RBAC compliance, reporting ACC / ASR / **over-refusal rate**) and
  [Role-Conditioned Refusals](https://consensus.app/papers/details/8e7bb885c5545e8bbc5d50e9808d2d4e/?utm_source=claude_desktop) [9]
  (generator-verifier vs LoRA fine-tuning; **explicit verification improves refusal precision
  and lowers false permits** — the same finding T-030's verify-before-deploy loop embodies).

**27/30 on 30 cases is not a competitive result against these.** The corpus is an order of
magnitude too small and there is no external baseline.

**But there is one thing here worth keeping, and it is a methodology finding, not a score.**
Dataset v1 could not be passed by *any* compiler: of 30 cases the positive principal id
appeared verbatim in the English in only 10, 13 required guessing an arbitrary suffix, 7 named
an id never mentioned — and the harness passed `entities=[]`, so `principal.role` and ownership
did not exist at evaluation time. The ceiling for any compiler was ≤10/30, and reaching it
required emitting a policy about one named person, which is the *wrong* generalization. Two
published figures (43% local, 63% hosted) were additionally inflated by three dataset prompts
having been pasted into the system prompt as few-shot examples.

The fix — `evaluate_compiler.py --validate` scores every case's reference policy **with no
model at all** and must hit 30/30 before a run is worth starting — is a cheap, general,
reusable check that **none of [1]–[10] above report performing.** Frame it that way:

> *"A natural-language-to-policy benchmark should be required to demonstrate that a perfect
> policy-writer can score 100% on it. We show a corpus that could not be passed by any
> compiler, and a one-line harness change that detects the condition."*

And keep `test_the_prompt_does_not_contain_any_evaluation_prompt` as a *test* rather than a
note, with the stated reason: the quickest way to fix a failing case is to show the model that
case, and the temptation recurs every time a case fails. That is a contamination-control
practice worth naming.

### 3.5 The drift lane

The `goal drift detection` sweep returned 486 records that were overwhelmingly surveys and
taxonomies, not detectors — [AI Agents vs. Agentic AI](https://doi.org/10.1016/j.inffus.2025.103599),
[The Rise of Agentic AI](https://doi.org/10.3390/fi17090404),
[TRiSM for Agentic AI](https://doi.org/10.48550/arxiv.2506.04133),
[Towards trustworthy agentic AI](https://doi.org/10.20935/acadai8260). The one quantitative
signal worth quoting is from the healthcare taxonomy
([Vatsal et al., IEEE Access 2026](https://doi.org/10.1109/access.2026.3651218)): across 49
studies, **"Drift Detection & Mitigation" was absent in ~98%** of them. So the *gap* is real
and citable.

**AgentIAM cannot fill it in its current state**, and the reason is written down in this repo:
NFR-9 requires a false-positive rate below 5% on a benign corpus at the chosen threshold;
T-034 (2,000+ labelled pairs) and T-035 (the calibrated classifier) are both deferred; nothing
consumes a feature vector. A detector with a hand-set 0.7 threshold and no FPR is not a result.

**There is, however, a genuine and quotable negative result** in `specs/06` §5.1 that is worth
more than a weak positive one:

> **Embeddings are near-blind to numeric magnitude.** Inflating a payment 211× (45,000 →
> 9,500,000 BDT) moved the f2 cosine feature by **0.0102** — inside the noise between two
> correctly-aligned cases. **No embedding feature can catch an amount attack.** f1 is
> additionally non-monotonic in alignment: a *related read* scored 0.5412 against the correctly
> aligned payment's 0.4834.

That is a clean, reproducible, falsifiable claim about a technique other people are proposing,
and it directly motivates why f5 (symbolic argument-entity overlap, numeric comparison by
value so `45000`, `45,000` and `45000.0000` are one entity) exists. **Publish the negative
result; do not publish a detector.**

### 3.6 Where the instruments failed — read this before trusting §3's negatives

Three retrievals returned off-field noise, and honesty requires distinguishing *"no literature
exists"* from *"the lexical engine could not find it."*

1. **`macaroons attenuable bearer credentials…` returned one irrelevant 2016 EU deliverable.**
   Macaroons (Birgisson et al., NDSS 2014) is a well-cited paper. **This is an instrument
   failure, not a gap.** The engine is lexical and these are compound technical phrases.
2. **`capability systems resource accounting revocation confinement…` returned VPLS surveys,
   prison architecture and MINIX.** The classical capability literature — Dennis & Van Horn
   (1966), Levy's *Capability-Based Computer Systems*, EROS, KeyKOS, SPKI/SDSI, Miller's
   object-capability model — did not retrieve at all. Only
   [COAST/CURLs](https://openalex.org/W2572280356) surfaced, and only incidentally.
3. **The gap-saturation curve on quantitative budgets self-reported an
   `instrument_disagreement`:** 5 strict-concept matches vs ~9,721 broad matches, with the
   closest recent hits being *Indonesian education budgeting* and *mental-health parity
   enforcement*. The tool's own note says a thin count can be a vocabulary artefact.

**Consequence — a required work item.** §10's RP-0. The related-work section on classical
capability systems and quantitative resource control **must be built by hand**, from ACM DL
and Google Scholar, not from these sweeps. Specifically: whether any capability system ever
addressed *quantitative resources shared across siblings*. `specs/03` INV-5 asserts that
"most capability-token literature does not address" this, and `specs/04` §13 calls it "the
publishable observation." **That assertion is currently unsourced and these instruments cannot
source it.** It is the load-bearing novelty claim of the whole paper and it needs a real
survey behind it — including the resource-accounting angle in Amoeba, the space-bank in
KeyKOS/EROS, and disk/CPU quota delegation in classical systems.

**However**, four independent on-field signals converge on the gap being real:

- The Duplication Test on the budget question returned nothing on-topic.
- [Authorization Propagation](https://openalex.org/W7160727562) names exactly one quantitative
  mechanism in the state of the art: execution-count revocation.
- [SoK: Security of Autonomous LLM Agents in Agentic Commerce](https://openalex.org/W7155244781)
  (2026) — covering ERC-8004, AP2, x402, ACP, ERC-8183, MPP — proposes a layered defense
  "addressing **authorization gaps left by current agent-payment protocols**." Named, open.
- [OAP](https://openalex.org/W7140345986) lists spending limits as something its infrastructure
  *can* enforce, and reports no experiment for it.

Four converging signals is enough to proceed. It is not enough to write "we are the first."
See §11.

---

## 4. Positioning: three candidate papers, scored

### Option A — "AgentIAM: identity and authorization for AI agents" (the system paper)

The framing `PLAN.md` §18.1 assumes.

| | |
|---|---|
| **Strength** | Uses everything built. Natural for the BIIN technical report |
| **Fatal weakness** | Collides head-on with [AIP](https://openalex.org/W7142557907), [OAP](https://openalex.org/W7140345986), [Digital Identity for Agentic Systems](https://openalex.org/W7161204566) and [CapChain](https://doi.org/10.3390/app16157776). Reviewer question: *"what does this do that AIP does not?"* — and the honest answer is only ~30% of the system |
| **Verdict** | **Do not submit this to a security venue.** Keep it as the BIIN technical report and the arXiv system description |

### Option B — "Quantitative mandates: lease-based enforcement of spend ceilings for delegated agents"

| | |
|---|---|
| **Strength** | Sits precisely in the measured hole. The formal content (Φ, INV-5, G1–G4, the skew argument) is genuinely a protocol contribution. The guard-ablation table is a *falsification* result, which is rare and persuasive. Corroborated as open by two independent 2026 papers |
| **Weakness** | Needs the ablation re-run against the *implementation*; needs a baseline; needs the reap() liveness bug fixed (§6.5); needs numbers from more than one machine |
| **Novelty risk** | Lowest of the three. Nothing retrieved comes close |
| **Verdict** | **This is the paper.** §5 |

### Option C — "The authorization-execution gap at the byte layer" (the TM-26 paper)

| | |
|---|---|
| **Strength** | Short, sharp, and it invalidates a *property* of an entire class of shipped systems. Slots directly into [The Authorization-Execution Gap](https://openalex.org/W7161204744)'s "channel-level corruption" category, which that position paper names but does not instantiate. Already measured across five parsers |
| **Weakness** | Thin for a full paper on its own. Needs a survey: take the retrieved gateways (OAP, AIP, the AgentDojo defenses) and *demonstrate* the differential against each |
| **Verdict** | **Write this second**, or fold it into Option B as its sharpest section. Excellent workshop paper. Would also make a responsible-disclosure story if any retrieved system is affected |

---

## 5. The recommended contribution claim

> ### Quantitative Mandates for Delegated Autonomous Agents
> ### Lease-Based Enforcement of Spend Ceilings under Concurrency and Partition

**The thesis, in one paragraph the abstract can carry:**

Capability-based delegation gives an agent a strictly narrower *set* of permissions than its
parent, offline and without an issuer round-trip. It does not give it a strictly smaller
*quantity*. A parent may hand the same ৳50,000 ceiling to three children; each token is
individually valid and correctly authorized, and together they can spend ৳150,000. No amount
of static token inspection prevents this, because nothing about any token is wrong. We name
this the **sibling budget problem**, show that it is not solvable in the token layer, and give
a lease protocol that solves it in a ledger — with a safety argument, a liveness bound, an
explicit CP choice, and a measured demonstration that each of its four guards is load-bearing.

**Four contributions, in the order they should appear:**

**C1. The sibling budget problem, stated and separated.** INV-1…INV-4 and INV-6…INV-10 are
enforceable in the token; **INV-5 is not, and that is a structural property of capability
tokens, not a design mistake.** Two mitigations, both implemented: *proportional split*
(static, at mint time, predictable, wasteful when one child needs more than its share) and
*shared pool* (dynamic, via leases, the default because it is what real workflows want).
`committed + leased + allocated ≤ total` — with `allocated` a *separate* column because
`leased` must keep meaning "the outstanding total of active leases" for the invariant checker
to work at all. That the third term is necessary, and why overloading the second breaks the
check the moment a split happens, is exactly the kind of detail a protocol paper exists to
carry.

**C2. The lease protocol, with safety and liveness proved and the guards falsified.** Seven
operations; safety by the potential function `Φ = committed + leased`, bounded above by
`total` because only `ACQUIRE` raises Φ and it raises it by at most `total − Φ` under
`FOR UPDATE`; **safety does not depend on PEP correctness** — a compromised PEP reporting an
inflated `actual` is clamped by G2, which matters because the PEP is the component most
exposed to a compromised agent. Liveness: worst-case reclaim `ttl + S + ttl/4` = 80 s at the
defaults. The skew rule (`expire early at expires_at − S`, `reclaim late at expires_at + S`,
`ttl > 2S` strictly) with the measured failure at `S = 0`. And the ablation table.

**C3. Two spec bugs found by probing our own specification.** This is the section that buys
the paper its credibility, and almost nobody writes it. Spec 04's `ACQUIRE` formula **could not
pass its own ticket's acceptance test** (ADR-015). Spec 04's `LEDGER_COMMIT` statement order
**was a TOCTOU race** (ADR-017). Both were written before any code, both looked correct, both
were wrong against a running model. *Writing the spec first is what makes a project
defensible; it does not make the spec right.* Two further findings in the same vein: TM-21
(late commit against a reclaimed lease drove `leased` negative in 55 of 400 interleavings) and
TM-25 (a library's 1 ms wall-clock authorizer default turning CPU contention into false
denials, and 10 of 10 injected timeouts into false invariant-violation reports).

**C4. Measured behaviour under real failure, with an honest availability third state.** The
invariant sidecar's *held / violated / **unavailable*** distinction, and what folding the third
into the first would have reported. Plus the four **product bugs the chaos suite found, none
of which any invariant checker could see** — most importantly that `ledger_commit` had no
production caller at all, so spent budget never reached the ledger, `RELEASE` returned it to
the pool, and the same money was spendable twice. *The books were perfectly consistent about a
number that had stopped describing reality.* That sentence is the paper's best argument for
why chaos engineering belongs in a security protocol evaluation.

**Attenuation, drift, the NL compiler, the console and the audit chain are all background**,
compressed into one "system context" section with forward references to [AIP](https://openalex.org/W7142557907),
[Progent](https://doi.org/10.48550/arxiv.2504.11703) and [1]–[10].

---

## 6. Gap analysis — what is missing for each claim

Ordered by how badly each one would hurt in review.

### 6.1 🔴 BLOCKER — no baselines anywhere

**Every number in `docs/benchmarks/` is AgentIAM measured against AgentIAM.** There is not a
single comparison against an alternative approach in the repository. A security or systems
reviewer will stop reading at the evaluation section.

**Needed, minimum:**

| Baseline | Why | Effort |
|---|---|---|
| **No enforcement** | Establishes the cost floor. Partially exists — T-018 transport mode is tier ② of the NFR-2 table | Low |
| **OAuth-scope-only** (static JWT, coarse scopes, no budget) | The thing the paper says is insufficient. Show it *permitting the sibling overspend*, quantitatively | Medium |
| **Centralized budget check** (every call round-trips to the ledger, no lease) | The obvious alternative design. **Show the latency and the partition behaviour.** This is the comparison that justifies leases existing | Medium |
| **[OAP](https://openalex.org/W7140345986)'s 53 ms median** | Published, directly comparable, and favourable at 150× | **Zero — it is a citation** |
| **[AIP](https://openalex.org/W7142557907)'s 0.189 ms Python verify** | Directly comparable to our 221 µs `verify()`. **Nearly identical — say so.** Convergent measurement from an independent team is *evidence*, not a threat | **Zero** |
| Cedar vs OPA (PB-6, specified, never run) | Specified in `PLAN.md` §13.1. `PolicyEngine` protocol already exists | Medium — deferred as T-024 |

> The centralized-budget baseline is the single highest-value experiment in this document. It
> is the *reason the protocol exists*, and right now nothing measures it.

### 6.2 🔴 BLOCKER — NFR-2 is not established, and throughput is unquotable

Enforcement p99 ranging **1.753–74.724 ms across three runs** at 100 RPS cannot support any
claim in either direction. 500 RPS is unofferable on the development host — the stub upstream
alone managed 138.4 RPS at p50 335 ms. `performance.md` already says this plainly, which is
good practice and will still not survive review as a *result*.

**Fix:** move the load generator off-box. Two machines on a LAN, or one cloud instance pair.
The measurement discipline is already right (HDR histograms, coordinated-omission-aware
driver, no averages, ≥3 runs with variance reported, three tiers so each subtraction compares
like with like) — **the discipline is a contribution and the hardware is the only thing
missing.**

Secondary: `generator_lag_ms` in `nfr2-load.json` already shows the harness itself stalling.
Report it. A paper that shows its own generator's lag series is more trustworthy than one that
does not.

### 6.3 🔴 BLOCKER — the formal argument is prose, not machine-checked

INV-1…INV-10 are English with mechanisms and counterexamples. The Φ argument is a five-row
table. For a workshop this is adequate. For USENIX Security / CCS / NDSS it is thin — the
neighbours are already ahead: [APPA](https://openalex.org/W7171748979) *proves* parent-label
preservation and merge confinement over a two-monoid model; [Fides](https://doi.org/10.48550/arxiv.2505.23643)
gives a formal planner model and characterizes the enforceable property class;
[Progent](https://doi.org/10.48550/arxiv.2504.11703) uses an SMT solver to classify each policy
update as narrowing or expansion; [SUDP](https://openalex.org/W7159547013) states seven
properties and proves them under standard cryptographic assumptions.

**Fix, in ascending order of cost:**

- **(a) TLA+/PlusCal model of the seven operations**, `Φ ≤ total` as a checked invariant,
  TLC-verified over a bounded state space. **This is the right choice.** The protocol is small
  — 7 operations, one state machine, four guards. A TLA+ spec is realistically 200–300 lines
  and reuses the reasoning already in `specs/04`. And it makes §6.4's problem disappear.
- (b) Alloy model of `narrows()` as a partial order, checking INV-1/INV-2/INV-9 by bounded
  exhaustive search over caveat sets.
- (c) Coq/Isabelle mechanization of the Φ argument. Overkill for the return.

### 6.4 🟠 HIGH — the guard-ablation table is not currently reproducible

`specs/04` §15 says: *"A model of §4 was executed before this document was written. Reproduce
by re-running the protocol model against these scenarios."* **The model is not in the
repository.** The most persuasive table in the project is backed by an artifact that does not
exist, and its numbers (400 interleavings, `leased` 160 vs `total` 100, 12/400, 55/400) cannot
be regenerated by a reviewer.

Worse, the table describes the *model*, not the *implementation*. The paper's claim must be
about the shipped code.

**Fix — and this is a strong experiment, not a chore:** re-run every ablation against the real
`agentiam_controlplane.db.ledger` with real Postgres, via the existing P-10 stateful
`hypothesis` machine. Add a `guards_disabled` fixture that patches out G1/G2/G3/G4 and the
skew margin individually. Report overspend magnitude and violation rate per guard.

**This converts a "we modelled it" table into a "we removed each guard from the shipped system
and measured the money escaping" table.** That is a substantially stronger result and it uses
machinery that already exists.

Note also that `specs/04` §13 already caught itself overstating this once: earlier drafts said
three siblings requesting 100 against a pool of 150 receive "100 / 50 / 0", which reads like a
guarantee and is not one — the split depends on which transaction takes the row lock first.
What is guaranteed is `Σ granted = min(Σ requested, available)`. **`threat-model.md` TM-06
still carries the stale "100 / 50 / 0" phrasing.** Fix it before it reaches a reviewer.

### 6.5 🟠 HIGH — the liveness argument is false in the deployed system

`STATUS.md` gap 27. `reap()` is fully correct — proved by T-013's tests and re-confirmed live
against real Postgres — and **nothing schedules it anywhere in the deployed system.** Only
chaos tests call it directly against an injected clock. Spec 04's own pseudocode says
`REAP() # background, every TTL/4`; no such loop runs.

**The paper's liveness bound — `ttl + S + ttl/4` = 80 s — is therefore a property of the chaos
harness, not of the system.** Publishing it as-is would be a false claim, and it is the kind a
reviewer who reads the code will find.

**Fix:** a periodic task in `pep_service.py` / the control plane's lifespan calling `reap()` at
spec 04's own `TTL/4` cadence. Narrow, well-specified, and it must land before T-058's F-4
drill (`Postgres restart mid-demo`) is rehearsed anyway — a restart strands a lease and today
nothing gives it back.

### 6.6 🟠 HIGH — no standard benchmark, and the honest answer is to build one

Reviewers in this space will ask why there is no AgentDojo number. The answer is real:
**AgentDojo, ASB, TAMAS and AgentLeak have no quantitative dimension at all.** They measure
whether a forbidden *action* occurred, never how much a permitted action *cost*. An agent that
correctly makes an authorized payment of ৳500,000 instead of ৳5,000 scores as a clean run on
every one of them.

**That is not a defect in AgentIAM's evaluation. It is the paper's argument, and it should be
turned into a contribution.**

> **AgentDojo-Budget** — extend the AgentDojo banking suite with (i) per-task spend mandates,
> (ii) sibling sub-agent delegation, and (iii) a *budget-violation* success criterion
> alongside the existing utility and security criteria. Release it.

Cost: moderate — AgentDojo is open, the banking suite is the natural host, and
[AgentDojo-PROV](https://doi.org/10.48550/arxiv.2406.13352) shows the instrument-and-extend
pattern working. **Payoff: high.** A benchmark contribution gives every subsequent paper in the
lane a reason to cite this one, and it converts "we didn't run the standard benchmark" from a
weakness into the finding.

### 6.7 🟠 HIGH — the classical-capability novelty claim is unsourced

Per §3.6. `specs/03` INV-5 and `specs/04` §13 both assert that classical capability systems do
not address quantitative resources shared across siblings, and both call it the publishable
observation. **Neither cites anything.** The search instruments could not reach that literature.

**This must be surveyed by hand** — Dennis & Van Horn 1966, Levy, KeyKOS/EROS space-banks,
Amoeba, SPKI/SDSI, Miller's object-capability model, Macaroons (Birgisson et al., NDSS 2014),
biscuit's own design notes, and the seL4 capability model. Space-banks in particular are the
closest prior art: a *quantitative* capability over storage. **If a reviewer knows about
space-banks and the paper does not mention them, the novelty claim collapses.** Better to cite
them, explain the difference (single-dimension, single-node, no partition, no monetary
semantics, no delegated concurrency), and be visibly correct.

### 6.8 🟡 MEDIUM — drift cannot be evaluated

§3.5. NFR-9 has never been measured; T-034/T-035 deferred. **Two options and no third:**

- **(a) Cut it.** Keep only the §3.5 negative result (embeddings blind to numeric magnitude,
  f1 non-monotonic) as a two-paragraph subsection titled honestly. **Recommended.**
- (b) Build the dataset. 2,000+ labelled pairs, weeks of irreducible human labelling, then
  calibration curves and an FPR at the operating threshold. A separate paper's worth of work.

If (a): also keep the slow-drift evasion (TM-11 / A-27) as **named future work**. The
threat-model already calls it a genuine open problem and stating it voluntarily is worth more
than a weak detector.

### 6.9 🟡 MEDIUM — one machine, one region, no deployment

All numbers come from one development host. Single-region is explicitly out of scope
(`PLAN.md` §1.4), which is defensible, but it must be stated as a validity threat. There is no
production deployment and no user study.

**Cheapest meaningful improvement:** run the full suite on two additional hosts (one cloud
x86_64, one ARM) and report the spread. The k3s manifests and the container image make this
nearly free, and it converts "measured on my laptop" into "measured on three platforms, here is
the variance." Note the existing evidence that platform matters: the SBOM's
`cdx:python:package:required-extra` property appears on Ubuntu and not on Fedora,
reproducibly, and was never fully root-caused.

### 6.10 🟡 MEDIUM — sibling modes are implemented but not compared

Proportional split and shared pool are both built, and the paper's C1 rests on the pair. But
there is **no quantitative comparison**: no utilization curve, no refusal-rate curve, no
characterization of when the split's waste exceeds the pool's contention cost.

**Experiment E3 (§7).** This is a small amount of work against machinery that exists, and it
turns a design-space observation into a result. Related: PB-8 (adaptive lease sizing, ≥60%
top-up reduction vs fixed) is specified in `PLAN.md` §13.1 and never run — T-015 is deferred.
The algorithm is specified; **if it can be run, it is a second results figure.**

### 6.11 🟢 LOW — housekeeping that will nonetheless be noticed

- Mutation testing (`mutmut`) never run — gap 4. One run is cheap evidence that ~1,500 tests
  are load-bearing; artifact reviewers like it.
- Coverage reported but never gated in CI — gap 14.
- Assumption A1 (biscuit block-fact scoping) verified by hand, not CI — gap 6. **This is the
  single assumption the whole design rests on** (`threat-model.md` §4). A paper asserting
  INV-1 with a hand-verified premise should say so, and better, pin it with a test.
- `budgets.mandate_id` has no FK — gap 7.
- `POLICY_BUNDLE_STALE` unreachable — gap 26. Affects the fail-closed story: a staleness guard
  that cannot fire is not a guard, and the paper claims fail-closed on stale bundles.

---

## 7. The evaluation the paper needs

Eight experiments. E1, E2 and E5 are near-free against existing machinery; E4 and E6 are the
expensive ones.

### E1 — Guard ablation against the implementation ★ headline result

Per §6.4. Remove G1 / G2 / G3 / G4 / the skew margin, one at a time, from the *shipped* ledger
against real Postgres. Drive with the existing P-10 stateful machine plus the concurrent
acquire harness.

**Report:** violation rate over N interleavings, **overspend magnitude in currency units**, and
which invariant fires first. Two of the five guards protect something other than what their
name suggests (`specs/04` §5.1) — that belongs in the table.

**Why it is the headline:** it is a falsification result. "We removed the guard and the money
escaped" is qualitatively more convincing than "the invariant held."

### E2 — Latency decomposition with baselines

Extend the existing PB-2 breakdown with the §6.1 baselines. Keep the discipline (percentiles
only, ≥3 runs, variance reported).

**Report:** the per-step table; per-request in-process cost (~392 µs) as the throughput bound;
against no-enforcement, OAuth-scope-only, and centralized-check. **Cite AIP's 0.189 ms Python
verify next to our 221 µs and note the convergence explicitly** — two independent
implementations of biscuit verification landing in the same place is a validity argument.

### E3 — Sibling mode comparison: split vs pool

Sweep: N children ∈ {2, 5, 10, 20}; demand skew ∈ {uniform, Zipf}; pool total fixed.

**Report:** budget utilization, refusal rate, top-up RPS against the ledger, p99 acquire
latency under contention. Establishes *when* each mode is correct, which is what makes C1 a
design contribution rather than a description. Fold in PB-8 (adaptive vs fixed sizing) if
T-015 can be un-deferred.

### E4 — Partition and failure, all twelve scenarios

Seven of twelve chaos scenarios are deferred. The most publication-relevant are:

- **CH-7 (clock skew +60 s on one PEP)** — this is the *only* direct test of the skew argument
  in §5's C2. **Non-negotiable if the skew rule is a claimed contribution.**
- CH-2 (Redis down → revocation falls back to pull) — the correctness backstop.
- CH-5 (500 ms ledger latency) — supports "decisions are local."
- CH-6 (10% packet loss) — no double-spend under retry.

CH-11 and CH-12 are lower value for this paper. Also fix, or state, the two known open items:
gap 21 (a partitioned PEP cannot shut down and `asyncio.wait_for` does not bound it — measured
stuck 5 minutes against a 5 s bound) and gap 20 (a transient audit-sink outage treated as a
poison batch, records discarded while requests keep being authorized, contradicting ADR-026).
**Gap 20 is an availability-vs-auditability tradeoff the paper should name rather than fix
quietly** — it is exactly the kind of thing CH-12 exists to expose.

### E5 — Adversarial evaluation, with the sibling swarm scaled up

The 22 attacks exist. Two things to add:

- **Scale A-17 (sibling swarm).** Today: 20 siblings. Sweep to 100+ concurrent siblings against
  a fixed pool and show `Σ spend ≤ mandate` holding, with the acquire-latency curve. This is
  the attack the paper is *about*.
- **Report like the neighbours do.** ASR and utility, not pass/fail. [AIP](https://openalex.org/W7142557907)
  reports 600 attempts / 100% rejection; [OAP](https://openalex.org/W7140345986) reports 879
  attempts / 0% success. Match the format so the numbers are comparable.

Keep the accepted risks visible: bearer replay (TM-01), slow drift (TM-11), agent-reported
amounts (TM-23), and the partial ones (TM-05 confused deputy, TM-07 stranded lease, TM-16
timing side channel, TM-22 clock skew). *Teams that claim everything mitigated get discounted.*

### E6 — AgentDojo-Budget ★ benchmark contribution

Per §6.6. Extend AgentDojo's banking suite with spend mandates, sub-agent delegation, and a
budget-violation criterion. Evaluate: unprotected agent, OAuth-scope-only, and AgentIAM.

**Predicted headline:** the unprotected and scope-only configurations complete the task and
violate the mandate; AgentIAM completes it within the mandate. *If that is not what happens,
that is a more interesting paper.*

### E7 — Parser differential survey (feeds Option C)

Take TM-26's measured differential and run it against the retrieved proxy-shaped systems.
Report which are affected. **Coordinate disclosure before publishing.**

### E8 — Cross-platform variance

Per §6.9. Full suite on ≥3 platforms; report the spread, not the best run.

---

## 8. Reviewer objections, and the honest answer to each

Written as Q&A because these will be asked verbatim.

**"How is this different from AIP / IBCT?"**
> AIP does verifiable delegation with holder-side attenuation — same library, and we should
> say so plainly and cite it as concurrent work. It has no quantitative dimension: no ledger,
> no concurrency analysis, no partition behaviour, no budget. Our contribution begins exactly
> where its token layer ends. **Prepare this answer as a table in the related-work section, not
> as a defensive paragraph.**

**"Why not evaluate on AgentDojo?"**
> Because AgentDojo cannot express the failure we prevent — it measures whether a forbidden
> action occurred, never how much a permitted action cost. We extend it (E6) and release the
> extension.

**"Isn't this just distributed rate limiting / a token bucket?"**
> No, and the difference is worth a paragraph. A rate limiter is best-effort and its failure
> mode is *slightly too many requests*. This is a monetary ceiling whose failure mode is *money
> spent twice*, under an adversarial holder rather than a cooperative client. Hence: safety
> that does not depend on PEP correctness (G2), one-way state transitions (G3), the `2S` skew
> gap, and a CP rather than AP choice. **Say "CP, not AP, and that is the correct choice for
> money" in exactly those words** — it is what a reader with a distributed-systems background
> is listening for.

**"The p99 latency numbers vary tenfold across runs."**
> Correct, and reported rather than hidden. It is a host artifact, the generator's own lag
> series shows it, and we quote only the figures the host can support. E2/E8 fix it. **Never
> quote the good run.**

**"Your safety argument is prose."**
> §6.3(a). Ship the TLA+ model.

**"Only one implementation, one language, one deployment."**
> True. Mitigated by E8's cross-platform runs and by the artifact being fully containerized and
> reproducible. State it as a validity threat.

**"You claim classical capability systems don't handle quantitative resources — do you know
about space-banks?"**
> §6.7. **This is the objection that could actually sink the novelty claim.** Do the by-hand
> survey and pre-empt it.

**"Your drift detector has no false-positive rate."**
> Correct, which is why it is not a contribution. §6.8(a).

**"Isn't the intent-hash binding defeated by an agent that just asserts the right intent?"**
> A fair and sharp question the repo already answers: the PEP is stateless and cannot look the
> mandate up, so the client asserts the task intent in plain English and the PEP hashes it and
> compares against the token's `intent_hash`, denying `INTENT_MISMATCH` on a mismatch. The
> agent cannot assert a *different* task — but it can assert the true task while attempting a
> drifted action, which is precisely why drift scoring exists and precisely why TM-11 is an
> accepted risk. **Have this answer ready; the design is sound and the honest limitation is
> already documented.**

---

## 9. Venues, timing, and the priority problem

### 9.1 The priority problem comes first

**§3.2 and §3.3 show a field that went from empty to a dozen entrants in ~18 months, with the
closest competitor four months old and an independent team already using the same library for
the same technique.**

`PLAN.md` §21 defers the paper (T-060) past BIIN submission, which was correct when written.
It is no longer obviously correct. **Recommendation: post an arXiv preprint of Option B as soon
as E1 and §6.5's reap fix are done — ahead of, and independent of, any venue submission.**
Cost: a few weeks. Benefit: a dated public claim on the sibling-budget framing before someone
else makes it.

The repository is unusually well-placed for this: `docs/DECISIONS.md` is an append-only ADR log
and `docs/JOURNAL.md` is a dated development narrative, so the invention timeline is already
documented — which is what `PLAN.md` §18.2 asked for and has, in fact, been maintained.

### 9.2 Venue analysis

| Venue | Fit | Requires | Realistic? |
|---|---|---|---|
| **arXiv preprint** | Perfect | E1 + §6.5 fix + honest §6 | **Yes — do this first** |
| USENIX Security / CCS / NDSS (main track) | Good topic fit | TLA+ model, baselines, off-box load, benchmark contribution — essentially all of §6 | Hard solo, one cycle. Aim for the cycle *after* the preprint |
| **NDSS / CCS workshops**, SaTS, AISec, WPES | **Strong** | Option B trimmed, or Option C standalone | **Yes** |
| ACSAC, AsiaCCS, ESORICS | Strong — these reward measured systems work | E1, E2, E4 | **Yes** |
| **EuroSys / Middleware / USENIX ATC** | **Underrated.** This is arguably a *systems* paper: a lease protocol, a potential-function safety argument, chaos results, a latency decomposition | E1–E4, off-box load | **Yes, and the reviewing may be friendlier** |
| SoK submission | Possible — §3's landscape map is half an SoK already | A by-hand survey (§6.7) plus a taxonomy | Medium |
| DSN, SRDS (dependability) | Good fit for the chaos + invariant-checker methodology | E1, E4 | Yes |

**Recommended sequence:** arXiv preprint → workshop (fast feedback, dated priority) →
ACSAC/EuroSys-class full paper with the complete §7 evaluation. Reserve USENIX Security for
after the benchmark (E6) exists, because that is what would carry it.

### 9.3 Interaction with BIIN

None of this competes with the submission. The BIIN technical report is **Option A** — the
system paper — and it can be written from `README.md`, `STATUS.md`, `threat-model.md` and the
evidence pack essentially as they stand. The research paper is Option B and is a strict subset
plus new experiments. `T-059` (submission package) and this document's RP-1/RP-2 overlap
substantially: the architecture doc, the IP statement and the evidence pack all serve both.

**One thing to protect:** `PLAN.md` §14.4's IP claim (100% own development, all core code
original) and NFR-10 (development in Bangladesh, all model weights open-weight and
self-hosted). Note that **ADR-040 already supersedes the no-egress guarantee** — the NL
compiler runs on hosted inference by default (`AGENTIAM_LLM_BACKEND` = gemini | groq | ollama),
with local remaining the production target and a config flip away, pinned by
`test_llm_backend.py`. If NFR-10 is quoted in a paper or a submission, quote it with that
qualification attached. The repository is already honest about this; the paper must be too.

---

## 10. Work packages

Effort in solo-developer weeks. **P0 = required for any submission.**

| ID | Work | Depends on | Effort | Priority |
|---|---|---|---|---|
| **RP-0** | **By-hand survey of classical capability + quantitative resource control** (§6.7). ACM DL / Scholar, not the connectors. Space-banks especially | — | 1 w | **P0** |
| **RP-1** | Fix gap 27 — schedule `reap()` at TTL/4 in the deployed lifespan (§6.5). Also unblocks T-058 F-4 | — | 2–3 d | **P0** |
| **RP-2** | E1 — guard ablation against the implementation (§6.4, §7). Fix TM-06's stale "100/50/0" phrasing while in there | RP-1 | 1 w | **P0** |
| **RP-3** | E2 — baselines: no-enforcement, OAuth-scope-only, centralized-check (§6.1) | — | 1.5 w | **P0** |
| **RP-4** | Off-box load generator; re-run NFR-2 (§6.2) | 2 hosts | 1 w | **P0** |
| **RP-5** | **arXiv preprint of Option B** | RP-0…RP-4 | 2 w | **P0** |
| RP-6 | TLA+ model of the 7 operations; TLC-check `Φ ≤ total` (§6.3a) | RP-2 | 1.5 w | P1 |
| RP-7 | E3 — sibling split-vs-pool comparison; PB-8 if T-015 can be un-deferred (§6.10) | RP-2 | 1 w | P1 |
| RP-8 | E4 — CH-7 (skew), CH-2, CH-5, CH-6; name gaps 20 and 21 (§7) | RP-1 | 1.5 w | P1 |
| RP-9 | E5 — scale A-17 to 100+ siblings; re-report as ASR/utility (§7) | RP-2 | 4 d | P1 |
| RP-10 | E8 — cross-platform runs, report variance (§6.9) | RP-4 | 3 d | P1 |
| **RP-11** | **E6 — AgentDojo-Budget benchmark + release** (§6.6) | RP-3 | 3 w | **P1 ★** |
| RP-12 | E7 + Option C paper: parser-differential survey, coordinated disclosure (§4C, §7) | — | 2 w | P2 |
| RP-13 | `mutmut` run; CI coverage gate; pin assumption A1 with a test (§6.11) | — | 4 d | P2 |
| RP-14 | Cedar vs OPA (PB-6), if T-024's second backend is built | T-024 | 1.5 w | P3 |
| RP-15 | T-034 drift dataset + T-035 calibration — **only if drift becomes a contribution** (§6.8b) | — | 6 w+ | P3 |

**Critical path to a preprint: RP-0 → RP-1 → RP-2 → RP-3 → RP-4 → RP-5 ≈ 7 weeks.**

Full-paper path adds RP-6 through RP-11: **≈ 16 weeks.**

**Sequencing against the BIIN work.** T-057 (demo scripting), T-058 (failure drills) and T-059
(submission package) come first — they are the deadline. **RP-1 should be pulled forward and
done inside that window regardless**, because F-4 needs it. RP-0 can be done in parallel; it is
reading, not building.

---

## 11. What not to claim

Written down because these are the claims that would be easiest to make and would each cost
more credibility than they buy.

1. **Never "the first."** §3.6 shows the instruments cannot support it and RP-0's by-hand
   survey may well find prior art in space-banks. Write *"we are not aware of prior work that
   …, and §N surveys the closest"* and let the survey carry it.
2. **Never quote the good NFR-2 run.** The range straddles the budget tenfold. Quote the range,
   or quote nothing.
3. **Never quote the ~5 µs decision latency.** Superseded — it measured `decide()` against a
   stub policy, excluding the most expensive thing in the path. The real number is 151.3 µs
   median / 350.0 µs p99. Anywhere the old figure survives in a slide deck, fix it.
4. **Never quote 43% or 63% for the compiler.** Both were inflated by three dataset prompts
   having leaked into the system prompt as few-shot examples. The clean number is 27/30 on a
   leak-free prompt with all 30 attempted and zero throttling.
5. **Never say "5 chaos scenarios passed" without saying "of 12."** `chaos-results.md` already
   lists all twelve with the seven marked *not run — deferred*, precisely because five rows and
   no mention of the rest reads as twelve passes to anyone skimming.
6. **Never say the invariant "held" for CH-1 without the unavailable count.** 3 held / 0
   violated / **11 unavailable**. The third state is the point.
7. **Never claim NFR-10's no-egress guarantee unqualified.** ADR-040 superseded it.
8. **Never claim the liveness bound until RP-1 lands.** Today it is a property of the test
   harness (§6.5).
9. **Never claim drift detection works.** No FPR has ever been measured.
10. **Never claim 27/27 threats mitigated.** The threat model states 3–4 accepted risks and
    several partials, each with a bound. *That* is the claim, and it is the stronger one.

---

## 12. Artifact and reproducibility plan

AgentIAM is already close to the top of what artifact evaluation committees see. Inventory
against the usual ACM badging criteria:

| Criterion | Status |
|---|---|
| Available | Apache-2.0, public repo | ✅ |
| Functional | `make demo-up` all-healthy in 20 s across two cold runs | ✅ |
| Reusable | 5 packages, protocol-based interfaces, three composition roots | ✅ |
| Reproducible | Deterministic SBOM with a CI freshness gate; pinned deps; container image with three entrypoints; k3s manifests live-deployed | ✅ |
| Signed provenance | Keyless cosign + SBOM attestation, self-verified in the release job | ✅ **rare** |
| Self-contained evidence | `evidence-pack.html`, ~90 KB, zero network references | ✅ **rare** |
| Generated-not-written results | `chaos-results.md` and `performance.md` regenerated from JSON with `--check` in CI | ✅ **rare** |
| Security scanning | bandit, pip-audit, trivy, gitleaks in a dedicated CI job; every waiver documented with rationale | ✅ |

**Gaps to close for artifact evaluation specifically:**

- **The protocol model must ship** (§6.4). Whether it becomes TLA+ (RP-6) or a re-runnable
  Python model, the ablation table needs a regenerating artifact.
- `performance.md` has no CI drift check (gap 24) — deliberately, since PB-2 timings vary by
  design and a byte-exact check would fail on noise. **State the reason in the artifact
  appendix**; an unexplained missing check reads as an oversight.
- One-command reproduction of *every* figure. Today: `make chaos`, `make security`, `make sbom`
  and `scripts/run_load_test.py` each regenerate a piece. Add `make paper-figures`.
- The hardware dependency must be declared. The load test spins its own Postgres on an
  ephemeral port precisely because a native install can shadow the compose one on 5432 — a real
  reproducibility hazard the repo already handles correctly. **Document it in the artifact
  README**; a reviewer hitting it blind would report a broken artifact.

**One methodological practice worth naming in the paper itself**, because it is unusual and
directly serves reproducibility: the LLM client **logs the model alias it resolved to**
(`gemini-flash-lite-latest` → `gemini-3.5-flash-lite`), on the stated grounds that an
unattributable benchmark is not a benchmark. Model aliases churn — `gemini-2.0-flash` is
retired, `gemini-2.5-flash-lite` is closed to new users, both 404. Any paper reporting an LLM
number against a floating alias and not recording the resolution has published an
irreproducible result. **This is a one-sentence contribution to evaluation practice and it
costs nothing to state.**

---

## 13. Bibliography

Retrieved 2026-08-21. Grouped by the role each source plays. **Every one of these is a lead to
open and read, not a conclusion** — several are recent preprints with zero citations, and their
claims are unverified by anyone but their authors.

### Direct competitors — read before writing

- [AIP: Agent Identity Protocol for Verifiable Delegation Across MCP and A2A](https://openalex.org/W7142557907) — Prakash, 2026, arXiv 2603.24775. **The collision.**
- [CapChain: A Capability-Token Access Control Architecture with Verifiable Provenance for Multi-Agent LLM Systems](https://doi.org/10.3390/app16157776) — Choong, Hsieh & Leu, *Applied Sciences*, 2026
- [Before the Tool Call: Deterministic Pre-Action Authorization for Autonomous AI Agents](https://openalex.org/W7140345986) — Uchibeke, 2026 (OAP; 53 ms median, 0%/879)
- [Digital Identity for Agentic Systems: Toward a Portable Authorization Standard](https://openalex.org/W7161204566) — Madhira, 2026
- [Delegation Without Escalation: Capability tokens, attenuation discipline, and the patterns that survive in production](https://doi.org/10.5281/zenodo.20242392) — Hasbini, 2026
- [SUDP: Secret-Use Delegation Protocol for Agentic Systems](https://openalex.org/W7159547013) — Yu et al., 2026

### Gap corroboration — cite in the introduction

- [Authorization Propagation in Multi-Agent AI Systems: Identity Governance as Infrastructure](https://openalex.org/W7160727562) — Tallam, 2026. **Names execution-count revocation as the state of the art on quantitative limits.**
- [SoK: Security of Autonomous LLM Agents in Agentic Commerce](https://openalex.org/W7155244781) — Mao et al., 2026. **"Authorization gaps left by current agent-payment protocols."**
- [OAuth Is Not Enough: Authorization Challenges for Autonomous AI Agents](https://doi.org/10.36227/techrxiv.174952577.74018032/v1) — Chopra, 2025
- [The Authorization-Execution Gap Is a Major Safety and Security Problem in Open-World Agents](https://openalex.org/W7161204744) — Wu et al., 2026. **Taxonomy that TM-26 instantiates.**
- [Open Challenges in Multi-Agent Security](https://doi.org/10.48550/arxiv.2505.02077) — Schroeder de Witt et al., 2025 (24 authors)

### Adjacent defenses — related work

- [Progent: Securing AI Agents with Privilege Control](https://doi.org/10.48550/arxiv.2504.11703) — Shi et al., 2025 (SMT-checked monotonic confinement)
- [Securing AI Agents with Information-Flow Control](https://doi.org/10.48550/arxiv.2505.23643) — Costa et al., 2025 (Fides)
- [Aligning Provenance with Authorization: A Dual-Graph Defense for LLM Agents](https://openalex.org/W7162699945) — Wang, Li & Tian, 2026 (AuthGraph)
- [SecureClaw: Clawing Back Control of LLM Agents](https://openalex.org/W7164233523) — Ma & Schmid, 2026
- [Agentic Permissions Policy Algebra for Taint Confinement in LLM Agents](https://openalex.org/W7171748979) — Kravchenko et al., 2026 (APPA)
- [DRIFT: Dynamic Rule-Based Defense with Injection Isolation](https://doi.org/10.52202/085713-2791) — Li et al., 2025
- [The Task Shield: Enforcing Task Alignment to Defend Against Indirect Prompt Injection](https://doi.org/10.18653/v1/2025.acl-long.1435) — Jia et al., ACL 2025
- [IPIGuard: A Tool Dependency Graph-Based Defense](https://doi.org/10.18653/v1/2025.emnlp-main.53) — An et al., EMNLP 2025
- [RTBAS: Defending LLM Agents Against Prompt Injection and Privacy Leakage](https://doi.org/10.48550/arxiv.2502.08966) — Zhong et al., 2025
- [Delegation-Aware Runtime Contracts for Open LLM Multi-Agent Systems](https://doi.org/10.21203/rs.3.rs-10533072/v1) — Bamil, 2026
- [Safe Bilevel Delegation: A Formal Framework for Runtime Delegation Safety](https://openalex.org/W7159891422) — Sun, 2026

### Benchmarks — E6's context

- [AgentDojo-PROV: A W3C PROV-O Corpus of LLM Agent Executions](https://doi.org/10.48550/arxiv.2406.13352) — Debenedetti et al., 2024
- [Agent Security Bench (ASB)](https://doi.org/10.48550/arxiv.2410.02644) — Zhang et al., 2024
- [TAMAS: Benchmarking Adversarial Risks in Multi-Agent LLM Systems](https://doi.org/10.18653/v1/2026.acl-long.1442) — Kavathekar et al., ACL 2026
- [AgentLeak: Internal-Channel Privacy Leakage in Multi-Agent LLM Systems](https://doi.org/10.1109/access.2026.3704541) — El Yagoubi et al., IEEE Access 2026
- [Do Coding Agents Understand Least-Privilege Authorization?](https://openalex.org/W7161452818) — Yan et al., 2026 (AuthBench)
- [AGENTVIGIL: Automatic Black-Box Red-teaming for Indirect Prompt Injection](https://doi.org/10.18653/v1/2025.findings-emnlp.1258) — Wang et al., EMNLP 2025
- [Prompt Injection Attack to Tool Selection in LLM Agents](https://doi.org/10.14722/ndss.2026.230675) — Shi et al., NDSS 2026

### NL → policy — §3.4

[1] [“Say What You Mean”: Natural Language Access Control With Large Language Models for Internet of Things](https://consensus.app/papers/details/120d2023b2d659db86142d02bc7bfc0c/?utm_source=claude_desktop) (Cheng et al., 2025, 8 citations, *IEEE TIFS*)
[2] [Synthesizing Access Control Policies Using Large Language Models](https://consensus.app/papers/details/c2f0740d02ef5f81ae45a33fca92ea96/?utm_source=claude_desktop) (Vatsa et al., 2025, 11 citations, *NLBSE*)
[3] [Translating Natural Language Specifications into Access Control Policies by Leveraging Large Language Models](https://consensus.app/papers/details/dc3307e97840557183b3197c9d133c2e/?utm_source=claude_desktop) (Lawal et al., 2024, 19 citations, *IEEE TPS-ISA*)
[4] [On-device derivation of IoT usage control policies: Automating U-XACML policy generation from natural language with LLMs](https://consensus.app/papers/details/4a34a9468c1d5c289979197e19583679/?utm_source=claude_desktop) (Alajramy et al., 2025, 6 citations, *Future Generation Computer Systems*)
[5] [Policy-Aware Generative AI for Safe, Auditable Data Access Governance](https://consensus.app/papers/details/e1ca6959c44d58f0aa4de3420a16256b/?utm_source=claude_desktop) (Al Mandalawi et al., 2025, 7 citations, *KSE*)
[6] [Mining Attribute-Based Access Control Policies with Large Language Models](https://consensus.app/papers/details/92da122efcaa5df58aaf7130d4693872/?utm_source=claude_desktop) (Bui et al., 2026, 0 citations, *ACM SACMAT*)
[7] [From Plain English to XACML Policies: An AI-Based Pipeline Approach](https://consensus.app/papers/details/ac2e36a80b055879be944d452b08a2d7/?utm_source=claude_desktop) (Paratore et al., 2025, 5 citations)
[8] [A Behavioral Benchmark Dataset for Evaluating Access Control Compliance in Large Language Models](https://consensus.app/papers/details/c7f9ad819be65482899725a516c68b86/?utm_source=claude_desktop) (Xin et al., 2026, 0 citations, *GAIIS*) — ACBench
[9] [Role-Conditioned Refusals: Evaluating Access Control Reasoning in Large Language Models](https://consensus.app/papers/details/8e7bb885c5545e8bbc5d50e9808d2d4e/?utm_source=claude_desktop) (Klisura et al., 2025, 4 citations, *ArXiv*)
[10] [Natural Language Access Control (NLAC): From Help Desk Requests to Structured Policies](https://consensus.app/papers/details/e0f554ad376352a2a7fe03129933866d/?utm_source=claude_desktop) (Wessner et al., 2026, 0 citations, *ArXiv*) — NLACBench

### Drift — §3.5

- [Agentic AI in Healthcare and Medicine: A Seven-Dimensional Taxonomy](https://doi.org/10.1109/access.2026.3651218) — Vatsal, Dubey & Singh, IEEE Access 2026. **Drift detection absent in ~98% of 49 studies.**
- [TRiSM for Agentic AI](https://doi.org/10.48550/arxiv.2506.04133) — Raza et al., 2025
- [Towards trustworthy agentic AI: safety, robustness, privacy, and system security](https://doi.org/10.20935/acadai8260) — Qi et al., 2026

### Protocol and ecosystem context

- [A Survey on Model Context Protocol: Architecture, State-of-the-art, Challenges and Future Directions](https://doi.org/10.36227/techrxiv.174495492.22752319/v1) — Ray, 2025 (35 citations, sharply rising)
- [The Synergistic Integration of Access Control Management and Large Language Model Agents: A Survey](https://doi.org/10.36227/techrxiv.177075184.43798334/v1) — Periti & Saha, 2026. **Its "ACM for agents" framing is exactly AgentIAM's lane.**
- [Protocol Translation Vulnerabilities in LLM Agent Communication Stacks](https://doi.org/10.2352/ei.2026.38.3.mobmu-320) — Mahipal, 2026 (TSAF)
- [Beyond Identity Governance: A Protocol-Level Security Testing Framework for Multi-Agent AI Systems](https://doi.org/10.5281/zenodo.19343034) — Saleme, 2026. **Its "WHO vs HOW governance gap" is a usable frame.**
- [A Systematic Survey of Security Threats and Defenses in LLM-Based AI Agents: A Layered Attack Surface Framework](https://openalex.org/W7158422866) — Chu, 2026 (116 papers, 2021–2026)

### To be surveyed by hand — RP-0, not retrievable by these instruments (§3.6)

Dennis & Van Horn (1966), *Programming Semantics for Multiprogrammed Computations* · Levy,
*Capability-Based Computer Systems* · KeyKOS / EROS space-banks · Amoeba capabilities ·
SPKI/SDSI · Miller, *Robust Composition* (object-capability model) · Birgisson et al.,
*Macaroons: Cookies with Contextual Caveats for Decentralized Authorization in the Cloud*
(NDSS 2014) · the biscuit specification and its design rationale · seL4's capability model ·
Cedar's formal semantics · the classical lease literature (Gray & Cheriton, SOSP 1989).

**Gray & Cheriton's leases paper is the direct ancestor of spec 04 and is currently uncited
anywhere in this repository.** Fix that first.

---

## Appendix — cross-reference from this document to the repository

| This document | Repository |
|---|---|
| C1 sibling budget problem | `specs/03` INV-5; `specs/04` §13; TM-06 |
| C2 lease protocol | `specs/04` §4, §6, §7, §8, §9 |
| C3 spec bugs found by probing | ADR-015, ADR-017; TM-21, TM-25; `JOURNAL.md` |
| C4 chaos + invariant sidecar | `tests/chaos/`, `docs/benchmarks/chaos-results.md`; ADR-049, ADR-050 |
| E1 guard ablation | `specs/04` §15; P-10 in `tests/property/` |
| E2 latency | `docs/benchmarks/performance.md`; ADR-052 |
| E5 adversarial | `tests/security/test_redteam_suite.py`; ADR-048 |
| Option C parser differential | TM-26; `specs/10` §5.2; `tests/security/test_parameter_pollution.py` |
| §6.5 liveness bug | `STATUS.md` gap 27; `docs/deployment.md` §2.3 |
| §6.8 drift negative result | `specs/06` §5.1 |
| §11 superseded numbers | `performance.md`; `STATUS.md` rows 19, 19a, 19a-partial |
