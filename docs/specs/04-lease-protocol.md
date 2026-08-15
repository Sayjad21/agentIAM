# Spec 04 — Budget Lease Protocol

**Status:** accepted · **Ticket:** T-004 · **Implements:** `PLAN.md` §6.4
**Consumed by:** T-012, T-013, T-014, T-016, T-017, T-021 · **Invariant:** INV-5

> This is the hardest component in the system and the strongest technical claim. Specify it
> fully before any code.
>
> The safety and liveness arguments in §7 and §8 were **model-checked** before being written
> down (§13). One gap in the pseudocode of `PLAN.md` §6.4 was found that way and is fixed here.

---

## 1. The problem

Enforce a global limit — `Σ spend ≤ mandate` — without a network round-trip on every call,
under concurrent sub-agents, PEP crashes, and network partition.

The two halves pull against each other. A central counter is trivially correct and puts a
network hop in the hot path, destroying NFR-1. A purely local counter is fast and cannot bound
a global sum. The lease protocol buys locality by pre-allocating: a PEP takes a bounded slice of
the budget, spends it locally at memory speed, and settles asynchronously.

**INV-5 is why this exists.** A parent may hand the same ৳50,000 ceiling to three children; each
token is individually valid and together they could spend ৳150,000. No static token check
prevents that (see [`03-attenuation.md`](03-attenuation.md) §5.2). The ledger is the only
authority on budget.

---

## 2. State

### 2.1 Ledger, per `(mandate_id, dimension)`

```
total        Decimal   the mandate ceiling                  NUMERIC(20,4)
committed    Decimal   irrevocably spent
leased       Decimal   held by PEPs, not yet committed
available    Decimal   = total - committed - leased         derived, never stored
version      int       optimistic concurrency
```

Database `CHECK`: `committed >= 0 AND leased >= 0 AND committed + leased <= total`.
The invariant is enforced by the schema, not only by application code — a bug that violates it
MUST fail the transaction rather than corrupt the pool.

### 2.2 Lease record

```
lease_id     UUID
budget_id    FK
pep_id       text
granted      Decimal   deducted from the pool at ACQUIRE; immutable
settled      Decimal   sum of committed amounts against this lease
outstanding  Decimal   = granted - settled                  derived
granted_at   timestamptz
expires_at   timestamptz   ledger-issued, absolute
state        active | released | expired | revoked
```

**Ledger invariant:** `leased = Σ over active leases (granted - settled)`.

> `PLAN.md` §6.4 uses `lease.amount` and `lease.remaining` for both the ledger's and the PEP's
> view. Splitting them into `granted`/`settled` (ledger) and `remaining_local` (PEP) removes a
> genuine ambiguity: `RELEASE` decrements by the *unsettled* portion while `LEDGER_COMMIT`
> decrements by the *committed* portion, and conflating the two is how `leased` drifts.

### 2.3 PEP, per held lease

```
remaining_local  Decimal   = granted - Σ(unsettled reservations)
```

Held in memory. Never authoritative. A PEP that loses it has lost only its own ability to spend,
never the ledger's accounting.

---

## 3. Lease state machine

```
                  ACQUIRE
                     │
                     ▼
                ┌─────────┐
       RELEASE  │         │  REAP (expires_at + S < now)
     ┌──────────┤ active  ├──────────┐
     │          │         │          │
     │          └────┬────┘          │
     ▼               │ REVOKE        ▼
┌──────────┐         ▼          ┌─────────┐
│ released │    ┌─────────┐     │ expired │
└──────────┘    │ revoked │     └─────────┘
                └─────────┘
```

All three exits are terminal and one-way. Each decrements `leased` by `outstanding` **exactly
once**; the state transition and the decrement happen in the same transaction, which is what
makes "exactly once" true rather than aspirational.

`LEDGER_COMMIT` is the only operation that mutates an active lease without leaving `active`.

---

## 4. Operations

Seven. Everything inside `BEGIN … COMMIT` runs in one serialized transaction on the budget row.

### 4.1 `ACQUIRE(mandate_id, dimension, requested, pep_id, ttl) → Lease | Insufficient`

```
BEGIN
  SELECT ... FROM budgets
    WHERE mandate_id = ? AND dimension = ?
    FOR UPDATE                          -- serializes concurrent acquires
  available := total - committed - leased
  grant := min(requested, available, max_fraction * available)
  IF grant <= 0: ROLLBACK; RETURN Insufficient
  leased := leased + grant
  INSERT lease(granted = grant, settled = 0, state = 'active',
               expires_at = now() + ttl)
COMMIT
RETURN lease
```

