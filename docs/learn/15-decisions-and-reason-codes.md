# 15 — Decision records and reason codes

*Feature: every refusal names its exact cause.*

---

## 1. The principle

> **Every deny is explainable.** A decision record names the exact caveat, policy statement or
> budget that caused it. "Denied" without a reason is a bug.

That is design principle 4, and it is load-bearing. A system that says "denied" tells an
operator nothing about what to fix. A system that says `BUDGET_EXHAUSTED_CAVEAT` naming block 2
tells them the *parent agent's* ceiling is what refused, not the mandate — and those have
completely different fixes.

---

## 2. The codes

Grouped by which layer produces them.

**Token validity — the request never got started**

| Code | Meaning |
|---|---|
| `TOKEN_INVALID_SIGNATURE` | Forged, altered, truncated or spliced |
| `TOKEN_EXPIRED` / `TOKEN_NOT_YET_VALID` | Outside its validity window |
| `TOKEN_TOO_LARGE` | Beyond the size guard, rejected before parsing cost |
| `MALFORMED_REQUEST` | Unmapped route, missing `Bearer` scheme, duplicated parameter |
| `VERIFICATION_LIMIT_EXCEEDED` | The Datalog engine hit its explicit limits |

**Revocation**

| Code | Meaning |
|---|---|
| `TOKEN_REVOKED` | This token's own block was revoked |
| `ANCESTOR_REVOKED` | Something above it in the chain was |

**The token's own authority**

| Code | Meaning |
|---|---|
| `SCOPE_NOT_GRANTED` | The mandate never included this operation |
| `SCOPE_ATTENUATED_AWAY` | The mandate had it; **this chain narrowed it away** |
| `TOOL_DENIED` | A `ToolDeny` caveat |
| `ARG_PREDICATE_FAILED` | An argument failed a caveat's predicate |
| `DEPTH_EXCEEDED` | Deeper than the mandate allows |
| `INTENT_MISMATCH` | Claiming a different task |

**Budget**

| Code | Meaning |
|---|---|
| `BUDGET_EXHAUSTED_MANDATE` | Over the ceiling the **human** approved |
| `BUDGET_EXHAUSTED_CAVEAT` | Over a ceiling an **ancestor agent** set |
| `LEASE_UNAVAILABLE` | The PEP holds no usable lease right now |
| `LEASE_NOT_ACTIVE` | The lease was released or reaped |
| `RATE_LIMITED` | Too many requests |

**Policy and the humans**

| Code | Meaning |
|---|---|
| `POLICY_DENIED` | Cedar refused |
| `POLICY_BUNDLE_STALE` | The bundle is older than the staleness limit |
| `DRIFT_ESCALATION` | Off-task; a human must unblock it |
| `APPROVAL_REQUIRED` | A `RequiresApproval` caveat |

**Infrastructure**

| Code | Meaning |
|---|---|
| `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` | A dependency could not be consulted, so the answer is no |
| `UPSTREAM_ERROR` | The tool itself failed |

---

## 3. Pairs that look identical and are not

This is where the value is. Three pairs, each of which a lazier system would collapse into one:

**`SCOPE_NOT_GRANTED` vs `SCOPE_ATTENUATED_AWAY`** — did the human never grant this, or did an
agent in the chain give it up? The first means go back to the principal and widen the mandate.
The second means the delegation was wrong.

**`BUDGET_EXHAUSTED_MANDATE` vs `BUDGET_EXHAUSTED_CAVEAT`** — the money the human approved is
gone, versus an agent narrowed itself and hit its own limit. These read the same on a dashboard
and mean opposite things. They were one code until a manual pass noticed that the demo showed
"root exceeds the mandate" and "settlement exceeds its ceiling" as the same refusal.

**`TOKEN_REVOKED` vs `ANCESTOR_REVOKED`** — was I switched off, or was someone above me?

---

## 4. Which cause wins when several are true

A request can violate several rules at once. The record must name a **predictable** one, so the
order is fixed in advance rather than emerging from the code:

```
revocation  →  scope  →  intent  →  depth  →  caveats  →  policy  →  drift  →  budget
```

Intent is checked **before** the caveat loop specifically so a wrong task reports
`INTENT_MISMATCH` rather than whichever caveat happens to fail first. Sending an operator to fix
a budget ceiling when the real problem is that the agent is doing the wrong job is worse than
useless.

The block index is part of the answer too: knowing a `BudgetCeiling` failed is half the story;
knowing it was **block 2's** ceiling tells you which agent in the chain to look at.

---

## 5. What went wrong

### The failing caveat was always `None`

Spec 09 promised a `failing_caveat` field naming the exact restriction that refused. On every
record ever written, it was empty.

Two causes stacked. `decide()` takes the caveat list as an **input**, because a verified token
exposes the grant but not what later blocks added — and the deployed PEP had nothing to pass.
Reading a token's caveats back required the Datalog reader, which did not exist yet.

Once it did, the fix was to hand `decide()` the caveats read off the chain. An incomplete list
stays safe by construction: the caveat loop only ever *adds* a denial, and biscuit's own
authorizer enforces the chain regardless.

### A status code that told the wrong story

`BUDGET_EXHAUSTED_CAVEAT` maps to HTTP 429. That reads as "retry later" — for a ceiling that is
**permanent**. An agent that retries a request refused by its own parent's limit will retry
forever.

This was investigated and **deliberately left alone**, which is itself worth reading: the
alternative mappings were worse, and the reason code carries the real information for anything
that reads it. Written down as a known wart rather than silently accepted.

### Two log-assertion tests that quietly stopped asserting

Two tests checked that certain refusals were logged. When the whole suite ran in one process,
they went **vacuous** — the logging configuration from an earlier test had disabled the loggers
they were watching, so they passed by finding nothing to check.

The cause turned out to be a real product bug rather than a test bug: `fileConfig` disables
every existing logger by default, and a database migration was calling it — switching off the
application's own logging in production (ADR-060).

**A test that passes because it found nothing is the most dangerous kind**, because it is
indistinguishable from a test that passes because everything is fine.

---

## 6. Where to look

| Thing | File |
|---|---|
| Every reason code | `packages/agentiam-core/src/agentiam_core/errors.py` |
| Precedence and the decision | `packages/agentiam-core/src/agentiam_core/decision.py` |
| Record assembly | `packages/agentiam-pep/src/agentiam_pep/pipeline.py` |
| The normative rules | [`docs/specs/09-decision-record.md`](../specs/09-decision-record.md) |

---

## Next

[16 — Console, SDK and demo](16-console-sdk-and-demo.md).
