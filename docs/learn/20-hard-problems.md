# 20 — Hard problems, and how they were solved

The bugs worth remembering, collected in one place. Read this after Part 2, when you know
enough for the stories to land.

Every one follows the same shape: **what looked true, what was actually true, how we found out,
what changed.**

---

## 1. The habit behind all of it

> **Before writing any specification, check its claims against a running system.**

That sounds like overhead for a documentation task. It was the opposite. Between the first spec
and the ledger it found **nine design errors** — every one of which would otherwise have shipped
as a *passing test written from the same wrong assumption*.

| Where | What the paper design said | What running it showed |
|---|---|---|
| Spec 01 | Caveats check the token's own scope facts | Checks are existential; the narrowing did nothing |
| Spec 01 | Depth comes from a `depth(n)` fact | A depth-9 chain authorized successfully |
| Spec 01 | Budget ceilings can be strings | Datalog cannot compare strings numerically |
| Spec 02 | One clause form fits all caveats | Half fail closed, half fail **open** |
| Spec 03 | `TimeWindow` narrowing is interval containment | Refuses the most common attenuation there is |
| Spec 04 | The seven lease operations are complete | `leased` went negative in 55 of 400 interleavings |
| Spec 04 | `ACQUIRE` clamps by `max_fraction` | All 50 concurrent callers get a grant; `available` never reaches 0 |
| Spec 04 | Dedup check, then lock the lease | Concurrent duplicates both pass, then crash on the primary key |
| SDK | `copy_context()` carries identity into a thread pool | Two threads entering one context raise `RuntimeError` |
| SDK | Escaping a `role` correctly is enough | `block_source()` renders it back **unescaped** |

Twice the thing measured was **our own specification** rather than a library: spec 04's
`ACQUIRE` formula cannot pass its own ticket's acceptance test, and its `LEDGER_COMMIT`
statement order is a time-of-check-to-time-of-use race.

The pattern was identical every time: the design was defensible on paper and wrong against the
library. **Reading harder would not have caught any of them.**

---

## 2. The five that matter most

### The caveat that enforced nothing

*Narrowing a token to `invoice:read` still authorized `vendor:read`.*

Biscuit checks are **existential**. `check if scope($s), [...].contains($s)` reads in English as
"check the scope is in this list", but `$s` ranges over the token's *own grant facts* and
succeeds if **any** binding matches. It was asking "does this token grant at least one thing on
this list?" — almost always yes.

**Fix:** checks are written against verifier-supplied *request* facts, joined to the token's:
`check if operation($op), scope($op)`.

**Why it is first on this list:** the wrong version is more natural to write and reads correctly
out loud. Nothing but running it would have caught it.

### The money that never reached the ledger

*Two milestones of "budget cannot be overspent" being false.*

The settle step computed its result and discarded it. No production code path called
`ledger_commit` at all. Chaos measured 992 requests spending ৳4,960 with the ledger recording
`committed = 0`, and a shutting-down PEP handing back leases it had already spent.

**The part worth carrying:** the invariant checker was running and **passing**. It asserted
`committed == Σ settled reservations`, and that held perfectly — as `0 == 0`. The books were
entirely consistent about a number that had stopped describing reality.

**Fix:** a settlement queue with unbounded retry, plus a drain-before-release hook to close a
second route where a top-up discarded queued settlements.

### Six checks in the token; one enforced by nothing

The authority block carries six checks and biscuit enforces all six — but `verify()`
deliberately never calls `authorize()`. So every check the decision function did not separately
re-implement was enforced *nowhere* on the live path.

Five were covered by accident. The sixth, **intent binding**, was covered by nothing: a request
whose intent did not match was **allowed** by our code while biscuit denied the identical
request.

**Why it is instructive:** nothing was broken in any component. Verification was correct, the
token was correct, the decision function was correct at what it did. The gap was in the
**seam** — each side assuming the other checked.

### The PEP that ran half its policy

*Every corpus case passed while two of the shipped policy's rules were inert in production.*

The deployed enforcement point built its Cedar engine with no tool catalogue, so every resource
resolved to safe defaults and both resource-attribute rules were dead. All 51 corpus cases
passed the whole time, because **every case builds its own engine with the catalogue in hand**.

