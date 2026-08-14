# Spec 01 — Token Format

**Status:** accepted · **Ticket:** T-002 · **Implements:** `PLAN.md` §6.1
**Consumed by:** T-005 (domain models), T-007 (biscuit wrapper), T-009 (attenuation)

> **MUST / MUST NOT / SHOULD** are normative. Everything else is rationale.
>
> Every byte count and every semantic claim in this document was measured against
> `biscuit-python` 1.x on CPython 3.12, not derived on paper. The measurement scripts are
> reproduced in §10 so the numbers can be re-checked when the library version moves.

---

## 1. Purpose

An AgentIAM token is a **biscuit**. It carries the identity of an agent, binds that agent to
a task, and states the authority the agent holds — as a set of restrictions that only ever
narrows as the token is delegated.

The property that makes biscuit the right choice, and the sentence to say on stage:

> A JWT holder cannot narrow a JWT — narrowing requires the issuer. Biscuit's append-only
> attenuation with per-block signatures makes holder-side narrowing cryptographically sound
> and offline-verifiable.

A holder MUST be able to mint a strictly narrower child token with no network round-trip and
no issuer involvement, and a verifier MUST be able to check the result with nothing but the
root public key.

---

## 2. The central rule: grant lives in the token, request comes from the verifier

**This is the most important section in this document.** Getting it wrong produces a token
that appears to enforce restrictions and does not.

Biscuit checks are **existential**. `check if scope($s), ["invoice:read"].contains($s)` asks
*"does there exist a `scope` fact in this list?"*, not *"is the scope being requested in this
list?"*. Written against the token's own grant facts, it passes as soon as **any** granted
scope matches — so a token granting `invoice:read` and `vendor:read` would happily authorize
`vendor:read` under a caveat that names only `invoice:read`.

This was verified empirically, and it fails exactly as described.

### 2.1 Normative consequence

- Token blocks MUST carry only the **grant**: what this agent was given.
- The verifier MUST supply the **request context**: what is being attempted right now.
- Every check MUST be written against verifier-supplied request facts, never against the
  token's own grant facts.

The one exception is the authority block's grant-membership check, which deliberately joins
the two: `check if operation($op), scope($op)` — *the operation being attempted must be one
this mandate granted*.

### 2.2 Second trap: a check with no matching fact fails

`check if requested("tool_calls", $v), $v <= 400` **denies** when the request context contains
no `requested("tool_calls", …)` fact at all. An unrelated call carrying a `tool_calls` ceiling
would be denied outright.

Therefore: the verifier MUST supply a `requested(dimension, value)` fact for **every** budget
dimension in §5.2 on **every** authorization, defaulting to `0` for dimensions the call does
not consume. Omitting a dimension is not "no constraint" — it is a denial.

---

## 3. Identifiers and encodings

| Element | Encoding | Notes |
|---|---|---|
| `mandate`, `task` | ULID string | Sortable, 26 chars |
| `principal` | `kc:<oidc-sub>` | Keycloak subject, prefixed by issuer |
| `agent` | `agt_<ULID>` | |
| `intent` | 64 lowercase hex chars | SHA-256 of the canonicalized description; **no** `sha256:` prefix inside the token — the algorithm is fixed by this spec, and the prefix costs 7 bytes per token |
| Timestamps | RFC 3339, UTC, `Z` suffix | Biscuit date literals |
| Scopes | `resource:verb` | Lowercase ASCII. **No wildcards** (EC-T15) |
| Money and quantities | signed 64-bit integer, scaled | See §3.1 |

### 3.1 Money encoding — scaled integers

Money is `Decimal` in Python and `NUMERIC(20,4)` in Postgres. Datalog has no decimal type, and
Datalog cannot compare strings numerically.

**Money and all budget quantities MUST appear in token facts as integers scaled by 10⁴**
(`BUDGET_SCALE = 10_000`), representing the value in units of 1/10000.

```
৳500,000.0000  ->  budget("spend_bdt", 5000000000)
```

- Conversion MUST be exact: `int(Decimal(value) * 10**4)`, raising on any non-zero remainder.
- Range: ±9.22×10¹⁴ scaled, i.e. ±৳922,337,203,685.4775. Far beyond any mandate; a value
  exceeding it MUST be rejected at mint time rather than silently wrapping.
- Non-money dimensions (`tool_calls`, `rows_read`, …) are counts and use the **same scale**,
  so a single comparison rule covers every dimension. `2000` tool calls is `20000000`.