`FOR UPDATE` is not optional. Without it two concurrent acquires read the same `available` and
both succeed — measured overspend of 160 against a total of 100 (§13, CE-2.2).

A **top-up** is an `ACQUIRE` against the same `(mandate, dimension)`. There is no separate
operation.

### 4.2 `RESERVE(lease, amount) → Reservation | Insufficient`

PEP-side. **No network, no ledger mutation, no lock.** This is the hot path.

```
IF lease.state != 'active': RETURN Insufficient
IF now() >= lease.expires_at - S: RETURN Insufficient    -- expire early, see §9
IF lease.remaining_local < amount:
    trigger async TOP_UP; RETURN Insufficient
lease.remaining_local -= amount
RETURN Reservation(id = client_generated_uuid, amount = amount)
```

The reservation id is generated by the PEP and is the idempotency key (§10).

### 4.3 `COMMIT(reservation, actual)`

PEP-side settlement. `actual` may differ from the estimate in either direction.

```
delta := actual - reservation.amount
IF delta > 0:
    RESERVE(lease, delta) or escalate        -- must be covered before committing
IF delta < 0:
    lease.remaining_local += (-delta)        -- refund, local
enqueue LEDGER_COMMIT(lease_id, actual, reservation.id)   -- batched, async
```

The refund is local and immediate; the ledger learns about it via the reduced `actual`.

### 4.4 `LEDGER_COMMIT(lease_id, amount, reservation_id)`

Ledger-side. Idempotent by `reservation_id`.

```
BEGIN
  IF reservation_id already settled: ROLLBACK; RETURN          -- idempotency (§10)
  SELECT lease FOR UPDATE
  IF lease.state != 'active':                                  -- late commit (§11)
      record anomaly(lease_id, amount, lease.state)
      ROLLBACK; RETURN Rejected
  amount := min(amount, lease.outstanding)                     -- clamp; do not trust the PEP
  IF amount <= 0: ROLLBACK; RETURN
  SELECT budget FOR UPDATE
  committed := committed + amount
  leased    := leased    - amount
  lease.settled := lease.settled + amount
COMMIT
```

Three guards in one operation, and each protects something different — see §6.

### 4.5 `RELEASE(lease)`

Graceful shutdown or idle.

```
PRECONDITION: the PEP has drained all unsettled reservations on this lease
BEGIN
  SELECT lease FOR UPDATE
  IF lease.state != 'active': ROLLBACK; RETURN
  leased := leased - lease.outstanding
  lease.state := 'released'
COMMIT
```

### 4.6 `REAP()`

Background, every `ttl / 4`.

```
FOR EACH lease WHERE state = 'active' AND expires_at + S < now():
    BEGIN
      SELECT lease FOR UPDATE
      IF lease.state != 'active': ROLLBACK; CONTINUE
      leased := leased - lease.outstanding
      lease.state := 'expired'
    COMMIT
```

The `+ S` is the clock-skew margin and is load-bearing — §9.

### 4.7 `REVOKE(mandate_id)`

Immediate hard stop. Demo Beat 7.

```
BEGIN
  FOR EACH active lease on this mandate:
      leased := leased - lease.outstanding
      lease.state := 'revoked'
COMMIT
PUBLISH revocation to the Redis channel     -- PEPs drop their leases on receipt
```

Revocation of *authority* is `07-revocation.md`. This operation only returns the budget.

---

## 5. Guards

Five, and they are not interchangeable. §13 removes each one and reports what breaks.

| # | Guard | Protects | Removing it |
|---|---|---|---|
| G1 | `SELECT … FOR UPDATE` on the budget row | the pool invariant | **overspend** — 160 granted against a total of 100 |
| G2 | Clamp `amount` to `lease.outstanding` | the pool invariant | **`leased` goes negative** — 12/400 interleavings |
| G3 | Reject commits against a non-active lease | the pool invariant | **`leased` goes negative** — 55/400 interleavings |
| G4 | Idempotency by `reservation_id` | **accounting accuracy, not the pool** | see below |
| G5 | Drain reservations before `RELEASE` | avoids spurious anomalies | subsumed by G3 for safety |

