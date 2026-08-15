# Spec 09 — The Decision Pipeline and its Record

**Status:** accepted · **Ticket:** T-019 · **Implements:** `PLAN.md` §3.1, §6.9
**Depends on:** [`01-token-format.md`](01-token-format.md), [`02-caveat-language.md`](02-caveat-language.md), [`03-attenuation.md`](03-attenuation.md), [`04-lease-protocol.md`](04-lease-protocol.md)
**Consumed by:** T-019, T-020, T-022, T-046, T-048

> `PLAN.md` §3.2 principle 4: *every deny is explainable — a decision record names the exact
> caveat, policy statement, or budget that caused it. "Denied" without a reason is a bug.*
>
> That sentence is the whole reason this document exists. Naming a cause is only meaningful if
> the pipeline agrees, in advance, on **which** cause to name when several are true at once.
> That agreement is §3 below, and it is the part a reviewer should check.

---

## 1. What a decision is

A decision is a pure function of a verified token, a request context, and four locally-held
facts: what is revoked, what policy says, what the drift score is, and what budget remains.

**It performs no I/O.** `PLAN.md` §3.1 annotates steps 2 through 7 with *µs* and §3.2 requires
the hot path never to block on the network to reach a verdict. Leases, revocation sets and
policy bundles are all held locally and refreshed asynchronously, so the decision itself is
arithmetic over in-memory state. That is what makes NFR-1 (p99 < 1 ms) achievable and what puts
`decision.py` in `agentiam-core`, where `tests/unit/test_core_purity.py` enforces the absence of
I/O statically.

The outcome is one of four (`PLAN.md` §6.9):

| Outcome | Meaning |
|---|---|
| `allow` | Proceed. `reason_code` is `OK`, and is `OK` **only** here |
| `deny` | Refuse. `reason_code` names why |
| `escalate` | Refuse *for now*, and raise to a human. A first-class outcome, not a flavour of deny |
| `allow_with_flag` | Proceed, and mark the record for review |

---

## 2. The ten steps

Steps 1–7 decide. Steps 8–10 act on the decision and belong to the PEP, not to this function.

| # | Step | Where it runs | Can produce |
|---|---|---|---|
| 1 | Extract token, scope, tool, args | PEP (T-020) | `MALFORMED_REQUEST` |
| 2 | Verify the biscuit chain | `tokens.verify` (T-007) | `TOKEN_TOO_LARGE`, `MALFORMED_REQUEST`, `TOKEN_INVALID_SIGNATURE`, `TOKEN_NOT_YET_VALID`, `TOKEN_EXPIRED`, `DEPTH_EXCEEDED`, `VERIFICATION_LIMIT_EXCEEDED` |
| 3 | Check revocation | `decision.decide` | `TOKEN_REVOKED`, `ANCESTOR_REVOKED` |
| 4 | Evaluate the token's caveats | `decision.decide` | `SCOPE_NOT_GRANTED`, `SCOPE_ATTENUATED_AWAY`, `TOOL_DENIED`, `ARG_PREDICATE_FAILED`, `INTENT_MISMATCH`, `BUDGET_EXHAUSTED_CAVEAT`, `DEPTH_EXCEEDED`, `APPROVAL_REQUIRED` |
| 5 | Evaluate the policy bundle | `decision.decide` | `POLICY_DENIED`, `POLICY_BUNDLE_STALE` |
| 6 | Check intent drift | `decision.decide` | `DRIFT_ESCALATION` |
| 7 | Reserve from the local lease | `decision.decide` | `BUDGET_EXHAUSTED_MANDATE`, `LEASE_UNAVAILABLE` |
| 8 | Forward upstream | PEP (T-018) | `UPSTREAM_ERROR` — **not a decision**, see §6 |
| 9 | Commit or refund the actual | PEP (T-021) | — |
| 10 | Emit the record | PEP (T-022) | — |

`RATE_LIMITED` has no step: the `RateLimit` caveat is the one of nine that was dropped
(`ROADMAP.md` Part 1). The code stays in the enum so the console's filter list is stable, and
§7 records that it is currently unreachable.

---

## 3. Precedence — the part that matters

**The first failing step wins, in the order above.** No step runs after one has produced a
`deny`.

This is a choice, and the alternative is worse. Evaluating everything and reporting the "most
severe" failure sounds more informative and is not: it means a revoked token can be reported as
`SCOPE_NOT_GRANTED`, and the operator spends their afternoon adjusting scopes on a credential
that was killed hours ago. Ordering by *how fundamental the failure is* means the named cause is
always the one that must be fixed first.

Three rules resolve the cases where one step yields several answers.

### 3.1 Deny beats escalate (INV-8)

Within step 4, a caveat that denies outranks a caveat that requires approval. A token carrying
both `ToolDeny{payment.send}` and `RequiresApproval{payment:initiate}` calling
`payment.send` is **denied**, not escalated — approval cannot grant authority the token does
not have, so raising it to a human asks them a question with only one correct answer.

