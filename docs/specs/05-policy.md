# Spec 05 — Org Policy and the `PolicyEngine`

**Status:** accepted · **Ticket:** T-024 · **Implements:** `PLAN.md` §6.5, §9 T-024
**Depends on:** [`09-decision-record.md`](09-decision-record.md), [`02-caveat-language.md`](02-caveat-language.md)
**Consumed by:** T-024, T-025, T-026, T-027, T-030

> `PLAN.md` §3.2: **two-layer authorization.** The token's Datalog answers *what did this
> chain of delegation permit?* Cedar answers *what does the organization permit at all,
> regardless of token?* Both must pass, and neither can grant what the other denies.
>
> This document is the second layer's contract. The parts worth checking are §4 — where a
> third decision value that is neither allow nor deny has to be mapped by hand — and §6,
> where the measured cost changes what the project can claim about NFR-1.

---

## 1. Why two layers

A token is portable and offline-verifiable, which is exactly what makes it a poor place to
put organizational rules. A token minted this morning cannot know that at noon the company
stopped allowing external payments over ৳100,000. Revoking and re-minting every token in
flight to express that is not a policy mechanism; it is an outage.

So: caveats are **per-delegation and travel with the token**; Cedar is **org-level, centrally
managed, and evaluated fresh on every request** against a cached bundle.

They compose by intersection. Step 4 (caveats) runs before step 5 (policy) and the first
failing step wins (spec 09 §3), so a token that never granted the scope is reported as
`SCOPE_NOT_GRANTED` rather than as a policy denial — the operator's fix differs.

**Neither layer can widen the other.** Cedar cannot grant a scope the token does not carry,
because step 4 already denied. A caveat cannot override a `forbid`, because step 5 still runs.

---

## 2. The entity model

From `PLAN.md` §6.5, unchanged:

```
Principal:  AgentIAM::Agent   attrs: role, depth, task_id, principal_id
Action:     AgentIAM::Action  "invoice:read", "payment:initiate", …
Resource:   AgentIAM::Tool    attrs: tool_id, server, sensitivity, is_external
Context:    { time, amount, arg_digest, drift_score, elevated, environment }
```

`depth`, `task_id` and `role` change per request, so the principal entity is rebuilt on every
call and the entity set cannot be parsed once and reused.

**Measured**, because the alternative was tempting: moving those facts into `context` and
pre-parsing a static entity set costs **78.5 µs** against **83.0 µs** for rebuilding the
principal each time — under 5%. Cedar's own model treats entities as the durable graph and
context as the request, so the plan's placement is also the idiomatic one, and it is kept. A
5% saving is not a reason to make `principal.depth` unavailable to a policy author who
reasonably expects it.

The **action** namespace is the same scope vocabulary the token uses (`PLAN.md` §2), so a
policy and a caveat name the same operation the same way. That is deliberate: two vocabularies
for one concept is where the two layers would start to disagree.

---

## 3. What the engine is given, and what it returns

```python
class PolicyEngine(Protocol):
    def evaluate(self, context: RequestContext) -> PolicyVerdict: ...
```

The protocol already exists in `agentiam_core.decision` (T-019). This document fixes what an
implementation must do with it.

| Returned | Meaning |
|---|---|
| `allowed=True` | Some `permit` matched and no `forbid` did |
| `allowed=False` | Denied — `statement` names the determining policy |
| `statement` | The policy id Cedar reports as the reason, or `None` when nothing matched |
| `version` | The bundle version, recorded on every decision record |
| `stale` | The cached bundle is older than `max_staleness` (T-025) |

`statement` is not decoration. `PLAN.md` §3.2 principle 4 requires every deny to name its
cause, and spec 09 §4 puts that string in `reason_detail`. **Measured:** `cedarpy` reports
`diagnostics.reasons` as a list of policy ids (`['policy0']`), so this is satisfiable — an
engine that could only answer allow/deny would fail this spec.

---

## 4. Three decision values, and the one that is a trap

**Measured:** `cedarpy.Decision` has **three** members — `Allow`, `Deny`, and `NoDecision`.
`NoDecision` is returned when the policy set fails to parse, with the parse errors in
`diagnostics.errors`.

