# 13 — The audit chain

*Feature: a record of every decision that nobody can quietly edit.*

The project's tagline has two halves. "The credit limit" is [file 09](09-budgets-and-leases.md).
**"The chain of custody" is this file.**

---

## 1. What it is for

The threat is not someone stopping you writing records. It is someone **altering, deleting or
reordering** them afterwards, so an unauthorized action looks authorized. That is TM-12.

The defence is not prevention. It is **detection**: any alteration must be findable, so a
disputed transaction has an answer.

> This is a hash chain in one Postgres table. It is **not a blockchain** and does not pretend to
> be — that is the appropriate amount of machinery for a single-region system where the threat
> is tampering, not Byzantine consensus. The out-of-scope list says "no blockchain of any kind"
> for a reason.

---

## 2. How the link works

```
record_hash(n) = sha256(canonical_json({ "prev": record_hash(n-1), "record": body(n) }))
```

Read that carefully: the previous hash is bound **inside** the hashed structure, not
concatenated next to it. Alter any record and its hash changes, which changes the next record's
input, which changes every hash after it. One edit invalidates the whole tail.

The `canonical_json` part matters as much as the SHA-256. Two different serializations of the
same record must produce the same bytes, or a record re-encoded in transit would appear
tampered. The same canonicalization signs policy bundles and computes intent hashes — one
definition, used everywhere.

---

## 3. The hard part: two writers at once

Appending to a hash chain is a **read-modify-write**: read the current head, compute a new hash
from it, write. Two concurrent appends that both read the same head produce a chain that
**still verifies** while having lost a record.

That is the failure worth understanding — it does not look like corruption. It looks fine.
Verification passes. A record is simply gone.

The fix is serialization at the database: the head row is locked, so appends are ordered and
each one sees the previous. It costs concurrency on the audit write path, which is why the
emitter buffers and batches rather than writing synchronously per request.

---

## 4. What is in a record

```json
{
  "seq": 91,
  "decision_id": "6f59d622-…",
  "task_id": "d0d0d0d0-…",
  "principal_id": "kc:1111…",
  "agent_id": "agt-settlement",
  "role": "payer",
  "depth": 2,
  "scope": "payment:initiate",
  "tool_id": "payment_api",
  "outcome": "deny",
  "reason_code": "BUDGET_EXHAUSTED_CAVEAT",
  "failing_caveat_kind": "BudgetCeiling",
  "failing_caveat_block": 2,
  "arg_digest": "71981f26…",
  "policy_version": "demo-1",
  "budget_before": { "spend_bdt": "2850.0000", … },
  "budget_after":  { "spend_bdt": "2850.0000", … },
  "drift_score": null,
  "latency_us": 1385
}
```

Two details worth calling out.

**`arg_digest`, never the arguments.** Decision records carry a hash of the arguments, not the
values. A validator rejects anything that is not 64 hex characters, so a record carrying real
argument text cannot be constructed. That closes TM-13 — customer data leaking into an audit
store that is, by design, hard to delete from. Deny reasons cite caveats, never values.

**The budget before and after.** This is what makes a disputed payment answerable: not just
"allowed", but the exact ledger state on both sides of it.

---

## 5. A full audit buffer denies the request

If the audit sink cannot keep up, the request is **refused**.

This follows from an ordering decision in the pipeline: the record is written at step 10,
**before** the request is forwarded at step 8. A policy that denies after the tool has already
acted is not a policy, and an action nobody can prove happened is exactly what the chain exists
to prevent.

There is no "log-and-continue" mode offered (ADR-026). Offering one would make the chain of
custody conditional on load, which is when you most want it.

---

## 6. What went wrong

### A transient outage was treated as a poison message

**The root cause.** The emitter gave each batch a fixed number of retries and then **discarded**
it. That path was written for a single record the sink will never accept — but at the emitter's
layer, *a stopped database is indistinguishable from that*.

**The measurement.** Through a 30-second outage, records were dropped roughly every
`(max_retries + 1) × flush_interval` seconds. The queue never filled, so back-pressure never
engaged, so the PEP kept authorizing requests it could no longer record.

**That is the exact outcome the design exists to prevent, produced by the mechanism meant to
serve it.**

**The fix.** Transient failures now retry **without limit**, and capacity bounds them by filling
up and denying — which is the intended back-pressure. Only a `SinkRejectedRecord`, raised by the
sink itself, drops a batch, because **the sink is the only layer that can classify a failure as
permanent**. `max_retries` was removed rather than left as dead configuration (ADR-051).

The chaos test now asserts that *every* record survives the outage.

**The lesson.** "Retry N times then give up" is a reasonable-looking default that quietly
converts an availability problem into a data-loss problem. The question to ask is: *at this
layer, can I actually tell the difference between "this will never work" and "this is not
working right now"?* If not, you may not decide to give up.

---

## 7. Chain of custody, the query

The feature this all exists for:

```bash
curl -s http://localhost:8000/v1/audit/custody/<task_id>
```

Walks a task back to the human who approved it, through every agent that acted, naming the rule
that permitted each step. An unknown task returns **404**, not an empty list — an operator must
be able to tell *did nothing* from *does not exist*.

And the chain itself can be verified on demand:

```bash
python scripts/verify_audit_chain.py
chain intact: 113 record(s) verified
```

It re-hashes every record and checks each against its predecessor. Tamper with a row and it
names the first bad sequence number.

---

## 8. Where to look

| Thing | File |
|---|---|
| The emitter, buffering and retry | `packages/agentiam-pep/src/agentiam_pep/emitter.py` |
| Chain hashing | `packages/agentiam-core/src/agentiam_core/hashing.py` |
| Storage and the append lock | `packages/agentiam-controlplane/src/agentiam_controlplane/db/` |
| Search, custody, verification | `packages/agentiam-controlplane/src/agentiam_controlplane/audit_api.py` |
| The normative rules | [`docs/specs/08-audit-chain.md`](../specs/08-audit-chain.md) |

---

## Next

[14 — Escalation](14-escalation.md).
