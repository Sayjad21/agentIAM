# AgentIAM — Formal Threat Model

**Purpose.** This document states AgentIAM's adversary model, trust boundaries, and security
properties in the form a security-venue reviewer expects: capabilities enumerated before
mitigations, properties defined before they are claimed, and every property mapped to the
line of code that enforces it — or marked as unenforced.

**Relationship to `docs/threat-model.md`.** That document already exists and is not a
placeholder: it is a 27-entry STRIDE register with statuses, measured attack results, and
seven explicitly non-mitigated entries. It is an *engineering* threat model — organized by
threat, answering "what could go wrong and what did we do about it." This document is the
*academic* complement, organized by property, answering "what exactly is guaranteed, against
whom, under what assumptions, and where is that enforced." Neither supersedes the other. TM-nn
identifiers below refer to the STRIDE register; they are reused rather than renumbered.

**Status of this document.** Descriptive audit, not a design proposal. Every `file:line`
citation was read at the commit this was written against (`5c2c6ee`). Line numbers drift;
the function names are the durable reference.

---

## 1. System model

### 1.1 Principals

| Principal | Definition | Trusted for |
|---|---|---|
| **Human operator** (`principal_id`) | The person who approved the task. Bound into the root token as an authority-block fact | Nothing at request time; they are the *subject* of attribution, not a runtime actor |
| **Root issuer** | Holds the Ed25519 root private key; mints root tokens from a `Mandate` | Minting authority from nothing. Full compromise if exfiltrated (A3) |
| **Agent** (`agent_id`, depth *d*) | An LLM-driven process holding a token at chain depth *d*. May spawn children by attenuating its own token | **Nothing.** Assumed subvertible by prompt injection as a normal condition |
| **PEP** | The enforcement proxy. Verifies, decides, reserves budget, emits records, forwards | Correct execution. Safety does not depend on it *for a PEP that follows the protocol* (§5, P4) — but it holds database credentials, so a fully compromised one is a different case |
| **Control plane** | Ledger, policy bundle store, revocation publisher, audit sink, escalation queue, console | Integrity of the budget and audit ledgers |

### 1.2 Objects

A **mandate** is a grant: `(mandate_id, task_id, principal_id, intent_hash, scopes, budget,
max_depth, not_before, expires_at)`. A **token** is a biscuit chain whose authority block
encodes a mandate and whose subsequent blocks each add caveats. **Authority** of a chain is
the intersection of the constraints in all of its blocks.

---

## 2. Trust boundaries

```
  UNTRUSTED                  UNTRUSTED-BY-ASSUMPTION            TRUSTED
┌────────────────┐         ┌──────────────────────┐      ┌──────────────────┐
│ Tool outputs   │  B3     │ Agent process        │  B1  │ Control plane    │
│ Task text      │ ──────▶ │ holds a token,       │ ───▶ │ ledger · policy  │
│ Third-party    │ inject  │ may be fully         │      │ audit · revoke   │
│ web content    │         │ adversarial          │      └──────────────────┘
└────────────────┘         └──────────────────────┘               ▲
                                      │                           │ B2
                                      │ every tool call           │ leases,
                                      ▼                           │ records
                           ┌──────────────────────┐               │
                           │ PEP                  │───────────────┘
                           │ trusted code in the  │
                           │ most-exposed position│
                           └──────────────────────┘
                                      │ B5
                                      ▼
                           ┌──────────────────────┐
                           │ Upstream tool / API  │
                           │ second parser        │
                           └──────────────────────┘
```

- **B1 — Agent → PEP.** The principal boundary. Everything the agent asserts about itself is
  untrusted: its identity claim, its declared depth, its reported spend. Only what the token
  cryptographically carries, and what the verifier itself supplies, is believed.
- **B2 — PEP → control plane. Specified as a boundary; not one in the deployed system.**
  Guards G2/G3 are written so a *misbehaving* PEP cannot break the budget invariant, and they
  do that. But `scripts/pep_service.py` holds database credentials and speaks to Postgres
  directly — there is no ledger service and no network boundary here, so the guards bind only
  while the PEP calls `ledger.py`. See P4's boundary note and
  [`lease-plan.md`](lease-plan.md) GAP-A.