> This deviates from the illustrative `budget("spend_bdt", "500000")` in `PLAN.md` §6.1, which
> shows a string. A string ceiling cannot be compared in Datalog, so the token could not
> enforce its own budget caveat offline — the whole point of carrying it. See ADR-005.

---

## 4. Token structure

A token is a chain of signed blocks. Block 0 is the **authority block**, signed with the root
key. Blocks 1..n are **attenuation blocks**, each signed with an ephemeral key generated by the
holder appending it.

```
┌─ block 0 · authority ────────── signed by the ROOT key ────────┐
│  grant: mandate, task, principal, intent, scopes, budgets      │
│  checks: grant membership, depth, intent, budget, validity     │
├─ block 1 · attenuation ──────── signed by holder's key ────────┤
│  identity: agent, role                                         │
│  checks: narrower scopes, lower ceilings, shorter window       │
├─ block 2 · attenuation ──────── signed by holder's key ────────┤
│  … strictly narrower again                                     │
└────────────────────────────────────────────────────────────────┘
```

**Depth** is `block_count - 1`. The root token has depth 0.

---

## 5. Authority block

Minted by the issuance service. The only block signed with the root keypair.

### 5.1 Facts — identity and binding

| Fact | Arity | Required | Meaning |
|---|---|---|---|
| `mandate(id)` | 1 | yes | Mandate this token draws authority from |
| `task(id)` | 1 | yes | Task the principal approved |
| `principal(sub)` | 1 | yes | The human who approved it |
| `intent(hash)` | 1 | yes | SHA-256 of the canonicalized task description |
| `issued_at(ts)` | 1 | yes | Mint time |
| `max_depth(n)` | 1 | yes | Delegation cap; `n` MUST be ≤ 8 |

### 5.2 Facts — the grant

| Fact | Arity | Meaning |
|---|---|---|
| `scope(name)` | 1 | One fact per granted scope. An empty scope set grants nothing (EC-T14) |
| `budget(dimension, scaled)` | 2 | One fact per dimension |

Budget dimensions are exactly: `spend_bdt`, `tool_calls`, `rows_read`, `external_emails`,
`wall_clock_s`. A dimension absent from the authority block MUST be treated as a ceiling of
zero, not as unlimited.

### 5.3 Checks

```datalog
check if operation($op), scope($op);
check if current_depth($d), $d <= 8;
check if request_intent($h), intent($h);
check if requested($dim, $v), budget($dim, $max), $v <= $max;
check if time($t), $t >= 2026-08-14T11:45:00Z;   // not_before
check if time($t), $t <= 2026-08-14T12:00:00Z;   // expires_at
```

| Check | Enforces | Invariant |
|---|---|---|
| grant membership | the attempted operation was granted | — |
| depth | `current_depth ≤ max_depth` | INV-6 |
| intent | the request is bound to the approved task | INV-7 |
| budget | per-dimension ceiling from the mandate | — |
| `not_before` / `expires_at` | validity window | INV-9 |

Expiry is **exclusive at the boundary**: a token whose `expires_at` equals the verification
instant MUST be rejected (EC-T06). `$t <= expires_at` with the verifier supplying the current
instant gives inclusive behaviour, so the verifier MUST supply `time()` such that the boundary
rejects — in practice, compare with `<` in the PEP's own pre-check and treat the Datalog check
as the backstop. T-007 owns making this exact and testing both sides of the boundary.

---

## 6. Attenuation blocks

Appended by a holder, offline, signed with a freshly generated ephemeral key.

### 6.1 Facts

| Fact | Required | Meaning |
|---|---|---|
| `agent(id)` | yes | The sub-agent this block issues authority to |
| `role(name)` | yes | Human-readable role, for the console and audit |
| `declared_depth(n)` | no | Advisory only — see the warning below |

> **`declared_depth` MUST NOT be used for authorization.** A block's own facts are written by
> whoever appended the block. Authorization depth comes from `block_count - 1`, computed by
> the verifier. The fact exists only so the audit trail and the identity tree can show what
> each block claimed. This resolves an ambiguity in `PLAN.md` §6.1, which lists `depth(n)` as
> a block fact and `depth ≤ max_depth` as a check; done literally, that check is trivially
> satisfiable by the `depth(0)` fact in the authority block. See ADR-005.

### 6.2 Checks

