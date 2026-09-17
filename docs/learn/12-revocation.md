# 12 — Revocation

*Feature: switching a token off before it expires — and everything below it.*

---

## 1. Why it is needed

Tokens are short-lived (15 minutes by default) precisely so that theft has a bounded blast
radius. But "wait for it to expire" is not an incident response. When an agent misbehaves, or a
token leaks, somebody needs a switch.

And it has to reach **every** enforcement point quickly. A revoked token that keeps working on
one PEP for a minute is a revoked token that does not work. The target is **under 2 seconds at
p99 across three PEPs** — NFR-4.

---

## 2. How it works

Revocation names a **block id** — one specific layer of one specific token chain. Three scopes:

| Scope | Kills |
|---|---|
| `token` | That block, and therefore every chain containing it |
| `subtree` | A branch of the delegation tree |
| `mandate` | Everything issued under one grant |

Because a biscuit chain contains every ancestor block, revoking a parent automatically kills
every descendant. A child cannot outlive a revoked parent — that is invariant **INV-10**, no
resurrection. The reason code says which: `TOKEN_REVOKED` if it was your own block,
`ANCESTOR_REVOKED` if it was someone above you.

### Two delivery paths, and only one of them is the correctness path

```
control plane ──push──▶ Redis pub/sub ──▶ PEPs      fast
              ──pull──▶ GET /v1/revocations?since=N   correct
```

The spec is explicit that **pull is the correctness path and push is the latency
optimisation**. Its own words: *a deployment could delete the Redis channel entirely and still
be correct, only slower. The reverse is not true.*

That framing shows up in the configuration — the control-plane URL is a **required** setting
for a PEP, while Redis is what makes it fast.

### Persist before publish, always

The database insert must commit **before** anything is published. A crash between "published"
and "persisted" would leave PEPs believing in a revocation the ledger has no record of — and no
way to recover it on the next pull.

### Cold start refuses to serve

A restarted PEP begins with an empty set. It **does not serve any request until its first pull
completes**. Otherwise a freshly started PEP would behave like a PEP with a stale view, which is
the exact condition revocation exists to prevent.

---

## 3. Design choice: a Bloom filter, chosen by measurement

The revocation set is checked on every request, so the plan called for a counting Bloom filter
in front of the exact set — O(1) negative answers at scale.

The obvious Python library, `pyprobables`, was **measured against the plain `set` it was meant
to accelerate**, and came out **900 to 1,200 times slower**. The optimisation would have made
the hot path dramatically worse.

`fastbloom-rs` was chosen instead — by measurement, not by name recognition (ADR-044).

**The lesson is general.** "This data structure is asymptotically better" is a claim about large
inputs. At the sizes this system actually sees, a Python `set` is extremely hard to beat, and
the only way to know is to run both.

---

## 4. Design choice: `expires_at` is not what it looks like

A revocation record carries `expires_at`, and it is **the original token's expiry** — not a
lease on the revocation.

It exists so the row can be **pruned**: once the token it names could not have authorized
anything anyway, the row carries no information any decision could depend on.

**A revocation therefore keeps applying past that timestamp.** This looks like a bug and is not.
Honouring it as a deadline would silently **un-revoke** a token somebody revoked on purpose,
which is the worst possible failure mode for this feature.

It was checked deliberately during a live sweep, against the spec, before being written down as
correct.

---

## 5. Verified end to end

On a running stack, revoking the root authority block:

```
before:  doc-reader reads          200
         payer pays                200

revoke → 201

after:   doc-reader reads          401 ANCESTOR_REVOKED
         payer pays                401 ANCESTOR_REVOKED
         negotiator reads vendor   401 ANCESTOR_REVOKED
```

The whole subtree, from one record. Recovery is re-seeding, which mints fresh blocks that no
revocation names.

Two practical notes that cost time to learn:

- **Only an authorized revoker may revoke, and the caller has to prove who it is.** Present
  either a console session or `Authorization: Bearer <operator-token>`; the acting identity is
  derived from that credential and `revoked_by` is not a request field at all. No credential is
  `401`, a credential belonging to someone outside the approver set is
  `403 '<id>' is not an authorized revoker`. The approver set and the operator tokens are both
  configuration.

  This route shipped taking `revoked_by` in the body and checking that string against the
  approver list — ADR-041's pre-OIDC stopgap, which ADR-046 retired for escalation approve/deny
  and did not come back for this one. An unauthenticated `POST` naming a real approver was
  measured returning `201` on the demo stack, so anyone who could reach the control plane could
  revoke the root block and cascade every agent to `ANCESTOR_REVOKED`. Same principle as file
  14's: a body field naming yourself is a claim, a credential is evidence.
- **Revocation targets a block id, so it applies to one token generation.** Re-seed a demo and
  the old generation's block ids still exist in the audit history; revoking one of those appears
  to do nothing. Take the block id from the most recently seen node.

---

## 6. Where to look

| Thing | File |
|---|---|
| The PEP's local set, push and pull | `packages/agentiam-pep/src/agentiam_pep/revocation.py` |
| Issuing and serving revocations | `packages/agentiam-controlplane/src/agentiam_controlplane/revocations_api.py` |
| The normative rules | [`docs/specs/07-revocation.md`](../specs/07-revocation.md) |

---

## Next

[13 — The audit chain](13-audit-chain.md).
