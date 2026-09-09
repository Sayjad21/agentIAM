# 21 — What is not built, and what is accepted

Everything the system does not do, in one place.

This file exists because a gap you have written down is a **known limitation**, and the same gap
undocumented is a lie by omission. A reviewer who finds one you did not mention stops believing
the rest of the documentation — and they are right to.

The authoritative lists are [`docs/STATUS.md`](../STATUS.md) §3 and [`docs/TODO.md`](../TODO.md).
This is the plain-language version.

---

## 1. The one architectural hole that matters

**There is no issuance service.**

Nothing in the repository publishes mandates or policy bundles at runtime. The bundle is signed
once at bootstrap and loaded once at boot. That single absence shows up as four separate
symptoms, and it is worth seeing them as one thing:

| Symptom | Consequence |
|---|---|
| Policy staleness and hot reload are unreachable | A bundle changes only by restarting the PEP |
| Agent roles are static configuration | Adding or re-roling an agent needs a file edit and a restart |
| `budgets.mandate_id` has no foreign key | A budget row can reference a mandate that was never issued |
| Rollback protection is real but never exercised | The serial check works; nothing publishes a second bundle to test it against |

If you were to build one more thing, build this. It closes four gaps at once.

---

## 2. Deployment shape

**One PEP serves exactly one mandate.** The lease pool binds a mandate at construction. Fine for
a demo, where one task runs at a time. A real multi-tenant deployment needs either one process
per mandate or a multi-mandate pool — and changing it touches the money hot path and the
settle-before-release ordering, so it is a ticket of its own rather than a tweak.

**A partitioned PEP cannot shut down gracefully.** Releasing a lease needs the ledger, and a
timeout does not help: the cancellation lands inside the database driver's greenlet bridge while
the socket is dead. Measured stuck for five minutes against a five-second bound. This is
availability, not correctness — the stranded lease is bounded by the TTL and reclaimed by the
reaper.

**No multi-region.** Single region, documented as future work from the start.

---

## 3. Features specified and deliberately deferred

Each of these has a written reason and a trigger for revisiting it.

| Deferred | Why | Reopens when |
|---|---|---|
| Token reference mode | **Measured unreachable.** At maximum depth a token is 4,892 of 8,192 allowed characters | Depth limit rises above ~16 |
| Adaptive lease sizing | Fixed leases behave identically for the demo; the algorithm is specified | Production traffic with variable rate |
| Drift dataset (2,000+ labelled pairs) | Weeks of irreducible human labelling | Research phase |
| Calibrated ML drift classifier | The rule-based version gives the same demo experience | After the dataset exists |
| Drift features f3, f4, f6 | Three features measured sufficient for the ship | Research phase |
| NL-compiler template fallback | The compiler path demonstrates without it | Preparing the failure drill for real |
| MCP transport | The generic HTTP enforcement point covers the demo | Adoption work |

Notice the first row. The deferral is backed by a **number**, so it can be revisited
automatically when the number changes. A deferral without a trigger is just a thing you forgot.

---

## 4. Risks accepted on purpose

These are not bugs. They are decisions, with the reasoning attached.

**Token theft (TM-01).** Tokens are bearer credentials — a stolen one works until it expires or
is revoked. Mitigated by short TTLs and fast revocation. Proof-of-possession binding is
documented future work. *Accepted.*

**Trajectory-level drift (TM-11).** Twenty small steps, each individually plausible,
cumulatively off-task. A per-action detector will likely miss it. *Accepted*, and stated rather
than papered over — a drift detector claimed to catch what it cannot is worse than none, because
someone will rely on it.

**Confused deputy (TM-05).** A low-privilege agent induces a higher-privileged one to act for
it. The higher agent's own caveats still bound the action and the audit chain records who really
acted, but the residual is real. *Partially mitigated.*

**Under-reported spend (TM-23).** An agent reports spending less than it did. Over-reporting is
clamped and cannot break the invariant; under-reporting is flagged wherever the PEP can
cross-check, and not otherwise prevented. *Accepted.*

**Clock skew beyond the configured margin (TM-22).** The two-sided margin is correct only while
real skew stays inside it. *Partially mitigated*, and the configuration refuses a TTL too small
for its margin.

**Timing side channel on deny reasons (TM-16).** Response shape is uniform; a small timing
difference between deny paths is **measured and reported** rather than claimed absent.

---

## 5. Quality debt

Honest housekeeping.

- **Coverage is reported but never gated.** No `fail_under` anywhere, and the page claims 100%
  for core while the tree measures 98%. It has already slipped once and was caught by hand. One
  configuration line fixes it.
- **Mutation testing never run.** Coverage says the lines executed; mutation says the assertions
  bite. Specified, not done.
- **A synchronous embedding client on the event loop.** A hung model blocks concurrent requests
  for the full timeout. Pinned by a chaos test that turns red when it is fixed.
- **A1 re-verification is manual.** The security rests on biscuit scoping block facts a
  particular way, and nothing fails automatically if a library upgrade changes it. It should be
  a test.
- **No HTTP trailer support** — measured unavailable across the whole stack, so one acceptance
  criterion is consciously unmet.
- **Duplicate test fixtures** in one integration module that predates the shared conftest.

---

## 6. Demo gaps

- **No beat shows the role discrimination.** The rule is enforced and corpus-tested; every agent
  that can reach the payment API is one the organization made senior, and the one non-senior
  agent that tries is refused earlier by its own scope. Showing it needs a fourth sub-agent,
  which changes the tree the docs describe.
- **The demo mandate expires 8 hours after seeding.** Working as designed, and the likeliest way
  to find a dead demo. Re-seed before presenting.
- **Re-seeding in place clutters the identity tree.** Each run mints new blocks and the audit
  chain is append-only, so old generations persist as extra nodes. Benign, but use a clean
  volume for a pristine tree.
- **One integration-job failure seen once and never reproduced.** Recorded as watch-only rather
  than explained away.

---

## 7. How to read this list

Notice what is **not** here. There is no entry saying "revocation might not work" or "the budget
invariant might not hold." Those are claims the project makes and tests, and they hold.

The gaps are all of three kinds:

1. **Unbuilt things** — the issuance service, MCP transport, the ML classifier.
2. **Deliberate scope decisions** — single region, no real money movement, no homemade crypto.
3. **Residual risks with the reasoning written down** — token theft, trajectory drift, clock
   skew.

That separation is the point. A reviewer can check each one and decide whether they agree with
the judgement — which is a very different conversation from discovering something you hid.

---

## The end of the tour

You now know what the system does, how each feature works, what broke along the way, how it is
tested, and where the edges are.

Good next steps:

- **Run it.** [`docs/RUNNING.md`](../RUNNING.md) — every surface with the output it should print.
- **Read one function.** `decide()` in `packages/agentiam-core/src/agentiam_core/decision.py` is
  the whole authorization model in one place.
- **Pick an open item.** [`docs/TODO.md`](../TODO.md) — each entry says what to do, where to
  look, and what "done" means.