All checks constrain **verifier-supplied request facts**:

```datalog
check if operation($op), ["invoice:read", "vendor:read"].contains($op);
check if requested("spend_bdt", $v), $v <= 500000000;
check if requested("tool_calls", $v), $v <= 400;
check if current_depth($d), $d <= 3;
check if time($t), $t <= 2026-08-14T11:55:00Z;
```

The caveat types that compile to these checks are specified in `02-caveat-language.md`. This
document fixes only their **wire shape**.

### 6.3 What a block cannot do

An attenuation block MUST NOT be able to widen authority. Verified empirically: a block adding
`operation("payment:initiate")`, `scope("payment:initiate")`, `requested("spend_bdt", 0)`, and
`current_depth(0)` — a direct attempt to forge request context and re-grant a removed scope —
**failed to authorize anything it had been narrowed out of.** Biscuit scopes block facts so
they are not visible to earlier blocks' checks, and the verifier's own facts are authoritative.

---

## 7. Verifier-supplied request context

On every authorization the verifier MUST supply exactly these facts:

| Fact | Value |
|---|---|
| `operation(scope)` | The scope the current call maps to |
| `requested(dimension, scaled)` | **One per dimension in §5.2**, defaulting to `0` |
| `current_depth(n)` | `token.block_count() - 1` |
| `request_intent(hash)` | Intent hash of the task the call is being made under |
| `time(ts)` | The verifier's current instant |

The verifier MUST NOT accept any of these from the request, the token, or the agent. They are
facts about what is happening, determined by the enforcement point.

---

## 8. Revocation identifiers

Biscuit assigns each block a revocation id — measured at **128 hex characters (64 bytes)**, the
block signature.

A token MUST be treated as revoked if **any** id in its chain is in the revoked set. This gives
subtree revocation for free: revoking a parent's block id kills every descendant, because every
descendant chain contains that block. Verified: a child chain's revocation id list begins with
its parent's list, unchanged.

Sizing consequence for T-039: 64 bytes per revoked block, so 10,000 revoked blocks is ~640 KB
of exact-set storage per PEP. Comfortable.

---

## 9. Size limits

| Threshold | Behaviour |
|---|---|
| `max_depth` | MUST be ≤ 8; a chain deeper than `max_depth` MUST fail verification |
| base64 length > 4096 | SHOULD warn and record `token_size_warning` on the decision record |
| base64 length > 8192 | MUST hard-error at mint time |
| beyond 8192 | Token reference mode — **deferred, T-010** (`PLAN.md` §21 item 6) |

### 9.1 Measured growth

Measured with the format in §10: a realistic authority block (5 scopes, 5 budget dimensions,
6 checks) and typical attenuation blocks (2 identity facts, 3–5 checks).

| Depth | Raw bytes | Base64 chars | Status |
|---|---|---|---|
| 0 (root) | 1,051 | 1,404 | ok |
| 1 | 1,491 | 1,988 | ok |
| 2 | 1,854 | 2,472 | ok |
| 3 | 2,169 | 2,892 | ok |
| 4 | 2,476 | 3,304 | ok |
| 5 | 2,783 | 3,712 | ok |
| 6 | 3,090 | 4,120 | warn |
| 7 | 3,397 | 4,532 | warn |
| 8 | 3,704 | 4,940 | warn |

Growth is ~307 raw / ~410 base64 bytes per attenuation block.

**At `max_depth = 8` the token is 4,940 base64 characters — over the 4 KB warning threshold but
only 60% of the 8 KB hard limit.** The reference-handle path therefore cannot trigger within
the permitted depth range, which is the measured justification for deferring T-010. The warning
threshold does fire at depth 6, so the warning path is reachable and must be tested (EC-T11).

The full `Authorization: Bearer …` header at depth 2 is **2,494 bytes** — well inside the
common 8 KB server header limit, and inside nginx's 4 KB default `large_client_header_buffers`
line size only up to about depth 5. The demo runs at depth 3–4. **Deployment note for T-018:**
raise the header buffer explicitly rather than discovering this at depth 6.

---

## 10. Worked example — a 3-level chain

Procurement task: *"Procure 500 units of packaging film, budget ৳500,000."*

### Level 0 — authority block, minted by the issuance service

