# Spec 07 — Revocation

Implements `PLAN.md` §6.7, §7 (data model), §8 (API), §11.5 (edge cases EC-R01…EC-R10).
Threat model: mitigates TM-08 (revocation lag). Feeds T-038 (this ticket), T-039 (PEP cache
— Bloom filter in front of the exact set this spec defines), T-040 (subtree e2e, NFR-4).

---

## 1. The problem

A mandate or one of its delegates must be able to stop working **before its TTL expires**,
and every PEP enforcing it must observe that within seconds — not by waiting for the token to
time out. `decide()` (spec 09 step 3) already calls a synchronous `RevocationOracle
.is_revoked(revocation_id) -> bool` on the hot path (`agentiam_core/decision.py`); this spec
is about what keeps that oracle's answer both fresh and free of network I/O at the point it's
asked.

Two properties are non-negotiable, in this order:

1. **No resurrection (INV-10).** A revoked chain must never again authorize anything. A false
   *negative* here — reporting `not revoked` for something that is — is a security failure.
2. **No false denial.** A live, unrevoked chain must never be refused for a revocation that
   doesn't apply to it. A false *positive* is a correctness bug, not a security hole, and the
   asymmetry matters: T-039's Bloom filter is allowed to produce false positives (it falls
   through to the exact set), but this spec's exact set itself must not.

## 2. Granularity and mechanism

`PLAN.md` §8 names three granularities: **token**, **subtree**, **mandate**. Mechanically
there is only one operation underneath all three: **revoke one biscuit block id.**

**Verified** (probed against a live root → child → grandchild chain, 2026-08-17): a child
token's `revocation_ids` tuple begins with its parent's, unchanged, and each attenuation
appends exactly one id. `verify()` already exposes this as `VerifiedToken.revocation_ids`
(`tokens.py`), and `decide()` already walks every id in it (`decision.py` step 3, quoted
below) — this spec adds nothing to that mechanism, only a source of *which* ids are revoked.

```python
for position, revocation_id in enumerate(token.revocation_ids):
    if revocation.is_revoked(revocation_id):
        is_self = position == len(token.revocation_ids) - 1
        return _deny(
            ReasonCode.TOKEN_REVOKED if is_self else ReasonCode.ANCESTOR_REVOKED,
            f"block {revocation_id} is revoked",
        )
```

So the three granularities are the same call with a different *target id*, and the `scope`
field on a revocation record (§3.1) is a caller-declared classification for audit and console
display — not a different code path:

| Granularity | What is revoked | Effect |
|---|---|---|
| `token` | The leaf block id of one specific token | Only that exact token fails (`TOKEN_REVOKED`, position = last) |
| `subtree` | Any non-leaf block id in a chain | That token and everything ever attenuated from it fails (`ANCESTOR_REVOKED` for descendants, `TOKEN_REVOKED` for itself) |
| `mandate` | The root mandate's own (authority-block) id | The entire delegation tree for that mandate fails |

A caller revoking "subtree" or "mandate" is not asking the service to compute anything extra
— they are simply naming which block id they mean, the same as a `token` revocation. There is
no distinct `REVOKE_SUBTREE` operation.

### 2.1 Where the block id comes from

No `agents` or `mandates` SQL table exists yet (`STATUS.md` gap 7 — `T-005` built `Mandate`
as a pure Pydantic model, no persistence). A revoke call therefore cannot resolve "the mandate
belonging to task X" to a block id by querying such a table, because there is nothing to
query. It doesn't need to: `DecisionRecord.token_chain_ids` (`pipeline.py`'s `_record()`
already sets this from `token.revocation_ids`) is written to `audit_records` on every
decision, so **every block id that has ever made a request is already recoverable** from the
audit trail (`GET /audit/custody?action_id=...`, or a direct query). A human operator or the
console resolves "revoke this agent" to a block id by looking up its most recent decision
record — the revoke endpoint itself only ever takes a block id.

If T-045 (identity tree) or a later issuance service adds real `agents`/`mandates` tables,
resolving a friendlier identifier to a block id becomes that table's job, layered in front of
this API without changing it.

## 3. State

### 3.1 Control-plane record — `revocations` table

