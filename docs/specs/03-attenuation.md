# Spec 03 — Attenuation Semantics and Invariants

**Status:** accepted · **Ticket:** T-003 · **Implements:** `PLAN.md` §6.3
**Depends on:** [`01-token-format.md`](01-token-format.md), [`02-caveat-language.md`](02-caveat-language.md)
**Consumed by:** T-009 (the most important ticket in the project), T-011, T-019

> This document is what T-009's property tests are written **from**. A property test derived
> from a wrong invariant statement proves nothing, so every invariant below is stated formally,
> given a mechanism, and given counterexamples describing what would violate it.

---

## 1. Definition

```
attenuate(T, C) → T′
```

Appends caveat set `C` to token `T`, producing `T′`. The operation is:

- **holder-side** — performed by whoever holds `T`, using a freshly generated ephemeral keypair
- **offline** — no issuer round-trip, no network, no shared state (INV-3)
- **append-only** — `T` is unchanged and remains valid; `T′` is a new token (spec 01 §4)

`authority(T)` denotes the set of `(operation, request-context)` pairs that `T` authorizes. It
is a set, not a scalar, and every invariant below is a statement about it.

---

## 2. Where each guarantee actually lives

Three enforcement points, and conflating them is how a security argument goes wrong.

| Layer | Enforces | When | Failure mode if absent |
|---|---|---|---|
| **Biscuit structure** | INV-1, INV-2, INV-3, INV-4, INV-8 | Verification | Catastrophic — authority could widen |
| **`narrows()` at mint** | EC-T17, EC-T18, EC-T19 | `attenuate()` call | Confusing tokens, not insecure ones |
| **Ledger** | INV-5 | Spend time | Overspend across siblings |

**The security boundary is biscuit's append-only structure, not `narrows()`.** Because every
clause in every block must pass, the effective authority of a chain is the *intersection* of all
its blocks' constraints. Adding a caveat can only shrink an intersection. This holds whether or
not `narrows()` is ever called.

So why have `narrows()` at all? Three reasons, none of them "security":

1. **Legible errors.** A child that declares `spend ≤ ৳100,000` under a parent capped at
   ৳50,000 is not dangerous — the effective cap is still ৳50,000 — but it is a lie in the
   token, and the developer should hear about it at mint time (EC-T18).
2. **A computable authority set.** The identity tree (T-045) and the custody query must show
   what a token *can* do. That requires folding the caveat chain into an effective bound, which
   is exactly what the `narrows()` partial order provides.
3. **Testable invariants.** INV-1 as a property test needs a decidable comparison between two
   tokens' authority. Eight closed caveat types with a defined partial order give one.

State this distinction plainly when a judge asks "what if `narrows()` has a bug?" The answer is
that a `narrows()` bug produces a misleading token, not an over-privileged one.

---

## 3. The `narrows()` algebra

`c₁.narrows(c₂)` is true iff `c₁` is at least as restrictive as `c₂`, for caveats of the same
kind. It MUST be **reflexive** (P-03) and **transitive** (P-04). Comparing different kinds is a
programming error and MUST raise, not return `False` — silently returning `False` would let a
mint-time check pass a comparison it never actually made.

| Kind | `c₁.narrows(c₂)` iff | Order |
|---|---|---|
| `ScopeSubset(S)` | `S₁ ⊆ S₂` | subset |
| `BudgetCeiling(d, v)` | same `d`, `v₁ ≤ v₂` | numeric ≤ |
| `TimeWindow(a, b)` | `a₁ ≥ a₂ ∧ b₁ ≤ b₂` | interval containment |
| `ToolAllow(A)` | `A₁ ⊆ A₂` | subset |
| `ToolDeny(D)` | `D₁ ⊇ D₂` | **superset** — denying more is stricter |
| `ArgPredicate(p, op, v)` | same `p`, comparable `op`; upper bounds `v₁ ≤ v₂`, lower bounds `v₁ ≥ v₂`, membership `S₁ ⊆ S₂` | per operator |
| `DepthLimit(n)` | `n₁ ≤ n₂` | numeric ≤ |
| `IntentBound(h)` | `h₁ == h₂` | equality only |
| `RequiresApproval(S)` | `S₁ ⊇ S₂` | **superset** |

Two reversed directions (`ToolDeny`, `RequiresApproval`) and one degenerate one (`IntentBound`).
These are the three places a hand-written implementation gets it backwards, and the three the
property tests should hammer hardest.