### 5.1 G4 is not what it looks like

Idempotency does **not** protect the pool invariant. A replayed commit does
`committed += a; leased -= a`, which leaves `committed + leased` unchanged. Measured: 400
interleavings with replay enabled and idempotency off, zero violations.

What it protects is the **books**. Measured, one real spend of ৳30 delivered three times:

| | `committed` | `lease.settled` | `outstanding` |
|---|---|---|---|
| idempotent | 30 (correct) | 30 | 70 |
| not idempotent | **90** | **90** | **10** |

The pool stays safe and the accounting is wrong by 3×: the mandate looks exhausted early, the
PEP loses budget it never used, and the audit ledger records spend that did not happen. For a
system whose pitch is chain of custody, that is not a lesser failure — it is a different one.

Note also that **G2 is what stops G4's absence from becoming a safety bug**: once `outstanding`
reaches zero, an unclamped replay drives `leased` negative on the second delivery. Measured.

So P-12 ("duplicate commit is idempotent") must assert **accounting equality**, not the safety
invariant. A P-12 written against `committed + leased <= total` would pass while the books are
threefold wrong.

---

## 6. Safety argument

**Claim.** `committed + leased ≤ total` holds after every operation, therefore
`Σ spend ≤ total`.

Let `Φ = committed + leased`.

| Operation | Effect on Φ | Why bounded |
|---|---|---|
| `ACQUIRE` | `+grant` | `grant ≤ available = total − Φ`, computed under `FOR UPDATE` (G1), so `Φ′ ≤ total` |
| `RESERVE` | none | PEP-local; touches neither `committed` nor `leased` |
| `COMMIT` | none | PEP-local |
| `LEDGER_COMMIT` | `+a − a = 0` | Φ conserved. `a ≤ outstanding` (G2) keeps `leased ≥ 0`; `state = active` (G3) keeps the decrement matched to a live lease |
| `RELEASE` / `REAP` / `REVOKE` | `−outstanding` | Φ strictly decreases; one-way state transition makes it once-only |

Φ starts at 0, only `ACQUIRE` increases it, and `ACQUIRE` increases it by at most `total − Φ`.
Therefore `Φ ≤ total` always. Since `leased ≥ 0`, `committed ≤ Φ ≤ total`. ∎

The proof rests on three things being true simultaneously, which is exactly why G1–G3 are all
required: the `available` read must be serialized with its write, the decrement must be bounded
by what the lease actually holds, and the decrement must apply to a lease that has not already
been reclaimed.

**A PEP cannot break this.** `remaining_local` bounds what it may reserve, and `granted` was
already deducted from `available` at acquire time. Even a buggy or compromised PEP reporting an
inflated `actual` is clamped by G2 — safety does not depend on PEP correctness. This matters:
the PEP is the component most exposed to a compromised agent.

---

## 7. Liveness argument

**Claim.** Budget held by a crashed PEP returns to the pool within a bounded time.

Every lease carries a ledger-issued absolute `expires_at`. `REAP` runs every `ttl/4` and
reclaims any active lease past `expires_at + S`.

**Worst-case reclaim delay:** `ttl + S + ttl/4`. With the defaults in §12 (`ttl = 60 s`,
`S = 5 s`): **80 seconds**.

**Bound on degradation:** availability is reduced by at most `Σ outstanding` over crashed PEPs'
leases, for at most that window. Measured: a PEP holding 60 of a 100 pool crashes; available
drops to 40, and returns to 100 after the reap.

Progress is otherwise unconditional: `ACQUIRE` either grants or returns `Insufficient` without
blocking, and `RESERVE` never touches the network.

---

## 8. Partition behaviour

A partitioned PEP:

1. continues to spend its existing lease — bounded by `granted`, therefore safe;
2. cannot top up, because `ACQUIRE` needs the ledger;
3. **fails closed** at `expires_at − S`.

**This is CP, not AP, and that is the correct choice for money.** Say it in exactly those words
— a bank CTO will ask, and the answer they are listening for is that you chose deliberately
rather than inherited it.

The system remains available for pre-authorized spend within the lease, and refuses new
authority it cannot verify. Availability is traded for the guarantee that the mandate ceiling is
never exceeded.

---

## 9. Clock skew

This is the subtlest part of the protocol and the one place where a plausible-looking
implementation overspends.

