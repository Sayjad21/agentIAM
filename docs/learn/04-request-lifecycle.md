# 04 — How a request flows

One tool call, from the moment it hits the enforcement point to the moment it becomes a
permanent record. Ten steps. This is the spine of the system; every feature file later is a
close-up of one of these.

---

## 0. Where the request comes from

An agent wants to do something, so it calls a tool. It does not call the tool directly — it
calls the **PEP**, which proxies to the tool. That is the whole adoption story: change the base
URL, gain enforcement.

```
agent ──▶ PEP :8082 ──▶ tool :8081
             │
             └──▶ control plane :8000   (asynchronously — never on this path)
```

The call carries an `Authorization: Bearer <token>` header. Optionally it also carries
`AgentIAM-Task-Intent`, the agent's own statement of what it thinks it is doing.

---

## The ten steps

### Step 1 — Extract

Turn an untrusted HTTP request into a structured `RequestContext`: which operation, which
tool, which arguments, how much money.

This is the only step that touches a wire format, and it is more dangerous than it looks. A
route table maps `(method, path)` to a scope, a tool, and where each argument lives:

```json
{ "method": "POST", "path": "/payments", "scope": "payment:initiate",
  "tool": "payment_api", "args": { "amount": "body.amount" } }
```

Unmapped path? `401 MALFORMED_REQUEST` — deliberately not a 404, because the PEP refuses to
forward anything it cannot describe.

> **The subtle danger.** Two parsers read every request: ours and the upstream's. If they
> disagree about what `amount` is, the PEP can check a caveat against `1` while the tool acts
> on `999999`. This is threat **TM-26** and it is real — see [file 20](20-hard-problems.md).
> The fix is that the extractor never picks a winner: a duplicated parameter that a mapping
> rule names is refused outright.

### Step 2 — Verify

Check the token's signature chain against the root public key, and read out its facts.

This is pure cryptography and pure local computation. **No network, no database.** That
property has a name — INV-3, offline soundness — and it is what makes everything after it fast.

Verification also enforces the validity window and the chain's depth. A token whose signature
is wrong, that has been truncated, spliced, or reordered, dies here.

### Step 3 — Revocation

Is this token, or any of its ancestors, revoked?

The PEP holds a local revocation set kept fresh two ways: a Redis push channel for speed, and
a periodic full pull for correctness. If the check itself cannot be made, the request is
**refused**, not allowed — that is fail-closed, and it is the default everywhere.

Revoking a parent kills every descendant: `ANCESTOR_REVOKED`.

### Step 4 — The token's own authority

Now the token layer answers *what did this chain of delegation permit?* Four things, in a
fixed order:

1. **Scope** — is this operation in the mandate's grant? If the chain narrowed it away →
   `SCOPE_ATTENUATED_AWAY`.
2. **Intent** — does the claimed intent match the hash the mandate was bound to? If not →
   `INTENT_MISMATCH`.
3. **Depth** — is this agent deeper than the mandate allows?
4. **Caveats** — every restriction any ancestor wrote, evaluated against this request. A
   parent's ceiling refuses here as `BUDGET_EXHAUSTED_CAVEAT`.

> **The order is a design decision, not an accident.** When several causes are true at once,
> the record must name a *predictable* one. A wrong task reported as "some caveat failed"
> sends an operator to fix the wrong thing.

### Step 5 — Policy

The second layer: *what does the organization permit at all, regardless of token?*

An in-process Cedar engine evaluates a signed policy bundle. No network call — the bundle was
verified at boot and parsed once. A refusal here is `POLICY_DENIED`.

This is where the demo's depth rule lives: payments are permitted only at
`principal.depth <= 2`, so `agt-subcontractor` at depth 3 is refused while holding a perfectly
valid token.

### Step 6 — Drift

Does this action still look like the task the human approved?

The drift detector scores the action against the intent. Crucially, **drift escalates; it never
denies.** A false positive that blocks legitimate work would make the whole feature
unshippable, so the worst it does is raise a flag for a human. If the scoring model is
unreachable, the step fails **open** — the only place in the system that does, and it is
deliberate.

### Step 7 — Budget

Is there money for this, and can we hold it?

The PEP checks its **local lease**. No ledger call. If the lease covers the amount, a
reservation is held; if it does not, the request is refused (`LEASE_UNAVAILABLE` when the PEP
has no usable lease, `BUDGET_EXHAUSTED_MANDATE` when the grant itself is spent).

Steps 3 through 7 together are the function called `decide()`, and they are **pure** — no I/O,
no clock of their own. That purity is what makes them cheap to test exhaustively and is why
the p99 latency claim is measurable at all.

### Step 10 — Record (yes, before step 8)

The decision record is written **before** the request is forwarded.

That ordering is forced, not stylistic: a full audit buffer must *deny*, and a policy that
denies after the tool has already acted is not a policy. Everything the record needs — the
verdict, the cause, the reservation, the budget before and after — is known the moment step 7
finishes.

### Step 8 — Forward

Now, and only now, the request goes to the actual tool. The response comes back to the agent
essentially untouched. **An allow is invisible by design** — the agent sees exactly what it
would have seen without the PEP in the way.

### Step 9 — Settle

The tool says what actually happened, which may differ from the estimate. The reservation is
settled at the real amount, the difference is released, and the ledger is told.

That last part is asynchronous, batched, and retried forever on transient failure — and for a
long time it silently did not happen at all, which was the worst bug in the project's history.
[File 09](09-budgets-and-leases.md) tells that story.

---

## What the whole thing costs

Measured, not estimated:

| Step | Median |
|---|---|
| Verify (step 2) | ~216 µs |
| Cedar policy (step 5) | ~132 µs |
| The token's own Datalog (step 4) | ~111 µs |
| Everything else inside `decide()` | single-digit µs |

`decide()` p99 lands around 430 µs against NFR-1's 1 ms budget. Per-request in-process cost is
roughly verify + decide + extract — about 513 µs — and *that*, not NFR-1 alone, is what bounds
throughput.

Two steps are the decision: Cedar and the token's own rules. Everything else is noise.

---

## The two things that never happen on this path

1. **No network call is needed to refuse.** Every deny above is decided locally. Denying a
   payment for an agent with a ৳0 ceiling costs microseconds and touches nothing.
2. **No mutation of the token.** Elevation issues a new token; narrowing creates a new token.
   Tokens are immutable values, always.

---

## Next

[05 — Map of the code](05-codebase-map.md) shows where each of these steps lives and why the
package boundaries are drawn where they are.
