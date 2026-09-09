# 07 — Attenuation

*Feature: a parent agent hands a child strictly less power, by itself, offline.*

This is the differentiator. If you explain one thing about AgentIAM to somebody, explain this.

---

## 1. What it is

```python
child = attenuate(
    parent_token,
    [ScopeSubset(scopes={"invoice:read"}), BudgetCeiling(spend_bdt=Decimal("0"))],
    agent_id="agt-doc-reader",
    role="reader",
)
```

That call:

- runs **entirely locally** — no server, no network, microseconds;
- produces a token that **cryptographically cannot** exceed the parent;
- needs no cooperation from whoever issued the original token.

Compare with OAuth, where the only way to get a narrower credential is to ask the
authorization server for one, and the only way to give a sub-agent *something* is usually to
give it *everything*.

---

## 2. Why it is safe

The safety does not come from our code checking carefully. It comes from the token format.

A biscuit is append-only, and **every block's rules must pass** for the token to authorize
anything. So authority is an intersection:

```
authority(token) = block₀ ∩ block₁ ∩ block₂ ∩ …
```

Intersecting with anything can only make a set smaller or leave it the same. There is no block
anyone can append that adds authority back — not a malicious one, not a buggy one, not one
written by us.

That is invariant **INV-1**, and it is the promise the whole delegation story rests on:

> `authority(child) ⊆ authority(parent)`, for every parent and every set of caveats.

Four more follow from it:

| Invariant | Meaning |
|---|---|
| INV-2 Transitivity | Attenuating twice is narrower than either step alone |
| INV-4 Non-forgeability | Without the parent's block key you cannot produce a valid extension |
| INV-6 Depth bound | Each child is exactly one deeper; past `max_depth`, verification fails |
| INV-9 Expiry contraction | A child can expire sooner than its parent, never later |

---

## 3. The belt-and-braces check: `narrows()`

Biscuit's structure already guarantees the child cannot be wider *in effect*. But a parent
could still write a caveat that *claims* to be narrower and is not — say, a child ceiling of
৳900,000 under a parent ceiling of ৳200,000. The token would still be safe (the parent's
ceiling still binds), but the child's declared caveat would be a misleading number sitting in
the audit trail.

So `attenuate()` also refuses at mint time, using `narrows(child, parent)`.

**Three of the nine caveat kinds are where a hand-written implementation goes wrong**, and they
are exactly what the property tests hammer:

- **`ToolDeny`** runs *backwards*. Denying **more** tools is stricter, so a bigger set narrows.
- **`RequiresApproval`** likewise — requiring approval for more things is stricter.
- **`IntentBound`** narrows only by **equality**. A different intent hash is a *different task*,
  not a narrower one. Getting this wrong would let an attenuation quietly re-point a token at
  another job.

If two caveats are not comparable at all, `narrows()` raises rather than returning `False` —
because "not comparable" and "not narrower" are different facts, and silently conflating them
would hide a bug.

---

## 4. The sibling problem, and why tokens alone cannot solve it

Here is the thing that surprises people.

A parent with a ৳200,000 ceiling spawns three children and gives each of them a ৳50,000
ceiling. Every child token is individually valid. Every child stays inside its own limit.

Together they can spend ৳150,000 — which is fine. But nothing stops the parent giving each
child the *full* ৳200,000, and then the three of them spend ৳600,000 between them while every
token remains perfectly valid.

**Static inspection of a token cannot bound total spend across siblings.** This is invariant
**INV-5**, and it is the reason a ledger exists at all.

Two mitigations, and the project implements both:

- **Proportional split** — the parent explicitly divides the budget, so the sum is bounded by
  the tokens themselves.
- **Shared pool** — the children reference one ledger budget, and the lease protocol enforces
  the sum dynamically. **This is the default**, because it is what real workflows want.

This observation — that capability-token literature largely does not address *quantitative*
resources shared across siblings — is the project's most genuinely publishable point.
[File 09](09-budgets-and-leases.md) is the machinery.

---

## 5. What went wrong

### TM-19 — a caveat that looked like it enforced, and did not

**The symptom.** A token narrowed to `invoice:read` still authorized `vendor:read`.

**The root cause.** Biscuit checks are **existential**. This looks obviously correct:

```datalog
check if scope($s), ["invoice:read"].contains($s)
```

Read it in English: "check that the scope is in this list." But `$s` ranges over **the token's
own grant facts**, and `check if` succeeds when *some* binding satisfies the body. A token
granting both `invoice:read` and `vendor:read` has a binding that satisfies it — so the check
passes no matter which operation is actually being attempted.

The caveat was not enforcing anything. It was asking "does this token grant at least one thing
on this list?", which is almost always yes.

**The fix.** Checks are written against **verifier-supplied request facts**, not against the
token's own grant facts. The correct form joins the two:

```datalog
check if operation($op), scope($op)
```

— "the operation being attempted must be one of the granted scopes." `operation` comes from the
verifier and describes *this* request; `scope` comes from the token. Recorded as **ADR-005** and
stated normatively in spec 01 §2.

**Why it is worth remembering.** This is the single most important measurement in the project.
The wrong version is more natural to write and reads correctly in English. Nothing would have
caught it except running it — and a test written from the same misunderstanding would have
passed.

### TM-20 — a caveat that fails open when a fact is missing

**The root cause.** The mirror image. `reject if` is **vacuous** when its fact is absent:

```datalog
reject if time($t), $t > EXPIRY
```

If the verifier omits `time()`, nothing matches, nothing is rejected, and **the token never
expires**.

**The fix.** ADR-007 draws the line: `check if` for facts supplied on every single request,
`reject if` only for facts that are legitimately optional. And `RequestContext` validates at
construction that **every** budget dimension is present, defaulting to zero — so an incomplete
context is not a thing you can build. A red-team test covers every dimension individually.

### TM-27 — six checks in the token, one of them enforced by nothing

**The root cause.** The authority block carries six checks, and biscuit's authorizer enforces
all six — but `verify()` deliberately never calls `authorize()`. It extracts facts instead, so
that reason codes can be precise.

Which means every check that `decide()` did not separately re-implement was enforced *nowhere*
on the path the PEP actually ran.

Five of the six were covered by accident: scope by `decide()`, the validity window and depth by
`verify()`, the budget ceiling by the ledger. The sixth — **intent** — was covered by nothing.
Measured while wiring the pipeline: a request whose intent did not match the token's was
**allowed** by `decide()` while biscuit denied the identical request.

That is INV-7's binding and the main defence against task redirection, both decorative on the
live path.

**The fix.** `decide()` step 4 now checks intent and depth explicitly, ordered *before* the
caveat loop so a wrong task reports `INTENT_MISMATCH` rather than whichever caveat happens to
fail first.

**Why it is worth remembering.** Nothing was broken in any single component. Verification was
correct, the token was correct, `decide()` was correct at what it did. The gap was in the
**seam** — an assumption that the other side was checking. Only running the two side by side
found it.

---

## 6. Where to look

| Thing | File |
|---|---|
| `attenuate()` and `narrows()` | `packages/agentiam-core/src/agentiam_core/attenuation.py` |
| The invariant property tests | `tests/property/` |
| The normative rules and counterexamples | [`docs/specs/03-attenuation.md`](../specs/03-attenuation.md) |

Spec 03 is written specifically so that property tests can be derived from it — every invariant
is stated formally *and* given counterexamples describing what would violate it. A property
test derived from a wrong invariant statement proves nothing.

---

## Next

[08 — Caveats](08-caveats.md) — the restrictions themselves.