### 3.1 Adding a caveat of a kind the parent does not have

Always permitted. A restriction with no counterpart in the parent is strictly new, and a new
restriction narrows. `narrows()` is not consulted.

### 3.2 The mint-time check

```
attenuate(T, C) MUST raise AttenuationError if, for any c ∈ C,
there exists a caveat p of the same kind in T's chain
such that NOT c.narrows(p)
```

Where "the same kind" includes matching the discriminator: dimension for `BudgetCeiling`, path
and operator for `ArgPredicate`.

Covers EC-T17 (adding an ungranted scope), EC-T18 (raising a ceiling), EC-T19 (extending
expiry). All three MUST fail at mint, not at verification.

### 3.3 Effective bound

Folding a chain into a single displayable authority set, for the console and the custody query:

- `ScopeSubset`: intersection of all sets, further intersected with the authority grant
- `BudgetCeiling`: minimum per dimension
- `TimeWindow`: `[max of lower bounds, min of upper bounds]`; empty interval means the token is
  dead and MUST render as such
- `ToolAllow`: intersection · `ToolDeny`: union
- `ArgPredicate`: per `(path, operator)`, the tightest bound
- `DepthLimit`: minimum · `IntentBound`: the single hash · `RequiresApproval`: union

---

## 4. The invariants

### INV-1 Monotonicity

> For all tokens `T` and caveat sets `C`: `authority(attenuate(T, C)) ⊆ authority(T)`.
> No caveat set can widen authority.

**Mechanism:** every clause in every block must pass, so authority is an intersection over
blocks. Block facts are scoped and invisible to earlier blocks' checks. Request context comes
from the verifier, never from the token.

**Verified:** a block appending `operation("payment:initiate")`, `scope("payment:initiate")`,
`requested("spend_bdt", 0)`, and `current_depth(0)` — a direct attempt to forge request context
and re-grant a removed scope — authorized nothing it had been narrowed out of.

**Property test:** P-01. **Counterexamples in §5.1.**

### INV-2 Transitivity

> `authority(attenuate(attenuate(T, C₁), C₂)) ⊆ authority(attenuate(T, C₁)) ⊆ authority(T)`.

**Mechanism:** INV-1 applied inductively; the intersection is over all blocks, so each append
can only shrink it.

**Verified on the spec-01 worked chain:**
`L0 = {invoice:read, vendor:read, vendor:negotiate, payment:initiate}`
`⊇ L1 = {invoice:read, vendor:read, vendor:negotiate}` `⊇ L2 = {invoice:read}`.

**Property test:** P-02.

### INV-3 Offline soundness

> Verification requires only the root public key and the token bytes. No network, no database,
> no shared state.

**Mechanism:** biscuit verification is a signature chain check plus local Datalog evaluation.

**Property test:** P-05, with a socket monkeypatch that fails the test if any network call is
attempted during verification.

> Revocation checking is **not** part of verification and does need state (the revoked set).
> That state is local to the PEP and refreshed asynchronously, which is what keeps the hot path
> network-free — see `07-revocation.md`.

### INV-4 Non-forgeability

> Without a parent block's key, no valid extension can be produced.

**Mechanism:** per-block Ed25519 signatures over the preceding chain.

**Verified:** a token signed with a non-root key is rejected; a single flipped bit is rejected;
truncated bytes are rejected, with an exception rather than a crash.

### INV-5 Budget subadditivity

> For any dimension `d`: `Σ over children(budget_d) ≤ parent budget_d`, **enforced at spend
> time**, even where it is not statically true.

**This is the one invariant the token format deliberately does not carry, and the reason the
ledger exists.** A parent may hand the same ৳50,000 ceiling to three children. Each token is
individually valid; together they could spend ৳150,000. No amount of static token inspection
prevents this, because each token is genuinely, correctly authorized for ৳50,000.

Two mitigations, both implemented:

- **Proportional split** — the parent explicitly divides the budget among children. Enforced
  statically, at mint time. Predictable, and wasteful when one child needs more than its share.
- **Shared pool** — children reference the same ledger budget id and draw from one pool.
  Enforced dynamically by the lease protocol. **This is the default**, because it is what real
  workflows want.

**Property test:** P-10, a stateful `hypothesis` `RuleBasedStateMachine` over acquire, reserve,
commit, refund, release, expire, crash, and revoke, asserting `committed + leased ≤ total ∧
committed ≥ 0 ∧ leased ≥ 0` after every step.

