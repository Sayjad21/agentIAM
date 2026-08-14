# Spec 02 — Caveat Language

**Status:** accepted · **Ticket:** T-003 · **Implements:** `PLAN.md` §6.2
**Depends on:** [`01-token-format.md`](01-token-format.md) · **Consumed by:** T-008, T-009, T-019

> **MUST / MUST NOT / SHOULD** are normative. Every Datalog encoding below was executed
> against `biscuit-python` 1.x before being written down; §9 lists what was verified.

---

## 1. Purpose

A caveat is a restriction carried inside a token. The caveat language is a thin, typed Python
DSL that compiles to biscuit Datalog.

The DSL exists for one reason: **the narrowing check must be decidable in our own code.** Raw
Datalog is not — deciding whether one arbitrary Datalog program is more restrictive than
another is undecidable in general. By restricting expressible caveats to eight closed forms,
each with a defined partial order, `narrows()` becomes a small total function and INV-1 becomes
a property we can actually test.

> If a proposed caveat type cannot implement `narrows()` soundly, it does not go in the
> language. That rule is what keeps the attenuation invariants provable.

---

## 2. The three caveat operations

Every caveat kind MUST support three operations:

```python
def to_datalog(caveat: Caveat) -> str:
    """Compile to a biscuit check or reject clause. See §3."""

def evaluate(caveat: Caveat, ctx: RequestContext) -> CaveatResult:
    """Pure Python evaluation, for outcomes Datalog cannot express and for
    naming the failing caveat on a decision record."""

def narrows(caveat: Caveat, other: Caveat) -> bool:
    """True iff caveat is at least as restrictive as other, same kind.

    Reflexive and transitive (P-03, P-04). Comparing different kinds is a
    programming error, not False — raise.
    """
```

They are **module-level functions in `caveats.py`, not methods on the models.** `PLAN.md` §5
puts the Pydantic models in `models.py` and the DSL↔Datalog translation in `caveats.py`, and
keeping the data types free of behaviour preserves that split. Exhaustiveness is enforced by
structural pattern matching over the caveat union with `assert_never`, so a new kind fails type
checking until every operation handles it — the same guarantee a Protocol would give.

`to_datalog()` and `evaluate()` MUST agree on allow/deny for every input. **This is a security
property, not a nicety:** the PEP trusts Datalog for the decision and `evaluate()` for the
explanation, so a divergence means a decision record naming a caveat that did not actually fire.
The conformance test in T-008 asserts it by compiling each caveat into a real biscuit block and
comparing the authorizer's verdict against `evaluate()` — not by re-implementing the comparison
twice and hoping.

`RequiresApproval` is exempt from the agreement rule in one direction only: it compiles to a
fact, so Datalog always allows while `evaluate()` may return `escalate` (§4.9).

---

## 3. Encoding rule: `check if` versus `reject if`

Biscuit offers two clause forms, and choosing the wrong one silently breaks the caveat. This is
the most important rule in this document.

| Form | Semantics | Behaviour when the fact is **absent** |
|---|---|---|
| `check if BODY` | Passes iff BODY has at least one solution | **Fails** — denies |
| `reject if BODY` | Fails iff BODY has at least one solution | **Passes** — vacuous |

Both were verified as supported.

### 3.1 Normative rule

- A caveat constraining a fact the verifier supplies on **every** request (§7 of spec 01 —
  `operation`, `requested(dim, …)` for every dimension, `current_depth`, `request_intent`,
  `time`) MUST compile to **`check if`**.
- A caveat constraining a fact that may be **absent** for a given call (`tool`, `arg`) MUST
  compile to **`reject if`**.

### 3.2 Why this matters

Encoded as `check if`, an `ArgPredicate` on `payment.amount` denies an `invoice:read` call —
which carries no such argument — because the check finds no solution. Measured: exactly that.
Encoded as `reject if`, the same caveat is vacuous on unrelated calls and binding on payments.
Measured: correct in all three cases.

The converse error is worse. Encoded as `reject if time($t), $t > EXPIRY`, a `TimeWindow`
becomes **vacuous if the verifier omits `time()`** — the token never expires. `TimeWindow` is
therefore `check if`, and `time()` is mandatory request context. A caveat that fails open when
context is missing violates rule 6 (fail closed).