```python
r = cedarpy.is_authorized(request, "this is not cedar", entities)
r.decision            # Decision.NoDecision
r.decision is Decision.Deny   # False
r.diagnostics.errors  # ['policy parse errors:\nunexpected token `is`']
```

So the obvious implementation is wrong:

```python
if response.decision == Decision.Deny:   # WRONG — NoDecision falls through as "not denied"
    return PolicyVerdict(allowed=False)
return PolicyVerdict(allowed=True)
```

**An implementation MUST treat anything that is not `Allow` as a denial.** Written as
`allowed = response.decision is Decision.Allow`, so a fourth member added by a future Cedar
release also fails closed rather than silently permitting. This is the whole reason this
section exists: the failure mode of the natural spelling is a policy layer that stops
enforcing the moment its bundle is corrupt.

An empty policy set — a syntactically valid bundle with no statements — returns `Deny` with
no reasons, which is correct: **deny by default**, confirmed by measurement rather than
assumed.

---

## 5. The bundle

```
{serial, version, cedar_source, entity_schema, created_at, signature}
```

T-024 consumed `version`, `cedar_source` and optionally `entity_schema`. Signature
verification, staleness, rollback and hot reload are **T-025** and are specified in
§5.1–§5.4 below.

**A bundle is parsed once, at load, not per request** — see §6. A bundle whose source does not
parse MUST be rejected at load rather than at the first request that touches it. `cedarpy`
supports this deliberately: `format_policies()` raises `ValueError: cannot parse input
policies` on bad source, and `PolicySet.from_str()` is the parse step itself.

Rejecting at load is what keeps §4's `NoDecision` an impossible state in production rather
than merely a handled one.

### 5.1 What is signed, and how

**Ed25519, detached, over the bundle's canonical JSON** — `hashing.canonical_json` from T-005,
the same canonicalization the audit chain uses. That reuse is the point: two serializations of
one bundle must produce one signature, or re-encoding a bundle in transit breaks it.

The signed payload is every field **except** the signature itself:

```
{serial, version, cedar_source, entity_schema, created_at}
```

**Measured**, against `cryptography` 50:

| | |
|---|---|
| Signature | 64 bytes |
| Public key, raw | 32 bytes — 64 hex characters, which is what an operator pastes into config |
| Determinism | Ed25519 is deterministic: the same bundle and key always produce the same signature |
| A bad signature | **raises `InvalidSignature`**; it does not return `False` |

That last row shapes the API. A library returning a boolean invites `if verify(...)` being
written where `if not verify(...)` was meant, and the failure of that typo is *accepting every
bundle*. Verification here raises, and the caller cannot ignore it by accident.

Four tamper shapes were checked and all four raise: a flipped signature bit, an altered
payload, an empty signature, and a signature from a different key.

### 5.2 Rollback: the serial, not the label

`version` is a **label** — `"2026-08-15.3"`, whatever an operator finds readable. It is what
lands in `DecisionRecord.policy_version`, and it is *not* what rollback protection compares.

`serial` is a monotonically increasing integer, and the cache **refuses any bundle whose serial
is not greater than the one it currently holds** — even when the signature is perfectly valid.
That is the rollback attack: an attacker who captures an old, legitimately-signed bundle replays
it to restore a permission that has since been removed. Signature verification alone cannot see
anything wrong with it.

Two fields for what looks like one concept, deliberately. String labels do not order —
`"bundle-10" < "bundle-9"` lexicographically — and a rollback defence that depends on how an
operator names things is not a defence.

### 5.3 Staleness, and what happens on a bad bundle

| Situation | Behaviour |
|---|---|
| Bundle older than `max_staleness` (default 300 s) | `stale=True`, and spec 09 denies with `POLICY_BUNDLE_STALE` |
| A new bundle fails signature verification | **Rejected; the cache keeps serving the previous bundle** and the failure is counted |
| A new bundle has a serial ≤ the current one | Rejected the same way |
| No bundle has ever loaded | `OracleUnavailable` → `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` |

