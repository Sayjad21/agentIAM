# AgentIAM — Related Work and Positioning

**Purpose.** Locate AgentIAM in the literature, and state precisely which of its claims are
novel as of September 2026 and which have been overtaken.

## Method and citation confidence

Two tiers, marked throughout:

- **[✓ verified 2026-09-26]** — I retrieved the record in this session and confirmed title,
  author, identifier and date.
- **[VERIFY]** — the work is named here from one of two sources: my own background knowledge,
  or a prior literature survey found in this repository's git stash (`git show
  45a6a67:docs/research.md`, dated 2026-08-21, **not present in the working tree**). I did not
  re-retrieve these. Confirm identifier, year and venue before any submission.

> **A note on that stashed survey.** It is a substantial and careful document — 13 sections,
> a scored positioning analysis, and an explicit account of its own instruments' failures. It
> was **stashed rather than committed**, so it was one `git stash drop` from being lost. It is
> now preserved verbatim at [`prior-survey-2026-08-21.md`](prior-survey-2026-08-21.md); the
> stash is untouched. Its central conclusion is revised below (§6).

---

## 1. The comparison table

Required reading is the last column: what the system lacks that AgentIAM supplies.

| System | Mechanism | Delegation model | Quantitative limits | What it lacks that AgentIAM adds |
|---|---|---|---|---|
| **Macaroons** [✓] | Nested HMAC chains; caveats appended by any holder | **Offline attenuation** — the direct ancestor of AgentIAM's model | None. Caveats are predicates, not counters | Public-key verification (macaroons need the shared secret at the verifier); no ledger, so no ceiling that survives concurrency; no budget dimension at all |
| **Biscuit** [VERIFY] | Ed25519-signed append-only blocks + Datalog | Offline attenuation, public-key verifiable | None in the format | **This is AgentIAM's substrate, not a competitor.** AgentIAM adds the quantitative layer, the ledger, the lease protocol, and the audit chain |
| **UCAN** [◐ no finalized 1.0] | DID-keyed, JWT-encoded capability chains | Offline delegation, decentralized issuers | None | Quantitative ceilings; a ledger; enforcement under concurrency; an audit chain |
| **SPKI/SDSI** (RFC 2693) [VERIFY] | Authorization certificates, name linking, delegation bit | Offline chain delegation | None | Same three; plus a modern agent-shaped deployment story |
| **SPIFFE/SPIRE** [VERIFY] | SVIDs (X.509/JWT) issued after **workload attestation** | **None** — identity only; authorization is out of scope by design | None | Delegation, attenuation, and authorization entirely. *Complementary rather than competing* — SPIFFE answers "which workload is this," AgentIAM answers "what may it spend." A deployment could use both |
| **OAuth 2.0 Token Exchange** (RFC 8693) [✓] | Issuer mints a new, possibly narrower token | **Issuer round-trip required** — the defining contrast | None | Offline attenuation; the issuer is on the critical path for every narrowing, and unavailable means no delegation |
| **OAuth DPoP / mTLS-bound** (RFC 9449 / RFC 8705) [✓] | Proof-of-possession binding | n/a | None | **AgentIAM lacks what *these* have** — see §5. This row runs the other way |
| **MCP authorization** [✓] | OAuth 2.1 resource server; RFC 8707 resource indicators; bearer scopes | **None** — no delegation, no attenuation | None | Everything: sub-agent delegation, attenuation, budgets, custody. MCP explicitly makes authorization OPTIONAL and scopes it to transport-level token validation |
| **Cedar** [✓] | Policy language; analyzable, fast evaluation | n/a | None | **Used by AgentIAM, not competing.** Cedar is step 5 of the decision pipeline |
| **Zanzibar** [VERIFY] | Relationship-based ACLs at scale | Centralized; consistency tokens | None | Offline verification; delegation by the holder; quantitative bounds. Different problem (who-relates-to-what at read scale) |
| **AIP / IBCT** [✓] | **Biscuit + Datalog**, append-only chains, over MCP/A2A | **Offline attenuation — the same technique, the same library** | **None reported** | Quantitative ceilings; concurrency; partition; ledger; crash recovery. **The direct collision — see §4.1** |
| **Agent Contracts** [✓] | Formal contracts with multi-dimensional resource constraints and **conservation laws under delegation**; enforcement by a **centralized external monitor** | Hierarchical contract delegation | **Yes — multi-dimensional** | **Distribution, entirely.** Verified against the full text: single coordination framework, no partition, no crash recovery, no clock skew, no lease/TTL, no cross-process ledger. Its `Σ Rᵢ ≤ R_parent` is the *static split* half of INV-5; it has no dynamic shared-pool-under-concurrency half. **Second collision — see §4.2** |
| **HDP** [✓] | Ed25519-signed **append-only delegation chain** binding a human authorization event to each agent hop; offline verification, no registry | Multi-hop, provenance-focused | None | Quantitative ceilings; enforcement of any kind (it is a provenance protocol, not a PEP). **But it signs every hop, which AgentIAM's audit chain does not** — see §4.3 |
| **APort Vault** [✓] | Public benchmark for **agent payment authorization**; 4,371 human-written CTF attacks, 225,964 evaluations, released on HuggingFace | n/a — evaluation harness | **No — recipient allowlisting only, explicitly not spend caps** | Nothing; it is an opportunity, not a competitor. **The benchmark AgentIAM lacks, in AgentIAM's exact domain, with the money dimension left open** — see §4.4 |
| **MasDrift** [✓] | Benchmark for **authorization preservation across delegation**; 600 tasks, 8 domains, 3 coordination styles, varying depth/width | Tests delegation chains directly | None | Also an opportunity. One of its two evaluated defenses is "carrying an **attenuated policy** along the delegation chain" — AgentIAM's mechanism, already baselined — see §4.4 |
| **Token Budgets** [✓] | **Affine types in Rust**; compile-time ownership of cost-bearing values | Delegation-fanout handled by the borrow checker | **Yes — token/cost caps** | Distribution. Single-process compile-time ownership; explicitly contrasts with asyncio overshoot rather than solving it across hosts |
| **OAP** [✓] | Synchronous pre-execution interception; signed audit record | Policy-based, not capability-based | Names spend limits; **no budget experiment reported** | Offline capability attenuation; measured budget enforcement. Its reported 53 ms median is ~150× AgentIAM's in-process decision — a favourable contrast worth making |
| **Prompt-injection defenses** (Progent, CaMeL, Task Shield, …) [VERIFY] | Plan/dataflow-level confinement, taint tracking, LLM-generated policy | Varies | None | All operate **above the byte layer**. See §5.2 — AgentIAM's parser-differential finding sits beneath all of them |

---

## 2. The classical lineage

**Macaroons** [✓ verified — Birgisson, Politz, Erlingsson, Taly, Vrable, Lentczner, *Macaroons:
Cookies with Contextual Caveats for Decentralized Authorization in the Cloud*, NDSS 2014] is the
intellectual parent. The core move — any holder may append a caveat that only restricts
authority, with no issuer involvement — is exactly AgentIAM's attenuation. Two differences
matter: macaroons use nested HMACs, so the verifier needs the shared secret, whereas biscuit
uses Ed25519 and verifies with a public key alone; and macaroon caveats are *predicates*, which
cannot express "at most ৳50,000 total across all holders," because that is a statement about
aggregate consumption rather than about any single request.

**That limitation is the whole of AgentIAM's thesis.** A caveat is evaluated per-request and
statelessly; a budget is inherently stateful and shared. No amount of caveat expressiveness
closes the gap — which is why the lease protocol and the ledger exist, and why they, not the
attenuation, are the contribution.

**UCAN** and **SPKI/SDSI** [VERIFY] occupy the same design space with different encodings (DIDs
and JWTs; authorization certificates). Neither adds quantitative limits. They belong in a
related-work section as evidence that offline delegation is well-trodden — which is an argument
*against* claiming it as novel.

**SPIFFE/SPIRE** [VERIFY] is frequently miscited as a competitor. It is not: it issues attested
workload identity and explicitly leaves authorization to the consumer. The honest framing is
complementarity — SPIFFE could supply the attestation that AgentIAM's agent roles currently
lack (`threat-model.md` N9, `docs/STATUS.md` gap 28, where roles come from a static JSON file
requiring a restart). This is worth a sentence in the paper and possibly a future-work item.

---

## 3. The OAuth family, and the sentence to stop repeating

`README.md` argues at length that OAuth cannot express "this sub-agent may read invoices and
spend nothing." The argument is correct and **already published** — the stashed survey names
*OAuth Is Not Enough* (Chopra, 2025) [VERIFY] making the same case. Cite it and move on;
re-arguing it costs half a page and signals unfamiliarity with the field.

The precise technical contrast worth keeping is narrow and sharp:

- **RFC 8693 token exchange** can produce a narrower token, but the **issuer must be reached**.
  Under partition, delegation stops. AgentIAM's attenuation is a local computation over a token
  already held — `attenuate()` performs no I/O, enforced statically by
  `tests/unit/test_core_purity.py`.
- **Bearer semantics are shared**, and this cuts against AgentIAM. RFC 9449 (DPoP) and RFC 8705
  (mTLS-bound tokens) [VERIFY] are deployed answers to token theft that AgentIAM **does not
  implement** (TM-01, accepted risk). A reviewer familiar with OAuth will ask why not, and
  "documented future work" is a weaker answer than acknowledging that the mechanism exists and
  is well understood.

**MCP authorization** [✓ verified — `modelcontextprotocol.io/specification/2025-11-25/basic/authorization`,
plus a 2026-07-28 revision] makes the MCP server an OAuth 2.1 resource server validating tokens
from an external authorization server, with RFC 8707 resource indicators. Authorization is
OPTIONAL. There is no delegation model, no attenuation, and no quantitative limit — so the gap
AgentIAM targets is real at the protocol level. Note the spec revises frequently; pin the
revision date you cite.

---

## 4. The collisions — read these before writing the paper

### 4.1 AIP pre-empts the attenuation contribution

[✓ verified] **Sunil Prakash, *AIP: Agent Identity Protocol for Verifiable Delegation Across MCP
and A2A*, arXiv:2603.24775, submitted 25 March 2026.** Also published as IETF Internet-Draft
**`draft-prakash-aip-00`** — a fact the stashed survey does not record, and which raises the
work's standing considerably: it is a standards-track proposal, not only a preprint.

Invocation-Bound Capability Tokens fuse identity, attenuated authorization and provenance. Two
modes: compact JWT/Ed25519 for single-hop, and **chained mode using Biscuit tokens with
append-only blocks and Datalog policy evaluation** for multi-hop delegation. Bindings for MCP,
A2A and generic HTTP. Python *and* Rust implementations. The survey records verification at
0.049 ms (Rust) / 0.189 ms (Python), 0.22 ms overhead in MCP-over-HTTP, and 600 adversarial
attempts at 100% rejection [VERIFY these figures against the paper].

**Same library, same technique, same transport.** AgentIAM's attenuation, depth bound, offline
verification and adversarial-evaluation framing are all pre-empted. What AIP does *not* appear
to have: any quantitative resource dimension, ledger, concurrency analysis, or partition model.

### 4.2 The budget gap has narrowed since the August survey

**This is the finding that most changes the plan.** The stashed survey's central conclusion —
that quantitative/lease enforcement is "the genuine gap," corroborated four ways — was drawn on
2026-08-21. Two works that bear directly on it **predate that survey and were missed by it**
(its §3.6 acknowledges instrument failures on exactly these query framings):

- [✓ verified] **Agent Contracts: A Formal Framework for Resource-Bounded Autonomous AI
  Systems**, arXiv:2601.08815, January 2026, **accepted for oral presentation at COINE 2026,
  co-located with AAMAS 2026**. Unifies I/O specifications, **multi-dimensional resource
  constraints**, temporal boundaries and success criteria. For multi-agent coordination it
  "establishes **conservation laws ensuring delegated budgets respect parent constraints**,
  enabling hierarchical coordination through contract delegation." Reports **zero conservation
  violations** and detection of runaway agents.

  That is the same claim shape as AgentIAM's INV-5 (budget subadditivity) and the Φ potential
  function in `docs/specs/04-lease-protocol.md` §6. It is peer-reviewed and AgentIAM is not.

  **But I read the full text (`arxiv.org/html/2601.08815v3`), and the collision is narrower
  than the abstract suggests.** Enforcement is by "an external monitor [that] tracks
  consumption after each action and halts execution when constraints are breached" — a
  **centralized monitor inside a single coordination framework**. There is no partition model,
  no crash recovery, no clock skew, no lease or TTL, and no cross-process ledger. Its
  conservation law `Σ Rᵢ ≤ R_parent` is the **static allocation** constraint — the equivalent
  of AgentIAM's `split_budget()` proportional-split mode — and it has **no equivalent of the
  shared-pool-under-concurrency mode** that spec 04 §13 makes AgentIAM's *default*, which is
  precisely where the `FOR UPDATE` serialization and the 160-against-100 ablation live.
  Evaluation is 50–70 trials per experiment on a local three-agent pipeline (Google ADK /
  LiteLLM).

  **So: it takes the conservation-law framing, not the distributed enforcement.** Cite it as
  closest prior work on the algebra; distinguish on the systems half.

- [✓ verified] **Token Budgets: An Empirical Catalog of 63 LLM-Agent Budget-Overrun Incidents,
  with an Affine-Typed Rust Mitigation as a Case Study**, Sajjad Khan, arXiv:2606.04056, June
  2026. 63 production incidents across 21 frameworks; eight-cluster taxonomy (κ = 0.837); live
  testing across five runtimes and three providers (N = 160); zero cap violations, zero false
  refusals. Affine ownership in Rust gives "no aliasing, **no double-spend**, no
  use-after-delegation of a cost-bearing value." The **delegation-fanout race** is rejected at
  compile time, "while the same pattern under asyncio overshoots 30/30."

  That delegation-fanout race is precisely the double-spend AgentIAM's own chaos suite found in
  T-052 (gap 23: settlement never reached the ledger, so `RELEASE` returned spent budget to the
  pool). An independent catalog of 63 such incidents is **excellent motivation** — and it also
  means the problem is no longer unnamed.

- [✓ verified] **Delegation Without Trust: An Empirical Gap Analysis of Identity, Authorization,
  and Runtime Governance in Multi-Agent LLM Systems**, Dantuluri & Sundi, arXiv:2609.00267,
  31 August 2026. Untrusted-model assumption; finds three of four frameworks (LangGraph, CrewAI,
  AutoGen, MCP) provide no built-in confinement. An authorization broker reduces reachable
  actions from 8,100 to a mean of 1.5 across 2,000 scenarios. **Does not address budgets** —
  so it corroborates the confinement framing without taking the quantitative claim.

- [✓ verified] **Authorization Propagation in Multi-Agent AI Systems: Identity Governance as
  Infrastructure**, Tallam, arXiv:2605.05440, May 2026. Formalizes authorization propagation as
  a workflow-level property with three sub-problems and seven structural requirements. The
  stashed survey's most valuable retrieval: it reportedly surveys quantitative limits and names
  **execution-count revocation** as the state of the art [VERIFY this specific claim in the
  text]. If accurate, it remains the best external corroboration that *money*, multi-dimensional
  and under concurrency, is not covered — but it must now be read alongside Agent Contracts.

Also surfaced and worth checking: **MasDrift: Benchmarking Authorization Preservation Across
Multi-Agent Architectures**, arXiv:2608.07556 [VERIFY] — if it is a benchmark for authorization
preservation, it may be the shared evaluation currency AgentIAM currently lacks (§5.1).

### 4.3 HDP collides with the chain-of-custody claim — and clears a higher bar

[✓ verified] **Asiri Dalugoda, *HDP: A Lightweight Cryptographic Protocol for Human Delegation
Provenance in Agentic AI Systems*, arXiv:2604.04522, April 2026.**

An HDP token "binds a human authorization event to a session, records each agent's delegation
action as a **signed hop in an append-only chain**, and enables any participant to verify the
full provenance record using only the issuer's Ed25519 public key and the current session
identifier" — offline, with no registry lookup or third-party trust anchor. It explicitly
positions against OAuth 2.0 Token Exchange, JWT, UCAN and the Intent Provenance Protocol.

This is AgentIAM's "chain of custody back to the human who approved the task," and it is
**cryptographically stronger**: HDP *signs* each hop, so its chain is verifiable by any party
holding a public key. AgentIAM's audit chain is hashed but **unsigned and unanchored**, so it
is tamper-evident only against an adversary without database write access (see
[`threat-model.md`](threat-model.md) §5, P6). HDP is the concrete evidence that "signed
provenance" is the expected bar in this space, and it makes the P6 rename from
"non-repudiation" to "tamper-evidence" not merely honest but necessary — a reviewer who knows
HDP will make that distinction unprompted.

**Implication:** either sign the audit chain, or drop custody from the contribution list and
cite HDP. Do not claim non-repudiation alongside it.

### 4.4 The benchmark blocker is now solvable — two public harnesses exist

The stashed survey's sharpest criticism of AgentIAM was that it has **no baselines and no
number on a shared benchmark**, and that a defense paper without one "does not get read." That
was true in August. Two benchmarks retrieved in this pass change it:

- [✓ verified] **APort Vault: Benchmarking AI Agent Payment Authorization with the Open Agent
  Passport**, arXiv:2609.22076, September 2026. Replays **4,371 human-written attacks** from a
  capture-the-flag event; **225,964 evaluations**; 14 models from 8 labs; 5 policy
  configurations. **Publicly released** — dataset, level passports, scoring code and analysis
  script at `huggingface.co/datasets/aporthq/vault-benchmark-v1`. Reported result: at
  authorization levels 2–4, the OAP layer prevented all 105 cross-matched unauthorized
  transfers, versus 140 with models alone.

  **The critical detail: it measures recipient allowlisting, and explicitly not spend limits,
  budget ceilings or monetary caps.** So it is a public, released, adversarial benchmark in
  *exactly* AgentIAM's domain — agent payments — with **the money dimension left open**.
  Running AgentIAM against it gives the missing baseline; **extending it with a spend-ceiling
  track is itself a contribution**, and a far better evaluation story than 22 hand-written
  attacks.

- [✓ verified] **MasDrift: Benchmarking Authorization Preservation Across Multi-Agent
  Architectures**, Xu, Zhang, Luo, Jin, Dong & Salam, arXiv:2608.07556, August 2026. 600 benign
  productivity tasks across eight domains, pairing required work with restricted actions;
  single-agent, centralized and decentralized coordination; varying hierarchy depth and network
  width. Reports task completion, unauthorized-action frequency, and the tradeoff between them.
  It compares two defenses — re-anchoring each pending call to the original user request, and
  **"carrying an attenuated policy along the delegation chain."**

  That second defense is AgentIAM's mechanism, **already baselined by someone else**. This is
  the natural harness for the attenuation half and the depth-bound claims, and it means
  AgentIAM can be compared rather than only described. [VERIFY] whether the dataset is publicly
  released — the abstract does not say.

**This is the single highest-value item in this document.** It converts the project's largest
review blocker from "needs six weeks of dataset construction" into "run two public harnesses."

### 4.5 The rest of the near lane

From the stashed survey, all **[VERIFY]**: **CapChain** (capability tokens for field-level state
access plus tamper-evident provenance; overlaps the audit chain), **OAP / Open Agent Passport**
(pre-action authorization, 53 ms median), **Delegation Without Escalation** (Hasbini — states the
composed-chain problem in nearly INV-2's terms), **Digital Identity for Agentic Systems**
(Madhira — typed constraint algebra overlapping `narrows()`), **SUDP** (secret-use delegation;
a useful model for presenting an invariant set).

The prompt-injection defense lane — **Progent**, **CaMeL/Fides**, **Task Shield**, **RTBAS**,
**AuthGraph**, **SecureClaw**, **APPA**, **IPIGuard** [VERIFY] — is saturated and
benchmark-disciplined, with attack-success-rate on **AgentDojo**, **ASB**, **TAMAS** or
**AgentLeak** [VERIFY] as its currency. AgentIAM's 22 hand-written attacks are not that number.
**Do not enter this lane** without one.

---

## 5. What is actually left

### 5.1 The honest contribution claim, revised

The stashed survey recommended: *"Quantitative Mandates for Delegated Autonomous Agents —
lease-based enforcement of spend ceilings under concurrency and partition."* After §4.2 that is
**too broad**. Agent Contracts has conservation laws under delegation; Token Budgets has
no-double-spend under delegation fanout.

What neither has, on the evidence retrieved, is **distribution**:

- Agent Contracts governs within a coordination framework. No partition model, no crash
  recovery, no clock skew, no lease reclamation, no cross-process ledger.
- Token Budgets enforces at **compile time in one process**. Its own contrast case is asyncio
  overshoot — a single-runtime concurrency problem, not a distributed one.

AgentIAM's remaining defensible novelty is the **distributed-systems half**: multi-dimensional
ceilings enforced across *independent enforcement points* that may crash, partition, or skew,
with a lease protocol carrying an explicit CP declaration, a TTL reaper, a clock-skew margin
(`ttl > 2S`), settle-before-release ordering, and a potential-function safety argument that
holds **even against a compromised enforcement point** (guards G2/G3 — see `threat-model.md`
§5, P4). That last property is genuinely unusual and is the sharpest single sentence available.

Suggested reframing: *"Enforcing quantitative mandates across untrusted, partition-prone
enforcement points — a lease protocol whose safety does not depend on enforcement-point
correctness."*

### 5.2 The two findings that may be worth more than the framing

Both are carried from the stashed survey's assessment, and both survive §4.2 intact:

1. **TM-26, the parser differential.** The PEP authorizes one value while the upstream acts on
   another, because two parsers read every request. Measured across Starlette, `parse_qsl`, Go,
   Java and PHP: `amount=1&amount=999999` yields `999999` under one and `1` under another. Every
   proxy-shaped agent authorization gateway in §4.3 inherits this, and none appear to mention
   it. The defenses in the prompt-injection lane all reason **above the byte layer** — a system
   checking `amount = 1` while the upstream executes `amount = 999999` is decorative, and its
   audit record is *honest about what it saw*. Short, sharp, and mean.
2. **TM-25, the library-default timeout.** `biscuit-python` 0.4.0 defaults its authorizer to
   `max_time = 1 ms` of **wall clock, not work**, so a legitimate request is refused for want of
   CPU scheduling — under exactly the load a 1 ms latency budget describes. 2 of 42,014 authorize
   calls hit it in a real run; 10 of 10 injected timeouts produced a **false invariant-violation
   report**. Anyone building on biscuit inherits this — including AIP (§4.1), which uses the same
   library. Worth a paragraph and an upstream report.

### 5.3 Blockers, re-scored

| Blocker (stashed survey, Aug 2026) | Status now |
|---|---|
| **No baselines** — every number is AgentIAM against AgentIAM | **Solvable, cheaply.** APort Vault is public and released; MasDrift already baselines the attenuated-policy defense (§4.4) |
| **No shared benchmark number** | **Solvable.** Two harnesses now exist in-domain. This was the hardest blocker and is now the easiest |
| **NFR-2 unestablished** — p99 straddles the budget ~10× both ways on a one-machine harness | **Unchanged.** Needs the generator off-box. `docs/benchmarks/performance.md` states it plainly |
| **Guard-ablation table rests on a model not in the repository** | **Unchanged.** Not reader-reproducible; see [`gap-analysis.md`](gap-analysis.md) G-2 |
| **Gap 27 — nothing scheduled `reap()`, so the liveness bound was false** | **Resolved.** `_reaper_lifespan` runs in the control plane (`app.py:175`, wired `app.py:969`, default 15 s = TTL/4). Verified by grep: no other production caller |
| *(new)* **Audit chain is unsigned** | **Newly material.** HDP signs every hop (§4.3); "non-repudiation" is not defensible alongside it |

---

## 6. Recommendation

1. **Run APort Vault first** (§4.4). It is public, released, adversarial, and in exactly your
   domain — and it leaves the money dimension open, which is your contribution. This is the
   highest value-per-hour action available to the research track, and it converts the largest
   review blocker into an afternoon's work plus a results table. **Do this before writing
   prose.**
2. **Read AIP (arXiv:2603.24775) and Agent Contracts (arXiv:2601.08815) in full before writing
   a word.** They bound what can be claimed. I have read enough of Agent Contracts to confirm
   it is single-process (§4.2); read AIP with the same question in mind.
3. **Demote attenuation to background.** Cite AIP, Macaroons and UCAN; claim nothing there.
4. **Narrow the quantitative claim to the distributed case** (§5.1), and cite Agent Contracts
   and Token Budgets as closest prior work rather than being caught not knowing them.
5. **Decide on custody**: sign the audit chain, or drop it from the contribution list and cite
   HDP (§4.3). Do not claim non-repudiation alongside a protocol that signs every hop.
6. **Preprint quickly.** The field went from empty to crowded in roughly eighteen months. Of
   the works that bound your claims, three appeared *after* the previous survey was written
   seven weeks ago — HDP (Apr), MasDrift (Aug), APort Vault (Sep). Assume more are in flight.

---

## 7. Bibliography

| Work | Identifier | Status |
|---|---|---|
| Birgisson et al., *Macaroons: Cookies with Contextual Caveats…* | NDSS 2014 | ✓ verified |
| Prakash, *AIP: Agent Identity Protocol…* | arXiv:2603.24775; IETF `draft-prakash-aip-00` | ✓ verified |
| *Agent Contracts: A Formal Framework for Resource-Bounded Autonomous AI Systems* | arXiv:2601.08815; COINE 2026 @ AAMAS 2026 | ✓ verified |
| Khan, *Token Budgets: …63 LLM-Agent Budget-Overrun Incidents…* | arXiv:2606.04056 | ✓ verified |
| Tallam, *Authorization Propagation in Multi-Agent AI Systems* | arXiv:2605.05440 | ✓ verified (specific "execution-count" claim [VERIFY]) |
| Dantuluri & Sundi, *Delegation Without Trust* | arXiv:2609.00267 | ✓ verified |
| MCP Authorization specification | `modelcontextprotocol.io`, rev. 2025-11-25 / 2026-07-28 | ✓ verified |
| Xu et al., *MasDrift: Benchmarking Authorization Preservation Across Multi-Agent Architectures* | arXiv:2608.07556 | ✓ verified (public release of dataset [VERIFY]) |
| *APort Vault: Benchmarking AI Agent Payment Authorization with the Open Agent Passport* | arXiv:2609.22076; dataset `huggingface.co/datasets/aporthq/vault-benchmark-v1` | ✓ verified |
| Dalugoda, *HDP: A Lightweight Cryptographic Protocol for Human Delegation Provenance* | arXiv:2604.04522 | ✓ verified |
| Uchibeke, *Before the Tool Call: Deterministic Pre-Action Authorization* (OAP) | arXiv:2603.20953 | ✓ verified |
| Cuoq et al. [VERIFY authors], *Cedar: A New Language for Expressive, Fast, Safe, and Analyzable Authorization* | OOPSLA 2024; PACMPL 8(OOPSLA1):118 | ✓ verified (venue/article) |
| *aiAuthZ: Off-Host, Identity-Bound Authorization for AI Agents* | arXiv:2607.05518 | [VERIFY] — surfaced, not yet read |
| *Reason Less, Verify More: Deterministic Gates…* | arXiv:2607.07405 | [VERIFY] — surfaced, not yet read |
| Biscuit token specification | biscuitsec.org | [VERIFY] version |
| UCAN specification | ucan.xyz; `github.com/ucan-wg/spec` | ✓ exists; **no finalized 1.0** — v0.5–v0.8.x referenced |
| SPKI Certificate Theory | RFC 2693 | [VERIFY] |
| SPIFFE / SPIRE | CNCF | [VERIFY] |
| OAuth 2.0 Token Exchange | RFC 8693 | ✓ verified |
| OAuth 2.0 DPoP | RFC 9449 | ✓ verified |
| OAuth 2.0 mTLS / certificate-bound tokens | RFC 8705 | ✓ verified |
| Resource Indicators for OAuth 2.0 | RFC 8707 | [VERIFY] |
| Zanzibar | USENIX ATC 2019 | [VERIFY] |
| Hardy, *The Confused Deputy* | ACM SIGOPS OSR, 1988 | [VERIFY] |
| Greshake et al., indirect prompt injection | AISec 2023 | [VERIFY] |
| AgentDojo / ASB / TAMAS / AgentLeak benchmarks | — | [VERIFY] all |
| CapChain · Progent · CaMeL/Fides · Task Shield · RTBAS · AuthGraph · SecureClaw · APPA · IPIGuard · SUDP · Madhira · Hasbini · Chopra | from stashed survey | [VERIFY] all |