---

## 4. The nine caveat types

**On the count.** The language has **nine** caveat types. **Eight** of them compile to a Datalog
clause — `ScopeSubset`, `BudgetCeiling`, `TimeWindow`, `ToolAllow`, `ToolDeny`, `ArgPredicate`,
`DepthLimit`, `IntentBound` — which is the set named in T-008's acceptance criteria. The ninth,
`RequiresApproval`, compiles to a **fact** rather than a clause (§4.9, ADR-008), so it is
correctly absent from that list. `RateLimit` would be the tenth and is deferred (§5).

Every `<scaled>` value is an integer scaled by 10⁴ per spec 01 §3.1.

### 4.1 `ScopeSubset`

Limits which operations the holder may perform.

```datalog
check if operation($op), ["invoice:read", "vendor:read"].contains($op);
```

- **Consumes:** `operation`
- **`narrows`:** `ScopeSubset(S₁).narrows(ScopeSubset(S₂)) ⟺ S₁ ⊆ S₂`
- **Empty set:** denies every operation (EC-T14). Legal and meaningful — a token that may act
  on nothing but still carries identity for audit.
- **Reason code:** `SCOPE_ATTENUATED_AWAY` (authority block grant miss is `SCOPE_NOT_GRANTED`)
- **No wildcards** (EC-T15). Scopes are exact strings.

### 4.2 `BudgetCeiling`

Caps one quantitative dimension.

```datalog
check if requested("spend_bdt", $v), $v <= 500000000;
```

- **Consumes:** `requested(dimension, value)` — the verifier supplies one per dimension, always
- **`narrows`:** same dimension, `v₁ ≤ v₂`. **Different dimensions are not comparable** — raise
- **Zero:** a valid ceiling meaning "may not consume this at all"
- **Reason code:** `BUDGET_EXHAUSTED_CAVEAT` (the mandate ceiling is `BUDGET_EXHAUSTED_MANDATE`)

> This caveat bounds a **single request**. It does not and cannot bound the sum across
> siblings — see INV-5 in [`03-attenuation.md`](03-attenuation.md). The ledger does that.

### 4.3 `TimeWindow`

```datalog
check if time($t), $t >= 2026-08-14T11:45:00Z;
check if time($t), $t <= 2026-08-14T11:55:00Z;
```

- **Consumes:** `time` — mandatory context, never optional (§3.2)
- **`narrows`:** `[a₁,b₁].narrows([a₂,b₂]) ⟺ a₁ ≥ a₂ ∧ b₁ ≤ b₂`
- **Boundary:** upper bound exclusive (EC-T06); see spec 01 §5.3 for the PEP pre-check
- **Reason codes:** `TOKEN_EXPIRED`, `TOKEN_NOT_YET_VALID`

### 4.4 `ToolAllow`

```datalog
check if tool($t), ["invoice_api", "vendor_api"].contains($t);
```

- **Consumes:** `tool`
- **`narrows`:** `A₁ ⊆ A₂`
- **Reason code:** `TOOL_DENIED`

> `ToolAllow` uses `check if` despite `tool` being optional, and that is deliberate: a call with
> no tool identity MUST NOT satisfy an allow-list. Fail closed. The PEP MUST supply `tool` for
> every tool-directed call; a missing `tool` fact is a PEP bug, and denying is the correct
> response to it.

### 4.5 `ToolDeny`

```datalog
reject if tool($t), ["payment_api"].contains($t);
```

- **Consumes:** `tool` (optional — vacuous when absent)
- **`narrows`:** `ToolDeny(D₁).narrows(ToolDeny(D₂)) ⟺ D₁ ⊇ D₂`. **Note the reversed
  direction:** denying more is more restrictive
- **Reason code:** `TOOL_DENIED`

**Deny wins over allow.** Verified: with `ToolAllow({invoice_api, payment_api})` in one block and
`ToolDeny({payment_api})` in a later block, `payment_api` is denied and `invoice_api` allowed.
This needs no special ordering logic — all clauses in all blocks must pass, so a reject anywhere
in the chain wins (INV-8). Also verified: a later block adding `tool("invoice_api")` as a fact
cannot escape an earlier block's reject.

### 4.6 `ArgPredicate`