If the reaper reclaims a lease at `expires_at` while a PEP whose clock lags still believes the
lease is live, the same budget exists in two places: reclaimed into the pool and re-issued to
another PEP, while the lagging PEP is still spending it.

**Measured.** With no skew margin, at `now = 62` against a lease expiring at 60: the lease is
reaped, the full amount is re-issued to another acquirer, and the lagging PEP would still have
spent it. With the margin, the lease stays active and nothing is re-issued.

### 9.1 Rule

Let `S` be the configured skew allowance.

- A PEP MUST stop using a lease at `expires_at − S` — **expire early**.
- The reaper MUST NOT reclaim before `expires_at + S` — **reclaim late**.

The `2S` gap is what guarantees the two views never overlap.

### 9.2 Constraint on configuration

`ttl > 2S` strictly, and `ttl ≥ 4S` in practice — otherwise the usable lease window
`ttl − S` shrinks toward zero and the PEP tops up constantly, reintroducing the network
round-trip the protocol exists to avoid.

Defaults satisfy this comfortably: `ttl = 60 s`, `S = 5 s` on NTP-synced hosts, usable window
55 s.

### 9.3 Honest statement

**Safety under crash and partition depends on actual clock skew staying within `S`.** If skew
exceeds `S`, overspend is possible within the excess window. This is a real assumption, it is
checkable (the ledger can compare a PEP's reported clock at acquire time and refuse a lease to a
badly-skewed PEP), and it must be stated rather than buried. EC-T08 fixes the tolerance test at
±30 s for the token-expiry path, where fail-closed makes a wide tolerance harmless; the lease
path needs the tighter, explicitly configured `S`.

---

## 10. Idempotency

Reservation ids are **client-generated UUIDs**, created by the PEP at `RESERVE`, and are the
primary key of the `reservations` table. A replayed `LEDGER_COMMIT` collides on that key and is
a no-op.

This matters because the commit path is deliberately asynchronous and batched — retries after a
timeout are normal operation, not an error path. See §5.1 for what breaks without it, and note
that the failure is in the books, not the pool.

---

## 11. Late commits and reconciliation

**This is the gap found by model-checking `PLAN.md` §6.4, and it is not a corner case.**

`RELEASE`, `REAP`, and `REVOKE` each decrement `leased` by the lease's full `outstanding`. If a
commit for that lease arrives afterwards — a buffered batch from a crashed PEP, or a partitioned
PEP reconnecting — and is applied normally, `leased` is decremented a second time for budget
that was already returned. Measured: `leased` goes negative in 55 of 400 random interleavings.

**Rule.** `LEDGER_COMMIT` against a lease that is not `active` MUST be rejected. It MUST NOT
modify `committed` or `leased`. It MUST be recorded as a reconciliation anomaly carrying the
lease id, the amount, and the lease's terminal state.

**The consequence, stated plainly:** a spend that really happened is not recorded in the budget.
The pool invariant is preserved by construction; the divergence is surfaced loudly instead of
silently corrupting the ledger. This is the correct trade — AgentIAM emits settlement
instructions rather than moving money (`PLAN.md` §1.4), so an anomaly is a reconciliation item,
not a lost payment.

The anomaly count MUST appear on the Grafana budget dashboard (T-047) and MUST be zero in a
clean chaos run. A non-zero count is a signal, not noise.

---

## 12. Lease sizing

**Current: fixed size.** Adaptive sizing is deferred (T-015, `PLAN.md` §21 item 15), and the
algorithm is specified here so the deferral is a scheduling choice rather than an unanswered
question.

```
lease = clamp(EWMA(spend_rate) × target_horizon, min_lease, max_fraction × available)
```

| Parameter | Default | Rationale |
|---|---|---|
| `target_horizon` | 30 s | Lease covers ~30 s of observed spend |
| `min_lease` | one typical call | Below this, every call is a network round-trip |
| `max_fraction` | 0.25 | No single PEP may strand more than a quarter of the pool |
| `ttl` | 60 s | With `S = 5 s`, a 55 s usable window |
| `S` (skew allowance) | 5 s | NTP-synced hosts; see §9.2 |
| reap interval | `ttl / 4` = 15 s | Bounds reclaim latency |

All MUST be configurable. `max_fraction` is the parameter that bounds the blast radius of a
crash: it is what makes the stranded-budget limitation quantifiable rather than open-ended.

---

## 13. Sibling budget semantics