```datalog
mandate("01J8ZQ7X4M0000000000000001");
task("01J8ZQ7X4M0000000000000002");
principal("kc:9f2c1e40-7a3b-4d21-9c88-1b2e5f0a4d77");
intent("4f9a1c2d8e3b6057a1d4c9e2f8b30567a9c1e4d78b2f6039c5a8e1d4f7b0c3a26");
issued_at(2026-08-14T11:45:00Z);
max_depth(8);
scope("invoice:read");
scope("vendor:read");
scope("vendor:negotiate");
scope("payment:initiate");
scope("email:send");
budget("spend_bdt", 5000000000);      // ৳500,000.0000
budget("tool_calls", 2000);
budget("rows_read", 500000);
budget("external_emails", 50);
budget("wall_clock_s", 3600);
check if operation($op), scope($op);
check if current_depth($d), $d <= 8;
check if request_intent($h), intent($h);
check if requested($dim, $v), budget($dim, $max), $v <= $max;
check if time($t), $t >= 2026-08-14T11:45:00Z;
check if time($t), $t <= 2026-08-14T12:00:00Z;
```
**1,051 raw bytes · 1,404 base64.**

### Level 1 — the procurement lead attenuates to a negotiator

```datalog
agent("agt_01J8ZQ7X4M0000000000000010");
role("negotiator");
declared_depth(1);
check if operation($op), ["invoice:read", "vendor:read", "vendor:negotiate"].contains($op);
check if requested("spend_bdt", $v), $v <= 500000000;    // ৳50,000 — a tenth of the mandate
check if requested("tool_calls", $v), $v <= 400;
check if current_depth($d), $d <= 3;
check if time($t), $t <= 2026-08-14T11:55:00Z;
```
**1,491 raw bytes · 1,988 base64** (+440 / +584). `payment:initiate` and `email:send` are gone.

### Level 2 — the negotiator attenuates to a read-only document agent

```datalog
agent("agt_01J8ZQ7X4M0000000000000021");
role("doc-reader");
declared_depth(2);
check if operation($op), ["invoice:read"].contains($op);
check if requested("spend_bdt", $v), $v <= 0;            // cannot spend anything
check if requested("tool_calls", $v), $v <= 50;
check if time($t), $t <= 2026-08-14T11:50:00Z;
```
**1,854 raw bytes · 2,472 base64** (+363 / +484).

### Measured authorization outcomes

| Token | Operation | Spend | Result |
|---|---|---|---|
| L0 | `payment:initiate` | ৳100 | **allow** |
| L1 | `vendor:negotiate` | ৳40,000 | **allow** |
| L1 | `vendor:negotiate` | ৳60,000 | **deny** — over L1's own ceiling |
| L1 | `payment:initiate` | any | **deny** — narrowed away at L1 |
| L2 | `invoice:read` | 0 | **allow** |
| L2 | `invoice:read` | ৳0.0001 | **deny** — L2 may not spend |
| L2 | `vendor:read` | 0 | **deny** — narrowed away at L2 |
| L2 + forged block | `payment:initiate` | 0 | **deny** — fact injection does not widen |

Authority sets, measured: `L0 = {invoice:read, vendor:read, vendor:negotiate, payment:initiate}`
⊇ `L1 = {invoice:read, vendor:read, vendor:negotiate}` ⊇ `L2 = {invoice:read}`. INV-1 and INV-2
hold on this chain.

This is demo Beat 3: the doc-reader attempts a payment and is denied, naming the exact caveat,
with no network call made to reach that denial.

---

## 11. Invariant coverage

| Invariant | Mechanism | Verified |
|---|---|---|
| INV-1 monotonicity | Block facts are not visible to earlier checks; checks only add | yes — fact-injection attack denied |
| INV-2 transitivity | Every block's checks apply to every request | yes — authority sets strictly nest |
| INV-3 offline soundness | Verification takes the root public key and the token, nothing else | yes |
| INV-4 non-forgeability | Per-block Ed25519 signatures | yes — wrong key, bit flip, and truncation all rejected |
| INV-5 budget subadditivity | **Not enforceable in-token.** Siblings may each hold the full ceiling; the ledger enforces the sum | by design — see `04-lease-protocol.md` |
| INV-6 depth bound | `current_depth` from `block_count`, checked in the authority block | yes — depth 8 allows, 9 denies |
| INV-7 intent stability | `intent` lives only in the authority block; a later block's `intent` fact is invisible to the authority check | yes |
| INV-8 deny precedence | Biscuit requires **all** checks in **all** blocks to pass | yes — contradictory caveats deny everything |
| INV-9 expiry contraction | Each block MAY add a `time` upper bound; all apply | yes |
| INV-10 no resurrection | Any ancestor block id in the revoked set revokes the chain | §8; e2e in T-040 |