Constrains an argument of the call.

```datalog
reject if arg("payment.amount", $v), $v > 5000000;
reject if arg("email.domain", $d), !["example.com", "corp.example"].contains($d);
```

- **Consumes:** `arg(path, value)` (optional — vacuous when absent, §3.2)
- **Operators:** `<=`, `<`, `>=`, `>`, `==`, `!=`, `in`, `not in`
- **Numeric `arg` facts are scaled by 10⁴**, exactly like budgets, so one comparison rule
  covers every numeric term in the language. The verifier MUST scale numeric argument values
  when it builds the request context; string values are passed through unscaled.
- Compiles to `reject if` with the **negated** predicate, so the caveat is vacuous when the
  argument is absent and binding when present. `x <= v` becomes `reject if arg(p, $x), $x > v`.
- **`narrows`:** defined only for the **same path and comparable operator**:
  - numeric upper bounds: `v₁ ≤ v₂`; lower bounds: `v₁ ≥ v₂`
  - set membership: `S₁ ⊆ S₂`
  - different path, or non-comparable operators: raise — the caller treats an unrelated
    predicate as a *new* restriction, which is always narrowing (§6 of spec 03)
- **Reason code:** `ARG_PREDICATE_FAILED`
- **Paths** use dotted JSONPath-lite notation, resolved by the PEP's extractor (T-020). The path
  vocabulary is the extractor's contract, not this spec's.

### 4.7 `DepthLimit`

```datalog
check if current_depth($d), $d <= 3;
```

- **Consumes:** `current_depth` — computed by the verifier as `block_count - 1`, **never** read
  from a block fact (spec 01 §6.1, ADR-005)
- **`narrows`:** `n₁ ≤ n₂`
- **Reason code:** `DEPTH_EXCEEDED`

### 4.8 `IntentBound`

Pins the token to one approved task.

```datalog
check if request_intent($h), $h == "4f9a1c2d…";
```

- **Consumes:** `request_intent`
- **`narrows`:** `h₁ == h₂` and nothing else. An intent hash cannot be narrowed, only matched —
  a different hash is a *different task*, not a narrower one (INV-7)
- **Reason code:** `INTENT_MISMATCH`

### 4.9 `RequiresApproval` — the one that is not a Datalog clause

Forces the escalation path for a set of scopes. Its outcome is `escalate`, and **Datalog has no
third answer** — a biscuit authorizer returns allow or deny.

`RequiresApproval` therefore compiles to a **fact, not a clause**:

```datalog
requires_approval("payment:initiate");
```

The PEP reads it by parsing block sources (`Biscuit.block_source(i)`), reconstructing the caveat
set, and evaluating it in `agentiam-core`. Verified: block sources are readable for every block.

- **`narrows`:** `RequiresApproval(S₁).narrows(RequiresApproval(S₂)) ⟺ S₁ ⊇ S₂` — requiring
  approval for more scopes is more restrictive
- **Reason code:** `APPROVAL_REQUIRED`
- **Outcome:** `escalate`, never a silent deny

> **This is not a weaker guarantee.** The fact cannot be removed — biscuit blocks are
> append-only and signed. It can only be added to. And it cannot be forged into a *weaker*
> position by a descendant, because a descendant can only append. Verified separately:
> `authorizer.query` can read authority-block facts but returns **nothing** for block facts, so
> a block fact can never influence a Datalog decision — which is exactly why this one has to be
> evaluated in Python.

---

## 5. `RateLimit` — deferred

`RateLimit(n per window)` is deferred (`PLAN.md` §21 item 7). It is the one caveat requiring
verifier-side **state**: a count per window per token. Every other caveat is a pure function of
the request context, which is what keeps the hot path stateless and the latency claim (NFR-1)
defensible.

If it is implemented later it MUST NOT go in the token as a stateful check. The sound design is
a ledger dimension (`tool_calls` already exists) with the lease protocol enforcing it, which is
the existing machinery.

---

## 6. Duplicates, contradictions, and ordering