```sql
revocations(
  id           UUID PRIMARY KEY,
  seq          BIGSERIAL UNIQUE NOT NULL,  -- monotonic pull cursor, distinct from id
  block_id     TEXT UNIQUE NOT NULL,        -- the biscuit revocation id (128 hex chars)
  scope        TEXT NOT NULL,               -- 'token' | 'subtree' | 'mandate' (§2, descriptive only)
  reason       TEXT NOT NULL,
  revoked_by   TEXT NOT NULL,
  revoked_at   TIMESTAMPTZ NOT NULL,
  expires_at   TIMESTAMPTZ NOT NULL         -- the *original token's* expiry (§8), not this record's
)
```

`block_id UNIQUE` is the idempotency mechanism (§9): a second revoke of the same block id
finds the row already there. `seq` is separate from `id` for the same reason
`audit_records.seq` is separate from `audit_records.decision_id` (spec 08 §3): `id` is
whatever a client-generated or server-generated identity needs to be, `seq` is purely an
ordering cursor for `GET /v1/revocations?since=seq`, and the two must not be conflated or a
future need for one (e.g. a client-generated `id` for idempotent retries) constrains the
other.

### 3.2 PEP-local view — the exact set

One `set[str]` of currently-revoked block ids, held in the PEP process, mutated only by the
consumer described in §5 and read only by the synchronous `is_revoked()` call `decide()`
makes. This is `InMemoryRevocationSet`'s existing shape (`agentiam_pep/revocation.py`) —
this spec's job is to keep that set's contents correct, not to change the shape. **T-039**
puts a Bloom filter in front of this set for O(1) negative answers at scale (PLAN's EC-R10,
10,000 revocations); that is a performance layer over what this spec builds, not a
replacement for it.

## 4. Operations

### 4.1 `REVOKE(block_id, scope, reason, revoked_by, original_expires_at) → RevocationRecord`

1. Insert into `revocations`, `block_id UNIQUE` making a repeat call a no-op that returns the
   existing row rather than erroring (EC-R05 — idempotent).
2. **Persist before publish, always.** The insert must commit to Postgres regardless of
   whether Redis is reachable (EC-R07). Publishing is best-effort *after* the row exists —
   never the other way around, or a crash between "published" and "persisted" would leave a
   revocation that some PEPs saw and forgot (nothing pulls it later) while others never saw
   it at all.
3. Publish `{block_id, seq}` to the `agentiam:revocations` Redis channel. A publish failure is
   logged and counted, **not raised** — the row is already durable, and §5.2's pull path is
   what makes correctness independent of this step succeeding.
4. Return the persisted record either way.

Revoking a **nonexistent** block id (EC-R04) and revoking an **already-expired** token's block
id (EC-R09) both succeed as ordinary inserts — the service has no way to know a block id was
never minted, and does not need to: an unrevoked id being added to the revoked set is harmless
by construction (§6), and pruning (§8) removes it once it can no longer matter either way.

### 4.2 `PULL(since_seq) → {entries: [RevocationRecord], next_seq}`

`SELECT * FROM revocations WHERE seq > since_seq ORDER BY seq`. `next_seq` is the highest
`seq` returned, or `since_seq` unchanged if `entries` is empty — a caller that pulls
repeatedly with the last `next_seq` it received converges on the full set incrementally,
never needs to re-read rows it already has, and (§8) never needs pruned rows again once its
own last pull already passed their `seq`.

### 4.3 The PEP consumer

Owns the in-memory set (§3.2). Two inputs, one output:

- **On push** (§5.1): a message `{block_id, seq}` arrives on the subscribed channel → add
  `block_id` to the set, and remember `seq` as a watermark (used only to detect gaps, never
  to gate correctness — see §5.3).
- **On a pull tick** (§5.2, every `pull_interval`): call `PULL(last_pulled_seq)`, add every
  `entries[].block_id` to the set, set `last_pulled_seq = next_seq`.
- **Output:** `is_revoked(id) -> bool` is a plain set membership check. No I/O, matching the
  `RevocationOracle` protocol `decide()` is already written against.

Both inputs only ever **add** to the set. Nothing in this system un-revokes an id (§9)
short of the whole record being pruned (§8), at which point the id has already outlived its
own token's validity and adding or removing it from the live set changes nothing observable.