- **B3 — Tool output → agent.** Attacker-controlled content enters here. This is the injection
  channel, and it is assumed to succeed.
- **B4 — Human → console.** OIDC-authenticated. Approval actions are high value; self-approval
  is refused.
- **B5 — PEP → upstream.** A *semantic* boundary, not merely a network one: the PEP and the
  upstream each parse the same bytes, and a disagreement between the two parsers is an
  exploitable gap (TM-26).

---

## 3. Attacker model

Capabilities are stated as a lattice so that each property in §5 can name the strongest
attacker it holds against. This is the part most often left implicit in agent-security work,
and the part that determines whether a claim means anything.

| Class | Capability | In scope |
|---|---|---|
| **A0 — Content attacker** | Controls text the agent reads: web pages, documents, tool results, retrieved context. Cannot execute code, cannot see tokens | **Yes — the primary adversary** |
| **A1 — Subverted agent** | A0 plus full control of the agent process's *behaviour*: chooses which tools to call, with which arguments, in which order; may attenuate its own token and spawn children; holds its own token | **Yes** |
| **A2 — Network adversary (on-path)** | Observes and modifies traffic between components. Assumed to face TLS; relevant because tokens are bearer credentials | **Partially** — token theft is modelled (TM-01) and accepted |
| **A3 — Token thief** | Has obtained a valid token belonging to another agent, by any means | **Yes, and the property fails** — see §6, bearer semantics |
| **A4 — Compromised PEP** | Arbitrary code execution in a PEP process; holds live leases; **holds database credentials in the current deployment**; can submit arbitrary commits or arbitrary SQL | **Partially, and less than an earlier draft claimed** — the invariant's *shape* is held by a database `CHECK`, but `total` is mutable. Confidentiality and attribution do not hold. See P4's boundary note |
| **A5 — Compromised control plane** | Controls the ledger and audit database | **No** — out of scope; this is the root of trust |
| **A6 — Root key holder** | Has the Ed25519 root private key | **No** — total compromise by construction (A3 in `docs/threat-model.md` §4) |

**The interesting claim is at A1.** AgentIAM's thesis is that a *fully subverted agent* —
one whose instructions the attacker has completely rewritten — still cannot exceed the
authority its token carries, because authority is cryptographic rather than linguistic. Most
of §5 is the precise statement of what that means and where it stops.

**Explicitly not assumed:** that the agent is honest, that prompt injection can be prevented,
that the LLM can be relied upon to refuse, or that guardrails in the model are load-bearing.
Prompt injection succeeding is the *premise*, not a failure case.

---

## 4. Security properties — definitions

Each property is stated so it could be falsified.

- **P1 — Token unforgeability.** Without the root private key, no adversary can produce a
  token that `verify()` accepts with authority-block facts it did not sign.
- **P2 — Attenuation monotonicity.** For any chain, `authority(child) ⊆ authority(parent)`.
  No block append can widen scope, raise a budget ceiling, extend expiry, or increase depth.
- **P3 — Offline attenuation.** Producing a child token requires no network, no issuer
  round-trip, and no shared mutable state.
- **P4 — Budget confinement.** For every `(mandate_id, dimension)`:
  `committed + leased + allocated ≤ total`, and therefore `Σ spend ≤ mandate`, under
  arbitrary concurrency, partition, and PEP compromise (A4).
- **P5 — Revocation.** After a revocation is published, every PEP denies the revoked token and
  its entire subtree within a bounded delay.
- **P6 — Non-repudiation of spend.** Every authorized action produces an append-only,
  hash-chained record binding the action to the deciding caveat, the agent, and the human
  principal; any alteration or reordering of records is detectable.
- **P7 — Intent binding.** A token is usable only for the task it was approved for; a request
  whose intent does not match the token's is denied.
- **P8 — Explainability.** Every deny names a specific cause — a caveat, a policy statement,
  a budget, or a reason code — deterministically chosen.
- **P9 — Fail-closed.** Unavailability of any control-plane dependency yields deny, not allow.
- **P10 — Depth bound.** No chain authorizes beyond `max_depth`, and depth is computed by the
  verifier, never claimed by the token.

---

## 5. Property → enforcement mapping

