# 09 — Budgets and leases

*Feature: quantitative ceilings that actually hold, under concurrency and under partition.*

**This is the hardest component in the system and the strongest technical claim.** It is also
where the worst bug in the project's history lived. Take your time with this one.

---

## 1. The problem

A mandate says "৳500,000." Something has to make that true.

The naive answer is to ask a central database on every call. That works and it is wrong here,
for two reasons: it puts a network round trip on a path measured in microseconds, and it makes
the database an availability dependency for every agent action.

The other naive answer is to write the ceiling into the token and trust it. That fails the
sibling problem from [file 07](07-attenuation.md): three children each holding a valid
৳200,000 ceiling can spend ৳600,000 between them, and every token stays valid.

So we need something that is **local when spending** and **globally correct in total**.

---

## 2. The answer: leases

Borrowed from distributed systems. The ledger grants a PEP a **lease** — a slice of the budget
it may spend on its own for a limited time.

```
ledger:  total ৳500,000 · committed ৳8,600 · leased ৳5,000
                                                  │
                         ┌────────────────────────┘
                         ▼
PEP:  holds ৳5,000 for 60 seconds. Spends from it locally, at microsecond speed.
      Reports back what it actually used.
```

The PEP spends without asking. The **ledger remains the only authority** on what is left.

### The seven operations

| Operation | What it does |
|---|---|
| `ACQUIRE` | Ledger grants a lease to a PEP |
| `RESERVE` | PEP holds an amount locally while a call is in flight |
| `COMMIT` | The call finished; settle at the real amount |
| `LEDGER_COMMIT` | Tell the ledger what was actually spent |
| `RELEASE` | Give an unused lease back |
| `REAP` | Reclaim leases whose time ran out |
| `REVOKE` | Kill every lease for a mandate |

`RESERVE` and `COMMIT` are local and fast. `ACQUIRE`, `LEDGER_COMMIT`, `RELEASE` and `REAP`
touch the ledger and are deliberately kept off the request path.

---

## 3. Why it is correct

### Concurrency

Two PEPs asking for a lease at the same moment are serialized by `SELECT … FOR UPDATE` on the
budget row. Measured for the sibling case: three children each requesting 100 against a pool of
150 received **100, 50 and 0**. The third is refused, correctly, rather than the pool going
negative.

### Partition

If the ledger becomes unreachable, the PEP keeps working **inside the lease it already holds**,
then refuses. It does not guess and it does not fail open.

This makes the system **CP rather than AP** — consistency over availability — which is the
correct choice when the resource is money. A bank CTO will ask this exact question.

### Clock skew

The dangerous window is a lease being reclaimed centrally while a lagging PEP still believes it
holds it. Then the same budget is issued twice.

The defence is a margin `S` on both sides: the PEP expires **early** at `expires_at − S`, the
reaper reclaims **late** at `expires_at + S`, and the configuration refuses any `ttl ≤ 2S`. The
safety of this depends on real skew staying inside `S`, which is why it is recorded as
*partially* mitigated (TM-22) rather than solved.

---

## 4. What went wrong

Four separate failures, and the first is the most serious defect the project has had.

### The money never reached the ledger

**The symptom.** None visible. Everything looked healthy for two milestones.

**The root cause.** `Pipeline.settle()` computed the settlement, and then discarded the result
its own docstring said must be sent to the ledger. Grepping the whole tree found **no
production caller** of `ledger_commit` at all. So `budgets.committed` never moved.

And because `RELEASE` returns `granted − settled`, a PEP shutting down handed back the *entire*
lease — including everything it had spent.

**How bad.** The chaos suite measured it: 992 requests spent ৳4,960, and the ledger recorded
`committed = 0`. One instance that spent 300 of a 500 lease returned all 500 on shutdown.

**The headline claim of the whole project — budget cannot be overspent — was false across any
PEP restart or lease top-up.**

**Why nothing caught it.** This is the part worth internalising. The invariant checker was
running, and it *passed*. It asserted `committed == Σ settled reservations`, and that held
perfectly — as `0 == 0`. The books were entirely consistent about a number that had stopped
describing reality.