## 5. Distribution

### 5.1 Push — Redis pub/sub, fast path

Channel `agentiam:revocations` (`PLAN.md` §8). The control plane publishes on every
successful `REVOKE` (§4.1 step 3). This is the low-latency path that makes NFR-4 (< 2 s p99
across 3 PEPs, measured in T-040) achievable — pub/sub delivery is milliseconds, not the
`pull_interval`.

### 5.2 Pull — HTTP backstop, correctness path

Every PEP, on a fixed interval (`pull_interval`, default a few seconds — short enough that
"one pull interval" in T-038's own acceptance criterion is itself inside NFR-4's 2 s budget
is a reasonable target, though the two numbers are allowed to diverge and should each be
measured on their own rather than assumed equal), calls `GET /v1/revocations?since=seq`
against the control plane and merges the result (§4.3). This path does not depend on Redis at
all — it is what makes EC-R06 (a PEP misses the pub/sub message) and EC-R07 (Redis is down)
converge to the same correct state as a PEP that saw every push.

**Push is an optimization; pull is the source of truth.** A deployment could delete the Redis
channel entirely and still be correct, only slower (bounded by `pull_interval` instead of
pub/sub latency). The reverse is not true: push alone, with no pull, cannot recover from a
missed message, a reconnect gap, or a PEP that started after some revocations already
happened. This asymmetry is why §4.1 persists before it publishes rather than the reverse.

### 5.3 Staleness → fail closed

If a PEP's last successful pull is older than `staleness_limit` (a small multiple of
`pull_interval` — proposed 3×, so a single missed tick from ordinary jitter doesn't trip it,
but a genuinely stuck consumer does), `is_revoked()` raises `OracleUnavailable` instead of
answering from a set it no longer trusts. `decide()` already turns that into
`CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` (rule 6: fail closed on ambiguous state) — this spec
adds no new denial path, it only has to make the existing one reachable for the right reason.

Note the asymmetry with §5.2: pull failing to *keep up* is a staleness problem (§5.3); pull
being *unavailable outright* (the control plane itself down) is the same
`OracleUnavailable` → fail-closed outcome, reached the same way, once `staleness_limit` is
exceeded either way. No special-case handling is needed for "control plane down" versus
"control plane slow" — both are just "no successful pull recently enough."

## 6. Safety argument

**Claim:** no chain that should be denied is ever allowed (INV-10 holds), and the mechanism
that makes this true does not depend on Redis.

1. A REVOKE that succeeds is durable in Postgres before it is published (§4.1).
2. Every PEP pulls the full backlog on an interval bounded by `pull_interval`, independent of
   whether it saw any push (§5.2) — so every PEP's exact set converges to a superset of every
   revocation whose `revoked_at` is at least `pull_interval` in the past, regardless of Redis.
3. `decide()` checks **every** id in the chain, not only the leaf (§2, already true of the
   shipped code) — so revoking any ancestor id is sufficient, the descendant enumeration
   `PLAN.md` calls out as unnecessary ("for free") is exactly this: nothing has to look up
   *which* descendants exist.
4. The only window where a truly-revoked chain can still be used is between `revoked_at` and
   whichever PEP instance's next successful pull (or sooner, via push) — bounded above by
   `pull_interval` (or, degraded, `staleness_limit` before that PEP stops answering at all and
   fails closed instead). This window is the honest cost of an async distribution model and is
   exactly what NFR-4 measures.

**What this does not claim:** a PEP that is fully partitioned from the control plane (no pull
succeeds *and* it has not yet crossed `staleness_limit`) is answering from a set that is stale
by construction — safe in the false-denial direction only if nothing new was revoked during
the partition, which cannot be guaranteed. This is why `staleness_limit` exists and must be
tuned tighter than "how long can this be wrong" tolerance actually allows, not looser.

## 7. Failure and partition behaviour