And wiring it in was not the fix. The real blocker was one layer down: the Cedar role was a
single constant for the entire process, so a role-discriminating policy was always-on or
always-off, never discriminating.

**Fix:** the catalogue now ships **inside the signed bundle** — `sensitivity` is an
authorization input, so an unsigned catalogue is an authorization layer anyone with disk access
can rewrite. And roles are asserted per agent, keyed on the **delegation path** rather than the
agent's name, because the name is written by the parent.

**Proved by removing it.** With the role map emptied, the same payments went `200 OK →
403 POLICY_DENIED`. That is the difference between "the tests pass" and "I watched it work."

### The demo that died sixty seconds after boot

A lease leaves service two ways — it **drains**, or it **ages out**. The top-up logic only asked
about the first. A lease that expired *unspent* answered "no, still full", so no replacement was
ever scheduled and every payment was refused permanently.

Every automated path spends promptly, so every automated path took the drained branch. It needed
a human to start the stack, read the console for two minutes, and then try to pay.

**Fix:** reuse the refusal's own "no longer usable" predicate rather than writing a second one —
two conditions that must agree, expressed once.

**And then the sequel:** even repaired, scheduling is asynchronous, so the request that
*notices* the dead lease is still refused. The pool now renews on a timer. Nothing should have
to be refused to trigger the renewal that would have prevented it.

---

## 3. Recurring shapes

Once you have seen enough of these, the categories repeat.

**A default that is wrong for your workload.** A library's 1 ms authorizer timeout that is
**wall clock, not work**, refusing legitimate requests under load. Every limit is now set
explicitly, so it is bounded on purpose rather than by accident.

**Something exists and nothing calls it.** `reap()` specified and never scheduled.
`decision_span` never invoked from production code. `ledger_commit` with no production caller.
`AGENTIAM_PEP_DEFAULT_ROLE` documented as configuration and never read by anything.

> Grepping for **non-test callers** of a function you believe is wired is a five-second check
> that has found four separate defects in this project.

**A check that runs against its own output.** A CI job regenerated the benchmark data and then
asserted the committed document matched it. It could never pass.

**Two computations of one value.** The intent hash, computed two ways in two places, so an
agent asserting the *correct* intent was refused every call while one that said nothing was
allowed. Fixed with a name, not a formula.

**A gate that exists only in the UI.** The policy activation button was disabled in the
template, and a direct POST installed unparseable Cedar anyway.

**Retry N times then give up.** The audit emitter dropped batches after a fixed retry count — a
path written for a permanently bad record, applied to a temporarily unreachable database. Ask
whether *at this layer* you can tell "never" from "not now". If not, you may not give up.

**A string that means one thing where it is checked and another where it is used.** A crafted
role rendering back into two facts (TM-24). A duplicated query parameter where our parser reads
`1` and the upstream reads `999999` (TM-26). Same shape, two years of CVEs behind it.

---

## 4. Three that were investigated and deliberately *not* changed

Worth as much as the fixes, because knowing when not to act is harder.

**A status code that reads wrong.** `BUDGET_EXHAUSTED_CAVEAT` maps to 429, which suggests "retry
later" for a permanent ceiling. Every alternative mapping was worse, the reason code carries the
real information, and it is recorded as a known wart.

**A revocation that outlives its `expires_at`.** Looks like a bug. Is not: the field is the
*original token's* expiry, kept so the row can be pruned. Honouring it as a deadline would
silently un-revoke a token someone revoked on purpose.

**Four lease expirations after the renewal fix.** Looked like the fix failing. Was not — the
control plane's own logs stopped and resumed at the same moments. The host machine had been
sleeping, and every container froze together. Checked before filing.

---

## 5. What to take away

1. **Measure before you specify.** Nine design errors, all found this way.
2. **Test the composition root, not just the component.** Most expensive bugs here lived in the
   wiring between correct parts.
3. **Ask what your check would say if the feature were deleted.** If it still passes, it is not
   a check.
4. **A flaky test is a bug report until proven otherwise.**
5. **Grep for non-test callers.** Repeatedly effective, nearly free.
6. **Write the gap down.** Every honest limitation in these files is one a reviewer will not
   discover for themselves and lose trust over.

---

## Next

[21 — Known gaps](21-known-gaps.md).