**The fix.** A settlement queue that survives outages, wired into both reference assemblies. A
second route to the same bug — a top-up releasing a lease while settlements were still queued,
which declined 6,678 of 6,992 settlements — was closed by a `before_release` hook that drains
settlements first. Recorded as ADR-049.

**The lesson.** An invariant that compares two numbers you compute the same way is not a check.
It is a tautology wearing a check's clothes. Ask what the invariant would look like if the
feature were entirely absent — if the answer is "it would still pass", it is not testing the
feature.

### A late commit could drive the pool negative

**The root cause.** A commit arriving after its lease was released or reaped decrements
`leased` a second time, for budget already returned. Measured with random interleavings:
`leased` went **negative in 55 of 400** runs.

**The fix.** Commits against a non-active lease are **rejected and recorded as reconciliation
anomalies** rather than applied (ADR-009). The pool invariant is preserved and the divergence is
surfaced instead of silently absorbed. Note the choice: not "apply it anyway", not "drop it
quietly" — reject *and* record, so someone can see it happened.

### A lease that expired unspent was never replaced

**The symptom.** The PEP stopped authorizing every payment about a minute after boot. Reads
still worked, so the service looked alive.

**The root cause.** A lease leaves service two ways — it **drains**, or it **ages out**. The
top-up logic only asked about the first:

```python
if held.lease.remaining_local > held.lease.granted * low_water:
    return          # nothing to do
```

A lease that expires **unspent** answers that "no" — `remaining_local` is still the whole grant
— so no replacement was ever scheduled, and every later request was refused permanently.

**Why nothing caught it.** Every automated path spends promptly, so every automated path took
the *drained* branch. It needed someone to bring the stack up, read the console for two
minutes, and then try to pay.

**The fix.** The top-up test now reuses `check()`'s own "no longer usable" predicate rather
than writing a second one — two conditions that must agree, expressed once. Had that been true
from the start the expiry branch could not have been missed, because it is the condition the
refusal was already computing (ADR-065).

### …and even then, something had to be refused to trigger the fix

**The root cause.** The repair above schedules a replacement, but scheduling is asynchronous
and the refusal has already been decided. So the request that *notices* the dead lease is
still refused; the next one succeeds.

Invisible on a busy PEP. On an idle one it is the whole experience: a demo stack sitting for
more than a minute answers the first payment with `LEASE_UNAVAILABLE`.

**The fix.** The pool now renews on a **timer**, replacing any lease past half its TTL — half,
not "once stale", because the margin must be wider than the skew or the replacement lands after
refusals have already started (ADR-070). Nothing has to be refused to trigger the renewal that
would have prevented it.

---

## 5. Design choice: replacement, not addition

When a lease runs low, the PEP does not acquire a *second* lease alongside it. It acquires a
replacement and releases the old one.

Two leases for one dimension would mean two expiries and two releases to keep straight, for no
gain — the old lease's unspent remainder comes back with the release anyway (ADR-025). One
in-flight top-up at a time, and a flag prevents the request path and the renewal timer racing
into two concurrent acquires.

## 6. Design choice: the clamp that is not applied

Spec 04 §4.1 describes a `max_fraction` clamp bounding how much of a pool one PEP may hold. It
is **not implemented**, and that is a recorded, measured decision (ADR-015): applied to a fixed
caller-requested amount, the formula is mathematically incompatible with the ticket's own
acceptance test.

The consequence is stated as a known gap rather than hidden: there is no single-PEP blast-radius
bound beyond the TTL. It properly belongs to adaptive lease sizing, which is deferred.

---

## 7. Where to look

| Thing | File |
|---|---|
| The lease pool | `packages/agentiam-pep/src/agentiam_pep/pool.py` |
| Local lease arithmetic | `packages/agentiam-pep/src/agentiam_pep/lease.py` |
| Settlement queue | `packages/agentiam-pep/src/agentiam_pep/settlement.py` |
| Ledger operations | `packages/agentiam-controlplane/src/agentiam_controlplane/db/` |
| The protocol, model-checked | [`docs/specs/04-lease-protocol.md`](../specs/04-lease-protocol.md) |
| The live check | `scripts/run_invariant_checker.py --once` |

Spec 04's safety and liveness arguments were **model-checked before being written down**, and
doing so found a gap in the original plan's pseudocode.

---

## Next

[10 — The policy layer](10-policy-cedar.md).