INV-5 is the one invariant this format deliberately does not carry, and the reason the ledger
exists. Three siblings can each be handed a ৳50,000 ceiling; each token is individually valid,
and together they could spend ৳150,000. Static token inspection cannot prevent that. Say this
plainly — it is a genuine observation, not a gap.

---

## 12. Edge cases owned by this spec

Behaviour fixed here; tests land in T-007.

| ID | Case | Required behaviour |
|---|---|---|
| EC-T01 | Missing/empty token | `MALFORMED_REQUEST`, HTTP 401 |
| EC-T02 | Truncated bytes | Signature failure, no crash — verified |
| EC-T03 | Single flipped bit | Signature failure — verified |
| EC-T04 | Wrong root public key | Reject — verified |
| EC-T05 | Rotated root key | Reject unless the old key is still in the accepted set; both cases tested |
| EC-T06 | `expires_at` exactly now | Reject — boundary exclusive |
| EC-T07 | `not_before` in the future | `TOKEN_NOT_YET_VALID` |
| EC-T09 / EC-T10 | Depth 8 / depth 9 | Allow / `DEPTH_EXCEEDED` — verified |
| EC-T11 | 4 KB / 8 KB / 8 KB+1 | Warn / hard error / reference mode (deferred) |
| EC-T12 | Duplicate caveat | Idempotent; the stricter bound wins, because all checks apply |
| EC-T13 | Contradictory caveats | Deny everything, no crash — verified |
| EC-T14 | Empty scope set | Deny all operations |
| EC-T15 | Wildcard scope | Not supported. Scopes are exact strings |
| EC-T16 | Bengali text in roles and descriptions | Handled; canonicalization is `06-drift-detection.md`'s concern, encoding is this one's |
| EC-T20 | Replay from a different agent | **Allowed by design.** Bearer semantics. Mitigations are short TTL and revocation; PoP binding is documented future work |

EC-T20 is a real weakness. State it voluntarily, with its bound: default TTL 15 minutes,
revocation propagation under 2 s (NFR-4). It is the same trust model as every OAuth deployment
in production today.

---

## 13. Reproducing the measurements

```python
from biscuit_auth import AuthorizerBuilder, BiscuitBuilder, BlockBuilder, KeyPair

root = KeyPair()
token = BiscuitBuilder(AUTHORITY_SOURCE).build(root.private_key)   # §10 level 0
token = token.append(BlockBuilder(LEVEL_1_SOURCE))
token = token.append(BlockBuilder(LEVEL_2_SOURCE))

len(token.to_bytes())     # raw
len(token.to_base64())    # what goes in the header
token.block_count() - 1   # depth
token.revocation_ids      # one per block, 128 hex chars each

AuthorizerBuilder(REQUEST_CONTEXT + "allow if true;").build(token).authorize()
```

`REQUEST_CONTEXT` MUST contain every fact in §7. T-007 turns this into a fixture and a test.

---

## 14. Deviations from `PLAN.md` §6.1

Three, all recorded in ADR-005. Each resolves an ambiguity in the sketch rather than changing
its intent.

1. **Budget values are scaled integers, not strings.** A string ceiling cannot be compared in
   Datalog, so the token could not enforce its own budget caveat offline.
2. **Depth for authorization comes from `block_count`, not from a `depth` fact.** Block facts
   are written by whoever appends the block, and the check is existential — the literal reading
   is trivially satisfiable.
3. **Checks are written against verifier-supplied request facts, not the token's grant facts.**
   The literal reading of "checks: scope subset" produces a caveat that does not constrain the
   scope actually being requested.

None of these touch the caveat language, the attenuation semantics, or the lease protocol.

---

## 15. Open questions for downstream tickets

| # | Question | Owner |
|---|---|---|
| 1 | Exact boundary handling for `expires_at` — Datalog `<=` vs. a strict PEP pre-check | T-007 |
| 2 | Root key rotation: how many keys in the accepted set, and for how long (EC-T05) | T-007 |
| 3 | Does `ArgPredicate` need request facts beyond §7 (e.g. `arg("payment.amount", v)`) | T-003 |
| 4 | Whether `role` should be constrained to an enum for console rendering | T-011 |