Two modes, both implemented (T-017), resolving INV-5.

**Shared pool — the default.** Children reference the same pool row and draw from it. A pool
row is one with `parent_budget_id IS NULL`, one per `(mandate_id, dimension)`. Enforced
dynamically by leases and `SELECT ... FOR UPDATE`.

Measured: three children each requesting 100 against a total of 150 are granted amounts summing
to exactly 150 — the pool refuses rather than over-issuing. **The individual grants are not
`100 / 50 / 0` in any fixed order**; earlier drafts of this section and of §15 stated them that
way, which reads like a guarantee and is not one. Which caller gets the full 100 depends on
which transaction takes the row lock first. What is guaranteed, and what T-017's tests assert,
is `Σ granted = min(Σ requested, available)` and never more.

**Proportional split.** The parent divides the budget explicitly at mint time; each child gets
its own budget row. Enforced statically. Predictable, and wasteful when one child needs more
than its share.

An allocation row carries `parent_budget_id` and `agent_id` — both, or neither, enforced by
`ck_budgets_split_shape`. It shares its parent's `(mandate_id, dimension)`, so the uniqueness
guarantee on that pair is **partial**, scoped to pool rows.

The parent tracks what it has given away in `allocated`, and that column joins the pool
invariant:

    committed + leased + allocated <= total

Budget promised to a child is spoken for. Without the third term a parent could allocate its
whole pool and then lease the same money out again. `allocated` is a separate column rather than
an increment of `leased` because `leased` has a second meaning the invariant checker relies on —
it must equal the outstanding total of *active leases* — and overloading it would break that
check the moment a split happened.

`SPLIT` runs the whole division under the parent's row lock in one transaction: sum, compare
against `total - committed - leased - allocated`, create every child row, raise `allocated`. A
split that does not fit is refused before any row is created.

Shared pool is the default because it is what real workflows want. Both are offered because the
distinction — that classic capability systems do not address quantitative resources shared
across siblings — is the publishable observation.

---

## 14. Known limitations

State these voluntarily. Each has a bound.

1. **Stranded budget on PEP crash.** Real. Bounded by `max_fraction × available` per PEP, for at
   most `ttl + S + ttl/4` (80 s with defaults). Mitigations: short TTL, graceful-shutdown
   `RELEASE`, and heartbeat-based early reclaim. Do not pretend it does not exist.
2. **Safety depends on bounded clock skew** (§9.3). Beyond `S`, overspend is possible within the
   excess.
3. **Late commits are dropped from the budget** (§11). The pool stays correct; a real spend goes
   unrecorded and is flagged for reconciliation.
4. **Agent-reported `actual`.** Where the PEP cannot independently determine the true amount, it
   relies on the agent's report. Under-reporting to hide spend is red-team case A-19 and is an
   **accepted risk**, not a mitigated one. Over-reporting is bounded by G2.
5. **Single region.** Multi-region active-active is out of scope (`PLAN.md` §1.4).

---

## 15. Model-check results

A model of §4 was executed before this document was written. Reproduce by re-running the
protocol model against these scenarios; T-013 and T-016 turn them into real tests.