### 3.2 Within one step, the first caveat in chain order wins

Caveats are evaluated root-first. Two failing caveats produce the outcome of the one nearer the
root, because that is the broader restriction and the one whose removal is a larger decision.

### 3.3 Drift never denies

Step 6 can only escalate or flag (`PLAN.md` §6.6, T-036). A drift score is a heuristic over
natural language, and a heuristic that can deny is a heuristic that can deny a legitimate
payment at three in the morning. Above the escalation threshold it escalates; below it, the
score is recorded and the request proceeds.

---

## 4. Naming the cause

`reason_code` is a closed enum (`PLAN.md` §6.9). `reason_detail` is human-readable prose.
`failing_caveat` is a `CaveatRef` and is populated **whenever the cause is a caveat** — that is,
for every code produced by step 4 except `DEPTH_EXCEEDED` when the depth came from the block
count rather than a `DepthLimit` caveat.

Where a caveat is not the cause, `failing_caveat` is `None` and `reason_detail` still names
something specific: which policy statement, which budget dimension, which revoked block id.

**A known limitation, stated rather than hidden.** Naming the failing caveat requires *having*
the caveats. A `VerifiedToken` exposes the authority block's grant, not the caveats that later
blocks added (ADR-005, and `STATUS.md` §3 gap 2 — there is no Datalog-to-caveat parser). So
`decide()` takes the caveat list as an **input**: the SDK knows the caveats it minted, and the
PEP will pass what it can recover. Where the caveat set is incomplete the pipeline still denies
correctly — biscuit's own authorizer enforces the chain regardless — but `failing_caveat` may be
`None` where a complete set would have named one. Closing that is T-045's parser, not this
ticket's.

---

## 5. Fail-closed

Any locally-held fact that is *unavailable* — not "says no", but cannot be consulted — denies
with `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`.

