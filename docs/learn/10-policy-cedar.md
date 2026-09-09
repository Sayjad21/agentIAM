# 10 — The policy layer

*Feature: what the organization permits, regardless of what any token says.*

---

## 1. Why a second layer at all

The token answers *what did this chain of delegation permit?* That is a question about history:
who granted what to whom.

It cannot answer *what does the organization permit at all?* Suppose compliance decides today
that no agent below depth 2 may touch the payment API. Every token already in circulation is
still valid, and none of them knows about the new rule.

So there is a second layer, centrally managed, evaluated on every request:

> **Both must pass. Neither can widen the other.**

The token layer travels with the agent and works offline. The policy layer is what a compliance
team can read and change without reissuing anything.

---

## 2. What it looks like

Policy is written in **Cedar** — AWS's open policy language. Not one we invented; writing our
own policy language is on the explicit out-of-scope list.

The demo's shipped policy, in full:

```cedar
permit(principal, action == Action::"invoice:read", resource);
permit(principal, action == Action::"vendor:read", resource);

permit(principal, action == Action::"invoice:write", resource)
when { principal.role == "senior" };

permit(principal, action == Action::"payment:initiate", resource)
when {
  context.amount.lessThanOrEqual(decimal("500000.0")) && principal.depth <= 2
};

permit(principal, action == Action::"email:send", resource)
when { !resource.is_external };

forbid(principal, action, resource)
when { resource.sensitivity == "critical" && principal.role != "senior" };

forbid(principal, action == Action::"payment:initiate", resource)
when { decimal("1000000.0").lessThan(context.amount) };
```

Readable by a person who is not a programmer, which is much of the point.

---

## 3. How it is kept safe

### It is a signed bundle

Policy ships as a **bundle**: the Cedar source, a version label, a monotonic serial, and the
tool catalogue. Signed with Ed25519 over canonical JSON.

An unverified bundle is an authorization layer anyone with disk access can rewrite, so the
signature is checked at boot and a failure means the service **refuses to start**. Not "load it
anyway and warn".

### Rollback is caught by the serial, not the name

A correctly-signed *old* bundle is a real attack — the signature proves nothing is wrong with
it, only that it is genuine. So a bundle whose serial does not advance is refused.

The serial is an integer and the version is a label, deliberately separate: string labels do not
order. `"bundle-10" < "bundle-9"` is true as a string comparison, and a rollback defence that
depends on naming is not a defence.

### Anything that is not "Allow" is a denial

Cedar's decision type has **three** members, not two: `Allow`, `Deny`, and `NoDecision` — the
last returned when the policy set fails to parse.

Writing `if decision == Deny` therefore lets a **corrupt bundle through as not-denied**. The
code tests `is Allow` instead, so a corrupt bundle and any fourth member a future library
version adds both fail closed (ADR-028).

### It is parsed once

Re-parsing per request measured 167.7 µs against 80.1 µs pre-parsed — 17% of the entire 1 ms
budget versus 8%. So the policy set is parsed at construction, and a bundle that does not parse
is rejected at load rather than at the first request that touches it.

---

## 4. The activation gate

Before a new bundle can go live it must pass a **51-case corpus** derived from the demo
workflows. Each case names its expected outcome and explains *why* that outcome is correct — a
corpus whose rows nobody can explain is a corpus nobody will maintain.

Try to activate a blanket `permit(principal, action, resource)` and you get:

```
409  Refused: 24 of 51 corpus tests failed — beat1_worker_reads_invoices,
     beat1_worker_reads_vendors, ... (and 19 more). The previous policy stays.
```

**409, not 422**, and enforced server-side. It had previously been a UI-only check — the button
was disabled, and a direct POST installed unparseable Cedar anyway (ADR-039). A gate that only
exists in the template is not a gate.

---

## 5. The resource catalogue

Cedar policies read attributes off the resource: `resource.sensitivity`, `resource.is_external`.
Those come from a **tool catalogue**:

```json
{ "payment_api": { "server": "bank", "sensitivity": "critical", "is_external": true },
  "invoice_api": { "server": "erp",  "sensitivity": "low" } }
```