| Failure | PEP behaviour |
|---|---|
| Redis unreachable at publish time | Row already persisted (§4.1); publish failure logged, not raised; every PEP still converges via pull within `pull_interval` |
| Redis unreachable at subscribe time | PEP runs on pull alone; correctness unaffected (§5.2), latency degrades from milliseconds to `pull_interval` |
| One push message dropped (channel hiccup, not a full outage) | Next pull tick picks it up; no special detection needed because pull re-reads by `seq`, not by "did I see this message" |
| Control plane unreachable | Pulls fail; once `staleness_limit` is crossed, `is_revoked()` raises and `decide()` fails closed for every request that reaches step 3 |
| PEP process restarts | Starts with an empty set and `since_seq = 0`; first pull tick populates the full backlog before serving (a cold-start ordering choice: **do not serve any request until the first pull completes**, or a freshly-started PEP would briefly behave like a PEP with a stale-but-not-yet-detected view) |

## 8. Pruning

A `revocations` row is safe to delete once its `expires_at` (the *original token's* expiry,
not the revocation's own timestamp) has passed: past that point the token it names could not
have authorized anything even if it had never been revoked at all, so the row carries no
further information any PEP's decision could depend on. Pruning is out of scope for T-038's
acceptance criteria (which only requires the field exist and be correct) — a periodic job
deleting `WHERE expires_at < now()` is a small, independent addition whenever storage size
becomes a real constraint, and needs no schema change to add later.

## 9. Idempotency

`block_id UNIQUE` (§3.1) is the entire mechanism (EC-R05): a second `REVOKE` for an
already-revoked block id is a no-op that returns the existing record rather than erroring or
creating a duplicate. Nothing un-revokes an id — there is no `UNREVOKE` operation, matching
the audit posture of every other write path in this system (tokens are immutable, rule 11;
a revocation, once true, stays true for the life of the token it names).

## 10. Known limitations

- **No PoP binding.** A revoked bearer token's holder can still present it; revocation is
  what makes that presentation start failing, not what prevents the presentation itself.
  Already an accepted risk (TM-01) independent of this spec.
- **The partition window (§6) is real, not merely theoretical**, and is the honest cost
  named throughout this document rather than glossed over.
- **`scope` is advisory**, not enforced (§2) — nothing stops an operator from labelling a
  leaf-token revocation as `mandate` in the API call. It exists for audit readability, and a
  future console (T-045/T-050) should compute it from the block id's position in a known
  chain rather than trust the caller's label, once chain structure is queryable at all
  (§2.1's `agents` table, if it lands).

## 11. Test mapping

| ID | Case | Covered by |
|---|---|---|
| EC-R01 | Revoke a leaf token | `TOKEN_REVOKED`, position = last (already true of `decide()`) |
| EC-R02 | Revoke a mid-tree token | `ANCESTOR_REVOKED` for descendants; siblings sharing no common ancestor id are unaffected — T-040 |
| EC-R03 | Revoke the root | Every descendant fails — T-040 |
| EC-R04 | Revoke a nonexistent id | §4.1 — ordinary insert, 200 |
| EC-R05 | Revoke twice | §9 — `block_id UNIQUE`, idempotent |
| EC-R06 | PEP misses the pub/sub message | §5.2/§7 — pull converges regardless; test by dropping the channel against a real Redis, not a mock |
| EC-R07 | Redis down during revocation | §4.1 step 2 — persist-then-publish; test with Redis unreachable at revoke time |
| EC-R08 | Bloom filter false positive | T-039, not this spec — this spec's exact set has none by construction (§1) |
| EC-R09 | Revocation of an already-expired token | §4.1 — accepted, no-op in effect (the token was already unusable) |
| EC-R10 | 10,000 revocations | T-039 sizes the Bloom filter; this spec's obligation is that the exact set and `PULL` stay correct at that size, not that they stay fast — T-039 is where fast is measured |

## 12. Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Exact `pull_interval` and `staleness_limit` values — proposed a few seconds and 3× respectively (§5.2, §5.3), not yet measured against NFR-4 | T-038 implementation |
| 2 | Whether the revoke endpoint should require the caller to prove the block id is real (e.g. cross-check against `audit_records`) rather than accept any string — currently permissive per EC-R04's own "no-op on nonexistent id" requirement | T-038, revisit if abuse becomes a concern |
| 3 | Whether pruning (§8) becomes its own scheduled job or stays a manual script, and when | Post-T-038, whenever storage size is measured |
