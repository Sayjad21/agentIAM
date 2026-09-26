# AgentIAM — Research Foundation

Audit written 2026-09-26/27 against commit `5c2c6ee`. Six documents. **Read them in this
order** — each depends on the one before it.

---

## Reading order

### 1. [`gap-analysis.md`](gap-analysis.md) — start here
**Read first, even though it is logically last.** It is the shortest path to knowing what you
actually have. Every externally-facing claim marked ✅ implemented / ⚠️ partial / ❌
aspirational, with `file:line` for everything marked implemented.

**If you read only three rows:** C-8 (the "no proprietary black-box API" claim is contradicted
by shipped code), C-14 (the README says "Milestone 1 — in progress"), and §6 (three threat-model
citations point at chaos scenarios that never ran).

Ends with a fix list ordered by value per hour, and §8 — what is genuinely strong, which is not
short.

### 2. [`related-work.md`](related-work.md) — read second, act on it first
Where AgentIAM sits in the literature, and what can still be claimed. This is the document with
the most decision-relevant news in it.

**Read §4 in full.** It is five collisions and opportunities, all verified live this session:
- §4.1 — AIP uses the *same library, same technique, same transport*. Attenuation is gone.
- §4.2 — the budget gap narrowed; two works predate the August survey and were missed by it.
  But I read Agent Contracts' full text: it is **single-process**, so your distributed claim
  survives.
- §4.3 — HDP signs every delegation hop. Your audit chain does not.
- **§4.4 — the benchmark blocker is solvable.** Two public in-domain harnesses now exist, and
  the payment one explicitly leaves spend caps untested. **This is the highest-value item in
  the whole audit.**
- §4.5 — the rest of the competitive set.

Then §5.1 (the revised contribution claim) and §6 (recommendations).

### 3. [`threat-model.md`](threat-model.md) — read before writing any security claim
The formal complement to `docs/threat-model.md`, which already exists and is good STRIDE work.
This one is organized by *property* rather than by threat: attacker lattice A0–A6, trust
boundaries B1–B5, ten properties P1–P10 each mapped line-by-line to enforcing code.

**The two sections that matter most:** §6, ten properties claimed somewhere in project
materials and **not enforced**; and §8, what a reviewer will attack first, ordered by damage.

**The one claim to internalize, stated exactly:** P4 (budget confinement) holds against a PEP
that follows the protocol and **lies about amounts** — guards G2/G3, proved in spec 04 §6. That
is the realistic threat and nobody else claims it. It does **not** yet hold against arbitrary
code execution in a PEP, because the deployed PEP holds database credentials and `total` is
mutable. A restricted database role closes that (`lease-plan.md` GAP-A). Under-sold in the first
half, over-claimed in the second — get both halves right.

### 4. [`lease-plan.md`](lease-plan.md) — read when you start building
The plan for the lease contribution: the claim, what backs it, **eight gaps ordered by whether
the paper fails without them**, a five-phase sequence, and four decisions that need you.

**Two gaps are new and not in `docs/STATUS.md`:** GAP-A (the PEP holds database credentials, so
"safety does not depend on the enforcement point" is a protocol property, not a deployed one)
and GAP-C (nothing verifies a PEP's clock, and spec 04's own open question 1 is still open).

### 5. [`decisions.md`](decisions.md) — the four decisions, with their costs
`RD-1` TLA+ model (accepted) · `RD-2` restricted Postgres role now, ledger service deferred ·
`RD-3` top-tier venue, no deadline · `RD-4` team allocation deferred.

**The two consequences worth knowing:** RD-1 **reorders Phase 1** — the model goes before the
skew fix, because it decides the bound rather than assuming the one derived by hand here. RD-3
**promotes baselines and off-box performance from desirable to mandatory**.

### 6. [`prior-survey-2026-08-21.md`](prior-survey-2026-08-21.md) — reference, not required
The previous literature survey, recovered verbatim from git stash `45a6a67` where it was the
only copy. 1,129 lines, genuinely good work. Its central conclusion is **superseded** by
`related-work.md` §4.2 — read that first, then use this for its §3 search methodology, §7
evaluation plan, and §13 bibliography.

---

## What this audit changed

| Before | After |
|---|---|
| "No formal threat model exists" | One does — `docs/threat-model.md`, 27 STRIDE entries. This audit adds the property-oriented complement instead of duplicating it |
| "The quantitative/budget half is the genuine gap" | Narrowed. Agent Contracts has conservation laws; Token Budgets has no-double-spend. **The distributed half survives** |
| "No baselines, no shared benchmark — six weeks of dataset work" | Two public harnesses exist. APort Vault is released and leaves spend caps open |
| "Chain of custody is a contribution" | HDP signs every hop. Sign the chain or cite HDP |
| Prior survey was stash-only | Preserved here verbatim; stash untouched |

## Caveats on this audit

- I did **not** run the test suite. "Implemented" means the enforcing code is at the cited
  location and a named test covers it — not that I watched it pass.
- Citations are marked **[✓ verified]** (retrieved this session) or **[VERIFY]** (carried from
  the prior survey or background knowledge, not re-retrieved). Check every [VERIFY] before
  submission.
- Nothing outside `research/` was modified. The `research/` directory is committed on its own;
  the unrelated working-tree changes present during the audit were left untouched.