**Counterexamples in §5.2.** Full protocol in `04-lease-protocol.md`.

> Most capability-token literature does not address quantitative resources shared across
> siblings. Naming the problem and shipping both mitigations is the publishable observation.

### INV-6 Depth bound

> `depth(T′) = depth(T) + 1`, and any token with `depth > max_depth` fails verification.

**Mechanism:** depth is `block_count - 1`, computed by the verifier. A `declared_depth` fact in
a block is advisory and MUST NOT be used for authorization (ADR-005).

**Verified:** at `max_depth = 8`, depth 8 authorizes and depth 9 does not. Also verified as a
*negative* result first: with depth read from block facts, a depth-9 chain authorized
successfully — which is why the fact is advisory.

**Property test:** P-06. Related: P-17, token size grows monotonically with depth.

### INV-7 Intent stability

> `intent_hash` is set once in the authority block and cannot be changed by attenuation.

**Mechanism:** `intent` appears only in the authority block. A later block's `intent` fact is
invisible to the authority block's `check if request_intent($h), intent($h)`.

**Verified:** confirmed by the same block-scoping result as INV-1, and by
`authorizer.query` returning nothing for block facts.

**Property test:** P-07.

### INV-8 Deny precedence

> If any caveat anywhere in the chain denies, the decision is deny, regardless of any allow.

**Mechanism:** biscuit requires all checks in all blocks to pass, and any `reject if` with a
solution fails the authorization.

**Verified:** contradictory caveats (`amount ≤ 100` in one block, `amount ≥ 200` in another)
deny everything without crashing; `ToolDeny` beats `ToolAllow` across blocks.

**Property test:** P-09.

### INV-9 Expiry contraction

> `expiry(T′) ≤ expiry(T)`.

**Mechanism:** every block's `TimeWindow` upper bound is a `check if` that must pass, so the
effective expiry is the minimum. Mint-time `narrows()` additionally rejects a child declaring a
later expiry (EC-T19).

**Verified:** nested windows intersect — a request inside the outer window but outside the inner
one is denied.

**Property test:** P-08. **Counterexamples in §5.3.**

### INV-10 No resurrection

> A token whose ancestor is revoked cannot authorize anything.

**Mechanism:** each block has a revocation id (128 hex chars, spec 01 §8). A token is revoked if
**any** id in its chain is in the revoked set. A child chain's id list begins with its parent's,
unchanged — verified — so revoking a parent kills every descendant.

**Property test:** P-21. End-to-end propagation timing in T-040 (NFR-4, under 2 s p99).

---

## 5. Counterexamples

What would violate each invariant. These are the shapes the property tests must be capable of
generating; a strategy that cannot produce them passes vacuously.

### 5.1 INV-1 Monotonicity

**CE-1.1 — Block facts visible to earlier checks.** If a later block's facts entered the same
Datalog world as the authority block's checks, a child appending `scope("payment:initiate")`
would satisfy `check if operation($op), scope($op)` for an operation the mandate never granted.
*Measured: does not occur — block facts are scoped.* This is the single assumption the whole
design rests on, and it must be re-verified whenever `biscuit-python` is upgraded.

**CE-1.2 — Last-writer-wins clauses.** If a later block's checks replaced earlier ones instead
of accumulating, a child could append a permissive `ScopeSubset` covering every scope and undo
its parent's restriction. *Biscuit accumulates; the effective bound is the intersection.*

**CE-1.3 — Request context taken from the token.** If the verifier read `current_depth` or
`operation` from token facts rather than computing them, a child could append
`current_depth(0)` and escape a `DepthLimit`. *Measured as part of the fact-injection attack:
denied. This is why spec 01 §7 makes request context the verifier's exclusive responsibility.*

**CE-1.4 — A `narrows()` direction inverted.** If `ToolDeny.narrows()` used `⊆` instead of `⊇`,
the mint-time check would accept a child that denies *fewer* tools than its parent. Authority
would not actually widen — the parent's reject still applies — but the token would misreport
its own authority, and the identity tree would show a privilege the agent does not have.

### 5.2 INV-5 Budget subadditivity

