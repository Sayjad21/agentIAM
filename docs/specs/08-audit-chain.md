# Spec 08 — The Audit Chain and Chain of Custody

**Status:** accepted · **Ticket:** M4 (audit chain, unnumbered in `PLAN.md` §9)
**Implements:** `PLAN.md` §6.9, NFR-6, TM-12
**Depends on:** [`09-decision-record.md`](09-decision-record.md)
**Consumed by:** T-023, T-048, T-055, T-051

> *The credit limit and chain of custody for AI agents.* The second half of that sentence is
> this document. A decision record that can be edited after the fact answers nothing.
>
> The part worth checking is §4: appending to a hash chain is a read-modify-write, and two
> concurrent appends that both read the same head produce a chain that still verifies while
> having lost a record.

---

## 1. What the chain is for

TM-12: an attacker — or a careless operator, or a bug — alters, deletes, or reorders decision
records to make an unauthorized action look authorized.

The defence is not preventing writes. It is making any alteration **detectable**, so that a
disputed transaction has an answer. `PLAN.md` NFR-6 states it as: tampering with any record is
detected, and the report names the first inconsistent sequence number.

This is not a blockchain and does not pretend to be. It is a hash chain in one Postgres table,
which is the appropriate amount of machinery for a single-region system where the threat is
tampering rather than Byzantine consensus.

---

## 2. The link

`hashing.chain_hash()` already implements it (T-005):

```
record_hash(n) = sha256(canonical_json({"prev": record_hash(n-1), "record": body(n)}))
```

The previous hash is bound **inside** the hashed structure, not concatenated alongside it.
That is what makes the chain tamper-evident rather than merely tamper-flagged: altering record
*n* changes `record_hash(n)`, which changes the input to `record_hash(n+1)`, and so on to the
head. A verifier walking the chain finds the first break.

The genesis record's `prev` is `null`, which canonicalizes distinctly from the string `"null"`
— `canonical_json` is total over the types involved (T-005), so there is no ambiguity to
exploit.

---

## 3. What is stored

```
seq          bigserial PRIMARY KEY   monotonic, gapless, assigned under the lock (§4)
decision_id  uuid UNIQUE             the DecisionRecord's own id — idempotency key
record       jsonb                   the canonical DecisionRecord body
prev_hash    char(64) NULL           the preceding record's hash; NULL only for seq 1
record_hash  char(64) NOT NULL       this record's link
created_at   timestamptz             when the ledger accepted it, not when the PEP decided
```

`record` carries `arg_digest` and never arguments — the `DecisionRecord` validator enforces
that at construction (NFR-5, TM-13), so the audit table cannot become a PII store by accident.

`created_at` is deliberately the ledger's clock and distinct from the record's own `timestamp`,
which is the PEP's. They differ by the emitter's batching window (spec 04 §17.2, up to 500 ms),
and conflating them would make a batched write look like a delayed decision.

`decision_id UNIQUE` makes append idempotent: a retried batch (T-022 retries failed writes)
must not append the same decision twice, and a duplicate is a no-op rather than a second link.

---

## 4. Appending is serialized, and that is load-bearing

Appending reads the current head hash, computes a new hash from it, and writes. Two concurrent
appends that both read head *h* will both write records claiming `prev_hash = h`.

**The result is a chain that still verifies and has silently lost a record.** Verification walks
`seq` order recomputing hashes; whichever record is second at that `seq` has a `prev_hash` that
does not match its predecessor's `record_hash`, so it *is* caught — but only if the verifier
walks strictly. The failure mode where it is not caught is worse: if the loser's write is
rolled back by a `UNIQUE` violation on `seq`, the decision is simply absent, and an absent
record breaks nothing at all. Nothing in a hash chain detects a record that was never written.

**So appends serialize on a single head row**, the same shape spec 04 §4.1 uses for the budget
pool:

```
BEGIN
  SELECT last_seq, last_hash FROM audit_chain_head WHERE id = 1 FOR UPDATE
  FOR EACH record in batch:
      hash := chain_hash(last_hash, record)
      INSERT audit_records(seq = last_seq + 1, prev_hash = last_hash, record_hash = hash, …)
      last_hash := hash; last_seq := last_seq + 1
  UPDATE audit_chain_head SET last_seq, last_hash
COMMIT
```

A single-row table rather than `SELECT max(seq) FOR UPDATE`, because there is no row to lock
when the table is empty — the first two concurrent appends would both find nothing and both
insert `seq = 1`.

Batching amortises the lock: T-022 delivers up to 64 records per write, so the serialization
point is taken once per batch rather than once per decision.

**This guard MUST be shown to fire.** T-013 set the precedent — remove the lock, run concurrent
appends against real Postgres, and observe the chain break — and the same standard applies here.

---

## 5. Verification

`verify_chain()` walks records in `seq` order and recomputes each link. It reports:

| Result | Meaning |
|---|---|
| `ok=True, checked=n` | Every link recomputes; the chain is intact from genesis to head |
| `ok=False, first_bad_seq=k` | Record *k*'s stored hash does not match its recomputation |

**It names the first inconsistent seq**, not merely "invalid" — NFR-6 requires it, and an
operator holding a broken chain needs to know where to look. Everything after *k* is
untrustworthy by construction, so reporting further breaks would be noise.

Two failure shapes it detects:

* **Altered record** — `record_hash(k)` no longer matches `chain_hash(prev_hash(k), record(k))`.
* **Broken linkage** — `prev_hash(k)` does not equal `record_hash(k-1)`, which is what a deleted
  or reordered record looks like.

One it does not: **truncation at the head**. Deleting the most recent records leaves a chain
that verifies perfectly. This is inherent to a hash chain with no external anchor, and it is
stated here rather than discovered by a judge. The mitigation is `audit_chain_head`, which
records `last_seq` independently — a head row disagreeing with `max(seq)` is evidence of
truncation, and §7 lists checking it as the verifier's job.

---

## 6. Chain of custody

`custody(task_id)` returns every decision recorded for one task, in `seq` order, with its
delegation depth and agent. That is the query behind the demo's *who authorized this payment?*
and behind T-048's audit explorer.

It is a read of the same table; the chain is what makes the answer trustworthy, not a separate
structure.

---

## 7. Known limitations

| # | Limitation | Bound |
|---|---|---|
| 1 | **Head truncation is undetectable from the chain alone** | §5. `audit_chain_head.last_seq` is the independent witness; the verifier compares it with `max(seq)` and reports a mismatch |
| 2 | Whoever can write the table can also rewrite `audit_chain_head` | A database superuser can forge a consistent chain. Defence is operational — restricted credentials, WAL archiving — not cryptographic. External anchoring (publishing the head hash somewhere the operator does not control) is future work |
| 3 | No signature on records | The chain proves *integrity*, not *origin*. A record's `pep_id` is asserted, not authenticated. Signing per-PEP is future work and would want the same key rotation story as spec 01 §8 |
| 4 | Verification is O(n) | Fine at demo and pilot scale. A Merkle tree would give O(log n) inclusion proofs; unnecessary until someone needs to prove one record without shipping the whole chain |

---

## 8. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Whether the head hash should be published externally at intervals, and where | T-055 (evidence pack) |
| 2 | Whether `custody()` should page, and on what key | T-048 |