| Situation | Behaviour | Why |
|---|---|---|
| Same caveat type twice in a chain | Both apply; the stricter binds (EC-T12) | All clauses must pass, so the effective bound is the intersection |
| Contradictory caveats (`amount ≤ 100` and `amount ≥ 200`) | Denies everything, no crash (EC-T13) | Verified. An unsatisfiable intersection is a valid, useless token |
| Clause order within a block | Irrelevant | Datalog evaluation is order-independent |
| Block order | Irrelevant to the outcome; relevant to attribution | Deny attribution walks blocks in order and reports the **first** failing caveat, so messages are stable |

Because the effective bound is always the intersection, **adding any caveat can only narrow**.
That is the structural reason INV-1 holds regardless of what `narrows()` says — see spec 03 §3.

---

## 7. Reason code mapping

Every caveat maps to exactly one code from the closed enum in `PLAN.md` §6.9. P-18 asserts every
deny yields exactly one code, and a CI test asserts every code is reachable.

| Caveat | Reason code |
|---|---|
| `ScopeSubset` | `SCOPE_ATTENUATED_AWAY` |
| authority grant miss | `SCOPE_NOT_GRANTED` |
| `BudgetCeiling` (caveat) | `BUDGET_EXHAUSTED_CAVEAT` |
| authority budget | `BUDGET_EXHAUSTED_MANDATE` |
| `TimeWindow` upper / lower | `TOKEN_EXPIRED` / `TOKEN_NOT_YET_VALID` |
| `ToolAllow`, `ToolDeny` | `TOOL_DENIED` |
| `ArgPredicate` | `ARG_PREDICATE_FAILED` |
| `DepthLimit` | `DEPTH_EXCEEDED` |
| `IntentBound` | `INTENT_MISMATCH` |
| `RequiresApproval` | `APPROVAL_REQUIRED` (outcome `escalate`) |

Biscuit reports *that* authorization failed, not *which* clause failed, in a form we can rely
on. So the PEP MUST determine attribution by re-evaluating the reconstructed caveat set in
`agentiam-core` via `evaluate()`. This is the second reason `evaluate()` exists, and the reason
its agreement with `to_datalog()` is a security property rather than a nicety.

---

## 8. Canonical form

For `tokens.caveats` (JSONB, `PLAN.md` §7), for the console, and for hashing:

1. Caveats sort by `(block_index, kind, canonical_payload)`.
2. Set-valued fields serialize as sorted arrays; no duplicates.
3. Scaled integers serialize as JSON numbers, never strings.
4. Timestamps serialize as RFC 3339 UTC with a `Z` suffix, seconds precision.

P-13 covers stability under key reordering.

---

## 9. Verified behaviours

Executed against `biscuit-python` 1.x, CPython 3.12. Re-run these when the library version moves.

| # | Behaviour | Result |
|---|---|---|
| 1 | `ToolAllow` admits listed tools, denies others | confirmed |
| 2 | `!` negation is supported in expressions | confirmed |
| 3 | `reject if` is supported in attenuation blocks | confirmed |
| 4 | `reject if` is vacuous when its fact is absent | confirmed |
| 5 | `ToolDeny` beats `ToolAllow` across blocks | confirmed |
| 6 | A later block adding a benign `tool` fact cannot escape an earlier `reject` | confirmed |
| 7 | `ArgPredicate` numeric and set-membership forms both work | confirmed |
| 8 | `check if arg(...)` denies when the arg is absent — the reason for §3.1 | confirmed |
| 9 | `IntentBound` matches and mismatches correctly | confirmed |
| 10 | Nested `TimeWindow`s intersect; the inner bound binds | confirmed |
| 11 | `DepthLimit` in a child block binds at the verifier-supplied depth | confirmed |
| 12 | `Biscuit.block_source(i)` is readable for every block | confirmed |
| 13 | `authorizer.query` sees authority facts, **not** block facts | confirmed |

Finding 13 is the load-bearing one for §4.9: a block fact can never influence a Datalog
decision, so `RequiresApproval` must be evaluated in Python — and equally, no block fact can
ever widen a Datalog decision.

---

## 10. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | The `arg` path vocabulary and its extraction rules | T-020 |
| 2 | Whether `evaluate()` should return the failing clause index for finer attribution | T-019 |
| ~~3~~ | ~~Whether `role` should be a closed enum for console rendering~~ — **resolved in T-011**: no, see `01-token-format.md` §6.1 and ADR-013 | done |