**CE-2.1 — Three siblings, one ceiling.** A parent holding ৳150,000 mints three children each
capped at ৳50,000. Every token verifies. Static analysis sees three valid ৳50,000 grants. They
spend concurrently and the mandate is exhausted three times over. *Only the ledger prevents
this; it is the entire reason for the lease protocol and for demo Beat 4.*

**CE-2.2 — Proportional split without atomic deduction.** A parent splits ৳50,000 into two
৳25,000 children, but both top up from the same ledger row without a serialized transaction.
Two concurrent `ACQUIRE`s both read `available = ৳25,000` and both succeed. *Prevented by
`SELECT … FOR UPDATE` on the budget row (T-013).*

**CE-2.3 — Refund race.** Child A commits an actual below its estimate and refunds the
difference while child B reserves. If the refund is applied outside the serialized transaction,
`leased` can be decremented twice, or driven negative, letting the pool over-issue. *Prevented
by idempotent commits keyed on `reservation_id` (P-12) and by the `leased ≥ 0` DB check.*

**CE-2.4 — Stranded lease counted as spent.** A PEP is hard-killed holding a ৳10,000 lease. If
the reaper released it *and* the crashed PEP's in-flight commits later arrived, the same budget
would be released twice. *Prevented by lease state transitions being one-way and commits being
idempotent. The stranded window itself is a real, bounded limitation — state it.*

### 5.3 INV-9 Expiry contraction

**CE-3.1 — Only the innermost window applied.** If verification used the last block's
`TimeWindow` rather than all of them, a child declaring an expiry *later* than its parent's
would extend the token's life. *Measured: all windows apply and intersect.*

**CE-3.2 — `TimeWindow` encoded as `reject if`.** `reject if time($t), $t > EXPIRY` is vacuous
when the verifier omits `time()` — the token would never expire. *This is exactly why
`TimeWindow` compiles to `check if` and `time` is mandatory request context (spec 02 §3.2). A
caveat that fails open on missing context is a bug, not a convenience.*

**CE-3.3 — Clock skew toward the past.** A PEP whose clock lags the ledger by 30 s accepts a
token for 30 s past its true expiry. *Mitigated by treating expiry conservatively — expire
early, never late — and by the ±30 s tolerance in EC-T08. The residual window is bounded by the
configured tolerance and must be stated, not hidden.*

**CE-3.4 — Boundary inclusive instead of exclusive.** `$t <= expires_at` admits a request at
exactly `expires_at`. EC-T06 requires rejection at the boundary. *The PEP's strict pre-check
owns this; the Datalog check is the backstop (spec 01 §5.3).*

---

## 6. Property test mapping

Written in T-009 unless noted. Every one uses `hypothesis`.

| Test | Statement | Invariant |
|---|---|---|
| P-01 | `attenuate` never widens authority | INV-1 |
| P-02 | Chains are transitively narrowing | INV-2 |
| P-03 | `narrows()` is reflexive, per kind | — |
| P-04 | `narrows()` is transitive, per kind | — |
| P-05 | `verify()` makes no network call | INV-3 |
| P-06 | Depth strictly increases; `> max_depth` always fails | INV-6 |
| P-07 | `intent_hash` immutable under attenuation | INV-7 |
| P-08 | Expiry monotonically contracts | INV-9 |
| P-09 | Deny beats allow anywhere in the chain | INV-8 |
| P-10 | `Σ committed ≤ total` for any interleaving (stateful) | INV-5 — T-013/T-017 |
| P-17 | Token size grows monotonically with depth | — |
| P-21 | Revoking any ancestor kills all descendants | INV-10 — T-040 |

**Strategy requirements for T-009.** The generators MUST be able to produce:

- caveat sets mixing all eight kinds, including the two reversed orders
- chains at every depth from 0 to `max_depth + 1`
- sibling chains from a common parent (for INV-5 shapes)
- empty scope sets, zero ceilings, and empty time intervals
- duplicate and contradictory caveats of the same kind
- Bengali and other non-ASCII text in roles and scope values (EC-T16)

A strategy that cannot generate the §5 counterexample shapes is not testing the invariant. Check
the generators against that list explicitly, and use `hypothesis`'s coverage reporting to
confirm the interesting branches are actually reached.

---

## 7. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Does the effective-bound fold (§3.3) need caching for the identity tree at depth 8 | T-045 |
| 2 | Should `AttenuationError` name every violated caveat, or fail on the first | T-009 |
| 3 | Proportional split: how a parent expresses the division in the SDK API | T-017 |