This includes a policy bundle older than its staleness limit (`POLICY_BUNDLE_STALE`, which is
its own code because the operator's fix is different). It does **not** include a drift oracle
that is unavailable: by §3.3 drift cannot deny, so an absent drift score records `None` and the
request proceeds. Failing closed on drift would let an outage of an advisory heuristic stop
every payment in the system.

---

## 6. `UPSTREAM_ERROR` is not a decision

Step 8 happens *after* the decision. When the upstream is unreachable the request was already
authorized; what failed is delivery. The PEP returns 502 or 504 with
`reason_code: UPSTREAM_ERROR` (T-018), and the decision record for that call still reads
`allow` — because it was allowed, and the audit trail must say what was decided rather than what
happened next.

Recording it as a deny would be a lie of exactly the kind §3.2 principle 4 exists to prevent.

---

## 7. Reason-code reachability

`PLAN.md` §6.9: *every deny path in the codebase maps to exactly one code. A CI test asserts
that every code is reachable and every deny in the source cites one.*

| Code | Reached by |
|---|---|
| `OK` | any allow |
| `MALFORMED_REQUEST` | step 1, step 2 |
| `TOKEN_TOO_LARGE`, `TOKEN_INVALID_SIGNATURE`, `TOKEN_NOT_YET_VALID`, `TOKEN_EXPIRED` | step 2 |
| `DEPTH_EXCEEDED` | step 2 (block count) or step 4 (`DepthLimit` caveat) |
| `TOKEN_REVOKED`, `ANCESTOR_REVOKED` | step 3 |
| `SCOPE_NOT_GRANTED`, `SCOPE_ATTENUATED_AWAY`, `TOOL_DENIED`, `ARG_PREDICATE_FAILED`, `INTENT_MISMATCH`, `BUDGET_EXHAUSTED_CAVEAT`, `APPROVAL_REQUIRED` | step 4 |
| `POLICY_DENIED`, `POLICY_BUNDLE_STALE` | step 5 |
| `DRIFT_ESCALATION` | step 6 |
| `BUDGET_EXHAUSTED_MANDATE`, `LEASE_UNAVAILABLE`, `LEASE_NOT_ACTIVE` | step 7, and the ledger (T-013, T-014) |
| `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` | §5 |
| `VERIFICATION_LIMIT_EXCEEDED` | step 2 — the Datalog engine exhausted its budget reading the token (TM-14 or TM-25). Fails closed; added by T-020 |
| `UPSTREAM_ERROR` | step 8, post-decision (§6) |
| `RATE_LIMITED` | **unreachable** — `RateLimit` was dropped (`ROADMAP.md` Part 1) |

---

## 8. What is never recorded

`arg_digest`, never the arguments (NFR-5, rule 10). The model enforces it: `DecisionRecord`
rejects anything but a 64-character SHA-256 hex digest, so a developer who passes the payload
gets a validation error rather than a PII leak into the audit ledger, the console, and the
evidence pack.

`reason_detail` is prose written by this pipeline and must name caveats, dimensions and policy
statements — never argument values. A deny reason reading *"amount 4,500,000 exceeds…"* has
copied the payload into the audit trail by another route.

---

## 9. Test mapping

| Test | Statement | Ticket |
|---|---|---|
| P-18 | Every deny carries a non-`OK` reason code; every allow carries `OK` | T-005 (model), T-019 |
| — | Every reason code in §7 is produced by some scenario, or listed as unreachable | T-019 |
| — | Precedence: for each ordered pair of failing steps, the earlier one is reported | T-019 |
| — | Deny beats escalate (§3.1) | T-019 |
| — | Drift never denies (§3.3) | T-019, T-036 |
| — | An unavailable dependency denies closed (§5) | T-019, T-051 |
| NFR-1 | p99 < 1 ms for the pure decision, warm caches, recorded | T-019 (`pytest-benchmark`) |

---

## 10. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Whether `reason_detail` should be a template id plus arguments, so the console can localise it into Bengali | T-046 |
| 2 | Whether `allow_with_flag` needs its own reason code, or `OK` plus a drift score is enough | T-036 |
| 3 | Whether step 4 should report *all* failing caveats rather than the first, for the console's benefit | T-045 |

---

## 11. A decision becomes an HTTP response

T-023 wires the pipeline into the gateway, so a `Decision` has to become a status code.
`PLAN.md` §11.1 fixes one case — EC-T01, a missing token, is 401 — and this section settles
the rest.

| Reason code | Status | Why that one |
|---|---|---|
| `OK` | *the upstream's* | An allow is a proxy hop; the client sees what the tool returned |
| `MALFORMED_REQUEST`, `TOKEN_INVALID_SIGNATURE`, `TOKEN_EXPIRED`, `TOKEN_NOT_YET_VALID`, `TOKEN_TOO_LARGE` | **401** | *Who are you?* — the credential is absent, unreadable, or out of date. Retrying with a fresh token is the fix |
| `TOKEN_REVOKED`, `ANCESTOR_REVOKED` | **401** | Also a credential problem, and deliberately indistinguishable from expiry at the status level — §11.1 |
| `SCOPE_NOT_GRANTED`, `SCOPE_ATTENUATED_AWAY`, `TOOL_DENIED`, `ARG_PREDICATE_FAILED`, `INTENT_MISMATCH`, `DEPTH_EXCEEDED`, `POLICY_DENIED` | **403** | *You are who you say, and you may not do this.* A fresh token of the same authority changes nothing |
| `BUDGET_EXHAUSTED_MANDATE`, `BUDGET_EXHAUSTED_CAVEAT`, `LEASE_UNAVAILABLE`, `RATE_LIMITED` | **429** | Exhaustion, not authority. The request may succeed later — after a top-up, a refund, or a new mandate — and 429 is the only standard code that says *not now* without saying *never* |
| `APPROVAL_REQUIRED`, `DRIFT_ESCALATION` | **403** | Not permitted **yet**. The escalation id travels in the body, because a client that cannot see it cannot follow up |
| `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`, `POLICY_BUNDLE_STALE`, `VERIFICATION_LIMIT_EXCEEDED` | **503** | *We could not decide.* Distinct from a denial: the fix is operational, and a client is right to retry |
| `UPSTREAM_ERROR` | **502 / 504** | Not a decision at all — §6 |

### 11.1 Why revocation is not its own status

A revoked token returning something distinctive tells a holder of a stolen token that the theft
was noticed. 401 for every credential failure keeps that quiet at the status level.

The `reason_code` in the body *does* distinguish them, and that is deliberate: the body is read
by the agent the token was issued to, which is entitled to know why it stopped working, while
the status line is what a passive observer sees. This is a small mitigation of TM-01's accepted
bearer-replay risk rather than a fix for it.

### 11.2 Why exhaustion is 429 and not 402

402 Payment Required is semantically apt and reserved by RFC 9110 for future use; clients,
proxies and load balancers do not treat it consistently. 429 has defined retry semantics and
existing client support, and the distinction that matters — *no budget* versus *no authority* —
is carried by the reason code, which every response has.

`Retry-After` is **not** set. The PEP genuinely does not know when budget will return: a top-up
depends on a sibling committing or a reservation being refunded, neither of which it can
predict. Guessing would be worse than silence.

### 11.3 The body

Every non-2xx response the PEP originates carries the same shape:

```json
{"reason_code": "SCOPE_NOT_GRANTED", "detail": "…", "decision_id": "…", "trace_id": "…"}
```

`decision_id` is what ties the refusal to the audit record, so *why was I denied?* is answerable
from the client's side without access to the ledger. `detail` names the failing caveat or policy
statement (§4) and **never** an argument value (§8).