| Scenario | Result |
|---|---|
| Protocol as specified, 400 random interleavings | invariant holds |
| G1 removed (stale `available` read) | overspend: `leased` 160 vs `total` 100 |
| G2 removed (trust PEP's `actual`) | `leased` negative, 12/400 |
| G3 removed (accept late commits) | `leased` negative, 55/400 |
| G4 removed (replay) | pool safe; `committed` overstated 3× |
| Skew margin removed | budget re-issued while a lagging PEP still holds it |
| Three siblings, shared pool of 150, each requesting 100 | grants sum to exactly 150; the split between them is whichever order the row lock serializes |
| Crash with 60 of 100 outstanding, then reap | available 40 → 100 |

**The value of this table is the failures, not the pass.** A guard whose removal changes nothing
is not protecting anything, and two of the five turned out to protect something other than what
their name suggests (§5.1).

---

## 16. Test mapping

| Test | Statement | Ticket |
|---|---|---|
| P-10 | `Σ committed ≤ total` for any interleaving — stateful `hypothesis` machine | T-013 |
| P-11 | reserve → commit → refund conserves budget exactly (`Decimal`) | T-014 |
| P-12 | duplicate commit is idempotent — **assert accounting, not the pool** (§5.1) | T-014 |
| P-20 | lease expiry never over-releases (`leased ≥ 0` always) | T-013 |
| — | 50 concurrent acquires serialize correctly (G1) | T-013 |
| — | commit exceeding `outstanding` is clamped (G2) | T-014 |
| — | commit against a reaped lease is rejected and recorded (G3, §11) | T-014 |
| — | invariant checker detects a deliberately corrupted budget row | T-016 |
| — | three siblings, concurrent, three PEP instances, `Σ ≤ mandate` (INV-5) | T-017 |
| CH-3 | SIGKILL one PEP of three; its lease strands ≤ TTL then reclaims | T-052 |

P-10's `RuleBasedStateMachine` rules MUST include: acquire, reserve, commit, refund, release,
expire, crash, revoke — and **late commit**, which §11 shows is where the protocol actually
broke.

---

## 17. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Should the ledger refuse a lease to a PEP whose reported clock is skewed beyond `S` (§9.3) | T-013 |
| ~~2~~ | ~~Heartbeat-based early reclaim: worth it, or is TTL sufficient for the demo~~ — **resolved in T-021: TTL is sufficient, and a heartbeat would make things worse.** See below | done |
| ~~3~~ | ~~Batching window for `LEDGER_COMMIT` — latency against ledger write load~~ — **resolved in T-022: 64 records or 500 ms, whichever comes first.** See §17.2 | done |
| 4 | Should reconciliation anomalies block a mandate from being marked complete | T-014 |

### 17.1 Why not heartbeats (Q2, T-021)

A heartbeat would let the ledger reclaim a dead PEP's lease in seconds instead of the 80 s
§7 bounds. It is rejected, and not only on effort.

**It replaces a bounded failure with an unbounded one.** Today the reclaim rule is a function
of the lease's own `expires_at` — a fact issued once, by the ledger, that no later event can
move. A heartbeat makes reclaim a function of *message arrival*, so a PEP that is alive and
spending but whose heartbeat is delayed — GC pause, a saturated NIC, a slow control plane —
has its lease reclaimed underneath it. That is exactly the double-issue TM-22 describes,
reintroduced through a channel with no bound on its lateness. The clock-skew margin `S` bounds
clock disagreement; nothing bounds queueing delay.

So a heartbeat would need its own grace period, which is a TTL by another name, and the
question becomes why there are two.

**And the bound it improves is already stated and small.** 80 s of at most `max_fraction ×
available` (a quarter of the pool by default), only when a PEP dies without `RELEASE`.
Graceful shutdown already covers every planned exit; this is the unplanned-death path only.

**Resumption trigger:** a deployment where PEP crashes are frequent enough that 80 s of
reduced availability is felt, *and* the heartbeat channel has a bounded delivery latency to
size the grace period against. Neither is true of the demo or of a single-region deployment.

### 17.2 The batching window (Q3, T-022)

**64 records or 500 ms, whichever comes first.** Both configurable.

The window is bounded from both ends, and the upper bound is the interesting one.

**Upper bound — a batch must land before its lease can be reaped.** A commit arriving after
the lease has been reclaimed is a *late commit* (§11): the ledger rejects it, records a
reconciliation anomaly, and a real spend goes unrecorded in the budget. The reap cutoff is
`expires_at + S`, so the batching window must be small against `ttl` — 500 ms against a 60 s
TTL is a margin of 120×, which leaves the failure entirely dominated by process death rather
than by the batching choice.

**Lower bound — below about 10 ms the batching buys nothing.** The wakeups start costing more
than the writes they combine, and a ledger round trip is already off the hot path, so the
latency saved is latency nobody is waiting on.

**What the window actually costs.** A commit delayed by up to 500 ms leaves `leased` high for
that much longer, so a concurrent sibling sees marginally less `available` and may take a
smaller grant. That is a fairness effect, not a safety one — `Σ committed ≤ total` holds
throughout, because the budget is already deducted at `ACQUIRE`.

**What it buys.** Up to 64 commits become one transaction on the budget row, and that row is
the serialization point for every `ACQUIRE` in the mandate (§4.1's `FOR UPDATE`). Reducing
write transactions against it is the difference between the ledger being a bottleneck under
concurrent siblings and not.

The same window governs the decision-record emitter (T-022), for the same reasons and with a
looser constraint — an audit record has no reap deadline. Sharing the numbers means one thing
to tune rather than two.