A tool the catalogue has never heard of resolves to the **safe end of every axis** —
`sensitivity: "low"`, `is_external: false` — so an unknown tool cannot accidentally satisfy a
policy written about a sensitive one.

**The catalogue lives inside the signed bundle.** That was not the original design and the
reason it changed is the next section.

---

## 6. What went wrong

### The deployed enforcement point had no catalogue at all

**The symptom.** None. Every test passed. The policy looked like it was working.

**The root cause.** The deployed PEP built its Cedar engine with no catalogue argument. The
constructor's default was an empty dictionary, and every lookup fell back to the safe-default
unknown tool.

So in the deployment **every tool looked low-sensitivity and internal**, whatever the catalogue
said — and both resource-attribute rules in the shipped policy were **inert**:

```cedar
forbid(...) when { resource.sensitivity == "critical" && principal.role != "senior" };
permit(... "email:send" ...) when { !resource.is_external };
```

Five corpus cases assert the first one. All of them passed throughout, because **every corpus
case builds its own engine with the catalogue in hand.** Nothing asserted that the *deployed*
engine had one.

**And wiring it in was not the fix**, which is what made this interesting. Measured, wiring the
catalogue under the deployment's configuration refused **every payment in the demo**.

The real blocker was one layer down: `principal.role` was a single constant for the whole
process. Every agent a PEP served got the same Cedar role. A policy that discriminates on role
is then **always-on or always-off, never discriminating** — and the corpus asserts both halves
of a discrimination the deployment could not make.

**The fix, in two parts.**

1. The catalogue now travels **inside the signed bundle**. `sensitivity` is an authorization
   input, so a catalogue mounted beside the bundle is an authorization layer anyone with disk
   access can rewrite — downgrade `payment_api` to `low` and the forbid is disarmed silently,
   because every request still returns a decision. Shipping them together also means one serial
   names both, so a policy and the attributes it reads cannot drift apart.

2. Roles are now asserted **per agent**, from operator-controlled configuration. The trust
   boundary does not move — the role still comes from the organization and never from the token
   — only the *arity* changes. Configuration could previously say one word for the whole
   process; now it can say the truth about each agent.

**The subtle part: what the role map is keyed on.** The obvious key is the agent's id. That is
wrong for exactly the reason the whole layer exists: `agent_id` is written by the **delegating
parent**, so keying a role on it would let any agent name its child `agt-payer` and collect
`agt-payer`'s authority.

So the key is the full **delegation path** — `agt-payer/agt-settlement`. A block can only be
appended below the chain that already exists, so an agent can forge paths that extend its own
and no others. The worst it reaches is a role the organization gave to its own descendant. Even
that is refused at boot, by a check that rejects any map where a descendant outranks an
ancestor — because at request time the forged chain *is* a real chain and cannot be told apart.

**Proved live**, which is the part that mattered. Same stack, only the role map emptied:

```
payer settles a small invoice           OK  →  403 POLICY_DENIED
settlement agent pays within its slice  OK  →  403 POLICY_DENIED
```

That is the forbid firing in a deployed PEP for the first time, and it shows both halves are
load-bearing.

**The lesson.** Every test passed for two milestones because every test supplied the missing
input itself. When a component takes a dependency as a constructor argument, ask what happens
when the real composition root forgets to pass it — and write the test that asserts the
*deployed* object has it, not the one your fixture built.

---

## 7. Where to look

| Thing | File |
|---|---|
| The Cedar engine and catalogue | `packages/agentiam-pep/src/agentiam_pep/policy.py` |
| Bundle loading, staleness, rollback | `packages/agentiam-pep/src/agentiam_pep/policy_cache.py` |
| Signing and verification | `packages/agentiam-core/src/agentiam_core/bundles.py` |
| The 51-case corpus | `packages/agentiam-core/src/agentiam_core/corpus.py` |
| The normative rules | [`docs/specs/05-policy.md`](../specs/05-policy.md) |

---

## Next

[11 — Intent and drift](11-intent-and-drift.md).