Every row is either enforced at a named location, or marked. `✓` = enforced and covered by a
test; `◐` = enforced with a stated residual; `✗` = claimed somewhere in project materials but
**not enforced** on the deployed path.

### P1 — Token unforgeability ✓ (holds against A1, A3; fails against A6 by construction)

| Mechanism | Location |
|---|---|
| Per-block Ed25519 signature over the preceding chain; parse fails on any tamper | [`tokens.py:210` `_parse()`](../packages/agentiam-core/src/agentiam_core/tokens.py#L210) |
| Multi-key acceptance set for rotation without invalidating in-flight tokens | [`tokens.py:71` `RootKeySet`](../packages/agentiam-core/src/agentiam_core/tokens.py#L71) |
| Mandatory authority facts; absence is a malformed token, not a default | [`tokens.py:300` `_require()`](../packages/agentiam-core/src/agentiam_core/tokens.py#L300) |
| Size guard *before* parse cost, bounding DoS via oversized chains | [`tokens.py:340`](../packages/agentiam-core/src/agentiam_core/tokens.py#L340) |

Evidence: `tests/security/test_redteam_suite.py` (A-01…A-04 — wrong key, bit flip, truncation,
splice). Rests on assumption **A2** (Ed25519/SHA-256 soundness); no cryptography is
implemented in this project.

### P2 — Attenuation monotonicity ✓ (holds against A1)

| Mechanism | Location |
|---|---|
| **The actual guarantee** — biscuit blocks are append-only and every clause in every block must pass, so authority is an intersection and appending can only shrink it | Library structure, not project code. Assumption **A1** in `docs/threat-model.md` §4 |
| Mint-time refusal of a widening caveat (legibility, not safety) | [`attenuation.py:260` `check_narrowing()`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L260) |
| Narrowing partial order over nine closed caveat kinds | [`attenuation.py:144` `narrows()`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L144) |
| Child mint; refuses on widening or size overflow, producing no token | [`attenuation.py:289` `attenuate()`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L289) |

Evidence: `tests/property/test_attenuation.py` — P-01 (`authority(child) ⊆ authority(parent)`),
P-02 (transitive), P-03/P-04 (reflexive, transitive `narrows`), P-09 (deny precedence),
Hypothesis-generated.

> **Precision worth keeping in the paper.** The source itself states that the security does
> *not* live in `narrows()` ([`attenuation.py:9-24`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L9)):
> a `narrows()` bug yields a *misleading* token, not an over-privileged one, because the
> intersection holds regardless. This is a stronger and more defensible claim than "we check
> narrowing at mint time," and it should be the one made.

> **Load-bearing dependency.** P2 reduces entirely to biscuit scoping block facts such that a
> later block's facts are invisible to an earlier block's checks (assumption **A1**). This is
> verified by hand against the library, **not by CI**, and must be re-verified on any
> `biscuit-python` upgrade. Automating it is `docs/STATUS.md` §4.1's own proposal. For a paper,
> this is the single assumption to state most prominently.

### P3 — Offline attenuation ✓

| Mechanism | Location |
|---|---|
| `attenuate()` takes a `VerifiedToken` and returns base64; no I/O in the module | [`attenuation.py:289`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L289) |
| Purity enforced statically across the whole core package | `tests/unit/test_core_purity.py` |

Evidence: P-05 in `tests/property/test_attenuation.py::TestOfflineAttenuation`. This is the
clearest differentiator from OAuth-family designs and the cheapest to demonstrate.

### P4 — Budget confinement ✓ against A1 · ◐ against A4 (see the boundary note below)

> **Correction.** An earlier draft of this document claimed P4 holds against A4 (compromised
> PEP) outright. That is true of the **protocol** and not of the **deployment**, and the
> difference was found by tracing the deployed composition root rather than reading the spec.
> `scripts/pep_service.py` requires `AGENTIAM_PEP_DATABASE_URL` and opens sessions **directly
> against Postgres** ([`pep_service.py:494`](../scripts/pep_service.py#L494)); `LedgerClient`
> ([`pool.py:61`](../packages/agentiam-pep/src/agentiam_pep/pool.py#L61)) is a Protocol whose
> only production implementation is that direct-database one. **Trust boundary B2 is a library
> call, not a boundary.** So G2/G3 bind only while the PEP chooses to call `ledger.py`.
>
> What still holds: `ck_budgets_invariant` is a real database `CHECK`
> ([`db/models.py:76`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/models.py#L76)),
> so arbitrary SQL cannot make `committed + leased + allocated > total` — Postgres refuses the
> transaction. What does not: **`total` is an ordinary mutable column**, so a compromised PEP
> raises the ceiling the invariant is relative to, or zeroes `committed` (which satisfies the
> constraint). Precisely:
>
> - ✅ **against a PEP that follows the protocol and lies about amounts** — G2 clamps, G3
>   refuses. The realistic threat, and genuinely proved.
> - ✅ **the invariant's shape against arbitrary SQL** — via the `CHECK`.
> - ❌ **against arbitrary code execution in a PEP** — it can move `total`.
>
> A restricted Postgres role (no `UPDATE` on `budgets.total`) closes the gap for hours of work.
> See [`lease-plan.md`](lease-plan.md) GAP-A. **Do not claim A4-resistance until it lands.**

| Guard | Purpose | Location |
|---|---|---|
| **G1** `SELECT … FOR UPDATE` on the budget row serializes the read of `available` with its write | Prevents overspend (measured without it: 160 granted against a total of 100) | [`ledger.py:176`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L176) in [`acquire()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L129) |
| **G2** commit amount clamped to `lease.outstanding` | Keeps `leased ≥ 0` **even if the PEP lies** — this is what makes the claim hold at A4 | [`ledger.py:239` `ledger_commit()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L239) |
| **G3** commits against a non-active lease refused, recorded as an anomaly | Prevents double-decrement after `RELEASE`/`REAP` (measured: `leased` negative in 55/400 interleavings without it) | same |
| **G4** dedup on `reservation_id` | Idempotency; keeps `committed` accurate under retry | same |
| Batch form preserving per-item G2/G3/G4 with a *cumulative* clamp | Throughput without weakening the guards | [`ledger.py:274` `ledger_commit_batch()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L274) |
| `allocated` column joins the invariant, so promised-to-a-child budget cannot be re-leased | Sibling confinement under proportional split | [`ledger.py:57` `split_budget()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/ledger.py#L57) |
| Schema `CHECK` constraints enforce the invariant in the database, not only in application code | Defence in depth | `db/models.py`, `test_lease_schema.py` |
| Settlement actually reaches the ledger (this had no production caller until T-052) | Without it, `RELEASE` returned spent budget to the pool — the same money twice | [`settlement.py:142` `SettlementQueue`](../packages/agentiam-pep/src/agentiam_pep/settlement.py#L142) |
| Expired leases reclaimed on a schedule | Liveness of the pool | [`app.py:175` `_reaper_lifespan()`](../packages/agentiam-controlplane/src/agentiam_controlplane/app.py#L175), wired at [`app.py:969`](../packages/agentiam-controlplane/src/agentiam_controlplane/app.py#L969) |

**Proof status — state this carefully.** There is a written safety argument
(`docs/specs/04-lease-protocol.md` §6): with `Φ = committed + leased`, only `ACQUIRE` increases
Φ and it does so by at most `total − Φ` under G1, therefore `Φ ≤ total` always. That is a valid
hand proof over the specified operations. What backs it empirically:

- a protocol model run over **400 random interleavings**, with each guard ablated in turn
  (`docs/specs/04-lease-protocol.md` §15);
- a stateful Hypothesis machine against real Postgres
  (`tests/integration/test_ledger_properties.py`), scoped to `acquire`/`release`/`expire`/
  `ledger_commit` — the module docstring states plainly that `revoke` has no rule and
  `reserve`/`commit`/`refund` are covered elsewhere because they are PEP-local and pure;
- a 50-concurrent-acquire test against real Postgres;
- chaos scenarios CH-1…CH-10 with an invariant sidecar.

**This is randomized testing plus a hand proof, not model checking.** The word "model-checked"
appears in the spec header, and the artifact is a throwaway script no longer in the
repository — it is not a TLA+/Spin model and there is no exhaustive state-space claim. A
reviewer will check this. See `gap-analysis.md` G-2; the honest phrasing is *"hand-proved and
validated by randomized differential testing with guard ablation."*

### P5 — Revocation ◐

| Mechanism | Location |
|---|---|
| Step 3 of the decision, before any other authority check; any ancestor block id in the set kills the whole chain | [`decision.py:259`](../packages/agentiam-core/src/agentiam_core/decision.py#L259) |
| Redis pub/sub fast path + periodic full-set pull as correctness backstop | [`revocation.py` `RedisRevocationSet`](../packages/agentiam-pep/src/agentiam_pep/revocation.py) |
| Counting Bloom filter as first check; positives fall through to the authoritative exact set (zero false denials by construction) | same |
| Distinct reason codes for self vs. ancestor revocation | [`decision.py:268`](../packages/agentiam-core/src/agentiam_core/decision.py#L268) |

Evidence: `tests/integration/test_revocation_nfr4.py` (3 PEP instances, NFR-4's < 2 s p99),
`tests/integration/test_subtree_revocation.py` (depth-4 tree of 12 agents; siblings survive).

**Residual:** revocation is only as fast as propagation, and a stolen token works until it
arrives (TM-01). The bound is a *latency* bound, not an immediacy guarantee — the property is
"eventually and quickly," not "at once."

### P6 — Non-repudiation of spend ◐

| Mechanism | Location |
|---|---|
| Hash chain: each record binds the previous hash inside the hashed structure | [`audit.py:101` `append()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/audit.py#L101) |
| Head row locked so `seq` is monotonic and `prev_hash` correct under concurrency | [`audit.py:84` `_head()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/audit.py#L84) |
| Verification recomputes and reports the first inconsistent `seq` | [`audit.py:173` `verify_chain()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/audit.py#L173) |
| Per-task custody query, binding actions to the approving human | [`audit.py:231` `custody()`](../packages/agentiam-controlplane/src/agentiam_controlplane/db/audit.py#L231) |
| Canonical serialization — sorted keys, NFC, fixed-4-place `Decimal`, floats rejected | [`hashing.py`](../packages/agentiam-core/src/agentiam_core/hashing.py) |
| Record emitted **before** the upstream call, so a full audit buffer denies rather than forwards | [`pipeline.py:388`](../packages/agentiam-pep/src/agentiam_pep/pipeline.py#L388) |

Evidence: `tests/property/test_canonical_serialization.py`, A-30 (audit tamper detection),
NFR-6 property test.

**Residual — the honest scope of "non-repudiation."** This is *tamper-evidence against an
adversary without database write access*, not non-repudiation in the cryptographic sense.
The chain is not signed and not externally anchored (no notarization, no transparency log),
so an adversary at **A5** who controls the database can recompute the entire chain and leave
no trace. Against A1 and A4 it holds. The distinction matters in a paper; "non-repudiation"
overclaims it and "tamper-evident append-only ledger" is both accurate and still a real
contribution.

> **This is no longer only a wording question.** HDP (arXiv:2604.04522, April 2026) records
> each delegation hop as an **Ed25519-signed** entry in an append-only chain, verifiable by
> any party holding the issuer's public key, with no registry lookup. A reviewer aware of it
> will ask why AgentIAM's chain is unsigned, and "non-repudiation" claimed next to it will
> read as overclaiming rather than imprecision. Two options, both defensible: **sign the
> chain**, or **rename the property and cite HDP**. See
> [`related-work.md`](related-work.md) §4.3.

### P7 — Intent binding ✓ (with a caveat on what "intent" means)

| Mechanism | Location |
|---|---|
| `intent_hash` bound into the authority block at mint | [`tokens.py:157`](../packages/agentiam-core/src/agentiam_core/tokens.py#L157) |
| Request intent checked against the token's, **ordered before the caveat loop** so a wrong task reports `INTENT_MISMATCH` rather than an arbitrary caveat failure | [`decision.py:299`](../packages/agentiam-core/src/agentiam_core/decision.py#L299) |
| Immutable under attenuation | P-07, `tests/property/test_attenuation.py` |

**This check existed in the token and was enforced nowhere** until T-023 — TM-27 in the
STRIDE register documents the measurement: a request whose `request_intent` did not match was
*allowed* by `decide()` while biscuit denied the identical request. It is enforced now, and
the episode is the strongest available illustration of a general point worth making in the
paper: *a capability check that no code path evaluates is decoration.* See `gap-analysis.md`.

### P8 — Explainability ✓

| Mechanism | Location |
|---|---|
| First-failing-step-wins, in fixed step order — deterministic cause selection | [`decision.py:228` `decide()`](../packages/agentiam-core/src/agentiam_core/decision.py#L228), rationale at [`decision.py:1-22`](../packages/agentiam-core/src/agentiam_core/decision.py#L1) |
| Failing caveat carried on the decision, by reference | [`decision.py:156` `_deny()`](../packages/agentiam-core/src/agentiam_core/decision.py#L156) |
| Token-error → reason-code table, so verify-stage failures are equally precise | [`decision.py:164`](../packages/agentiam-core/src/agentiam_core/decision.py#L164) |

The design note at `decision.py:5-11` is worth quoting in the paper: evaluating everything and
reporting the most severe cause "sounds more informative and is not — it lets a revoked token
be reported as `SCOPE_NOT_GRANTED`, and the operator spends an afternoon adjusting scopes on a
credential that was killed hours ago."

### P9 — Fail-closed ◐

| Mechanism | Location |
|---|---|
| Unavailable revocation oracle → deny | [`decision.py:272`](../packages/agentiam-core/src/agentiam_core/decision.py#L272) |
| Unavailable policy → deny | [`decision.py:340`](../packages/agentiam-core/src/agentiam_core/decision.py#L340) |
| Unavailable lease pool → deny | [`decision.py:393`](../packages/agentiam-core/src/agentiam_core/decision.py#L393) |
| Missing/invalid `Bearer` scheme → deny (RFC 6750 §2.1; previously tolerated a bare token) | [`pipeline.py:278`](../packages/agentiam-pep/src/agentiam_pep/pipeline.py#L278) |
| Audit buffer full → deny rather than forward | [`pipeline.py:388`](../packages/agentiam-pep/src/agentiam_pep/pipeline.py#L388) |

**Residual:** `POLICY_BUNDLE_STALE` is **unreachable in a deployed PEP** — `scripts/pep_service.py`
loads a signed bundle once from disk and bypasses `PolicyCache` entirely, because no bundle-
publishing service exists (`docs/STATUS.md` gap 26). Signature and tamper guarantees hold;
staleness detection and hot reload do not. This is a fail-closed guard that is present in the
code and not on the deployed path — exactly the class of defect TM-27 was.

### P10 — Depth bound ✓

| Mechanism | Location |
|---|---|
| Depth computed from `biscuit.block_count()`, never read from a block fact | [`tokens.py:369`](../packages/agentiam-core/src/agentiam_core/tokens.py#L369) |
| Verifier-supplied `current_depth` re-checked inside `decide()` | [`decision.py:307`](../packages/agentiam-core/src/agentiam_core/decision.py#L307) |
| `declared_depth` in the child block is explicitly advisory only (ADR-005) | [`attenuation.py:~333`](../packages/agentiam-core/src/agentiam_core/attenuation.py#L289) |

Evidence: P-06, `tests/property/test_attenuation.py`.

---

## 6. Properties claimed but **not** enforced

Stated separately so they cannot be read as guarantees.

| # | Claim appearing in project materials | Reality | Where |
|---|---|---|---|
| **N1** | Token possession implies the legitimate holder | **False by design.** Bearer semantics: a stolen token works until expiry or revocation. Proof-of-possession is future work | TM-01, accepted risk, `docs/threat-model.md` §5.1 |
| **N2** | Drift detection catches off-task behaviour | **Partially.** The deployed oracle is `RuleBasedDriftOracle` — a v0 heuristic. Features f1/f2/f5 are computed and recorded; **f3/f4/f6 are deferred** (ADR-036) and the trained classifier (T-035) and labelled dataset (T-034) are **deferred entirely**. NFR-9's < 5% FPR is therefore **unmeasured** | [`drift.py`](../packages/agentiam-pep/src/agentiam_pep/drift.py), [`drift_features.py:4-5`](../packages/agentiam-core/src/agentiam_core/drift_features.py#L4) |
| **N3** | Slow-drift evasion is handled | **No.** An adversary drifting gradually stays under a per-action threshold. Trajectory-level scoring is future work | TM-11, accepted risk |
| **N4** | The agent's reported spend is trustworthy | **No.** Over-reporting is clamped by G2; under-reporting where the PEP cannot cross-check is flagged, not prevented | TM-23, accepted risk |
| **N5** | A confused deputy is prevented | **Partially.** A higher-privileged agent induced to act is bounded by *its own* authority, which may exceed what the requester should reach | TM-05, partial |
| **N6** | Clock skew is handled | **Partially.** Safety depends on actual skew staying within the configured margin `S`, with `ttl > 2S`. Outside `S`, budget can be re-issued while still held | TM-22, partial |
| **N7** | Policy bundle staleness is enforced in production | **No** — unreachable on the deployed path (P9 residual above) | `docs/STATUS.md` gap 26 |
| **N8** | A deployed PEP enforces budget for concurrent mandates | **No.** `LeasePool` binds one `mandate_id` at construction; a deployed PEP serves exactly one mandate at a time | `docs/STATUS.md` gap 25 |
| **N9** | Agent roles are attested | **No.** Roles come from a static JSON map of delegation path → role, read at boot. They do not rotate, expire, or attest; adding an agent needs a file edit and a restart | `docs/STATUS.md` gap 28, ADR-069 |
| **N10** | Timing side channels on deny reasons are closed | **Partially.** Response shape is uniform; a measured timing difference between deny paths is reported rather than claimed absent | TM-16 |

---

## 7. Assumptions

Restated from `docs/threat-model.md` §4 because a property is only as strong as its
assumptions, and these belong next to the properties they carry.

| # | Assumption | Carries | If it fails |
|---|---|---|---|
| **A1** | Biscuit scopes block facts: a later block's facts are invisible to earlier blocks' checks | **P2, P10** | Total collapse of attenuation — any child could widen authority. **Verified by hand, not by CI; re-verify on every `biscuit-python` upgrade** |
| **A2** | Ed25519 and SHA-256 are sound | P1, P6 | Total compromise. No cryptography is implemented here |
| **A3** | The root private key is not exfiltrated | P1 | Arbitrary authority minted |
| **A4** | Clock skew between PEP and ledger stays within `S` | P4 | Overspend within the excess window (TM-22) |
| **A5** | Postgres serializes `SELECT … FOR UPDATE` correctly | P4 | Concurrent acquires overspend — measured at 160 against a total of 100 without G1 |
| **A6** | The control plane's database is not attacker-controlled | P6 | Audit chain recomputable; tamper-evidence lost |

---

## 8. What a reviewer will attack first

Ordered by how much damage the objection does.

1. **"Your threat model assumes the agent is adversarial but the PEP is not — why is that the
   right line?"** Draw the line **per-property and per-capability**, and be exact, because the
   honest answer is partly a concession. For P4 against a PEP that follows the protocol and
   lies about amounts, G2/G3 hold and spec 04 §6 proves it — lead with that. But the deployed
   PEP holds database credentials, so against *arbitrary code execution* it can move `total`
   (see P4's boundary note). Concede that, and say what closes it: a restricted database role,
   or a ledger service. A reviewer who finds this themselves will discount everything else;
   a reviewer who is handed it will believe the rest.
2. **"'Model-checked' — with what?"** 400 random interleavings with guard ablation, plus a
   hand proof. Reword before submission (§5, P4).
3. **"Non-repudiation without signed or anchored records?"** Concede and rename to
   tamper-evidence (§5, P6).
4. **"NFR-2 is unproven."** Already conceded in `docs/benchmarks/performance.md`, which states
   the 500 RPS profile could not be offered on the test host and the 100 RPS p99 straddles the
   budget by 10× in both directions. Leading with this concession is far stronger than being
   caught by it.
5. **"Drift detection is a rule-based v0 with no calibration."** True; N2 above. Either scope
   it out of the paper's claims or report it as negative/preliminary.
6. **"A1 is one library behaviour holding up the entire design, checked by hand."** The most
   serious unglamorous objection. Automating it is the highest-value pre-submission fix.

---

## 9. Out of scope

Model-level safety (whether the LLM *should* take an action), data exfiltration through
permitted channels, side channels beyond N10, supply-chain compromise of dependencies (scanned
via `make security`, not modelled here), and compromise of the control plane itself (A5/A6).