The second row is `PLAN.md` §11's EC-P01, and it is the one that looks inconsistent. Everything
else here fails closed; a bad bundle does not. The reason is that the alternatives are worse: a
forged bundle is *evidence of an attack in progress*, and responding by discarding the last known
good policy would let an attacker disable the policy layer by sending garbage. Keeping the
previous bundle means the attacker achieves nothing, and the staleness clock keeps running
underneath — so if the real bundle never arrives, the PEP fails closed on age anyway.

**Rejection is never silent.** The cache counts rejections and `/readyz` exposes them; a bundle
being refused repeatedly is the signal EC-P01 asks to alert on.

### 5.4 Hot reload

Loading a new bundle parses and verifies it **before** anything is swapped, then replaces one
reference. In-flight requests hold the engine they started with and finish against it; the next
request picks up the new one. There is no lock on the read path, because there is nothing to
lock — a Python attribute rebind is atomic under the GIL, and a request that began under bundle
*n* completing under bundle *n* is the correct outcome rather than a compromise.

A bundle that fails to parse or verify never becomes the current one, so a request can never
observe a half-loaded bundle.


---

## 6. Cost, and what it does to NFR-1's headroom

**Measured**, per authorize, on the development host:

| Arrangement | Cost |
|---|---|
| Policy source re-parsed on every call | 167.7 µs |
| `PolicySet.from_str` once, entities per request | **80.1 µs** |
| Both pre-parsed (not reachable — §2) | 61.7 µs |

**Pre-parsing the policy set is mandatory**, not an optimisation: it is a 2× difference, and
the naive spelling puts 17% of NFR-1's entire budget into one step.

The honest consequence, which supersedes a claim made in T-019:

> T-019 measured `decide()` at **~5.2 µs** and recorded *"about 200× inside the 1 ms budget…
> `PLAN.md` §17 R-2 can be considered closed rather than pending."*

That measurement excluded step 5, because no policy engine existed. With Cedar the decision
costs roughly **85 µs**, so the headroom is about **12×**, not 200×. R-2 (*p99 over 2 ms by M8
triggers a port to Rust*) is therefore **not closed** — it is comfortable, on one machine,
with a small bundle. It should be re-measured under T-053's load profile with a realistic
bundle, because the one variable this spec cannot bound is how large a policy set an operator
writes.

The engine is still in-process and makes no network call, which is the property `PLAN.md` §3.2
actually requires of the hot path.

---

## 7. Two engines behind one protocol

`CedarEngine` is the implementation. `OpaEngine` exists as a stub raising
`NotImplementedError`.

The stub is not padding. `PLAN.md` §6.5 wants the abstraction demonstrated rather than
asserted, and a protocol with exactly one implementation is a protocol nobody has tested the
shape of. The stub costs a few lines and proves the seam is real — an OPA sidecar is an
out-of-process call, so an implementation that could not express that would be an abstraction
over nothing.

Full OPA is deferred (`PLAN.md` §21).

---

## 8. Known limitations

| # | Limitation | Bound |
|---|---|---|
| 1 | No schema validation of the bundle by default | Cedar validates policies against an `entity_schema` when one is supplied; T-026's activation gate is where that becomes mandatory. Without it, a typo in an entity type is a policy that silently never matches — and never matching means never permitting, so it fails closed |
| 2 | Cost is measured with a small bundle | §6. A large policy set costs more; T-053 measures a realistic one |
| 3 | `arg_digest` is in context, the arguments are not | A policy cannot say *deny payments to account X* unless the extractor maps that argument (spec 10 §4). Deliberate: NFR-5 keeps argument values out of everything that is recorded |
| 4 | One bundle per PEP, no per-tenant bundles | Single-tenant is the scope (`PLAN.md` §1.4) |

---

## 9. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Whether the bundle should be validated against `entity_schema` at load, or only at activation | T-026 |
| ~~2~~ | ~~Whether `stale` should deny immediately or serve a grace window with a flag~~ — **resolved in T-025: deny immediately.** A grace window is a second staleness limit with a friendlier name, and it makes the failure mode *policy silently out of date* rather than *policy refused*. The operator's fix is the same either way — refresh the bundle — and `max_staleness` is already the knob for how much staleness is tolerable. Setting it to 600 s is the grace window, stated once | done |
| 3 | Whether policy evaluation should be cached by `(scope, tool, principal, rounded amount)` — worth it only if T-053 shows step 5 dominating | T-053 |
