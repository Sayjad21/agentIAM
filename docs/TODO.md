# TODO

Work queued out of the manual end-to-end pass ([`manual-test-report.md`](manual-test-report.md)),
ordered by what it costs to leave undone. Items that already have a home in
[`STATUS.md`](STATUS.md) §3 say so rather than restating them — where this list disagrees
with a gap's recorded impact, that disagreement is itself an item.

Each entry says what to do, where to look, and what "done" means. Anything marked
**Investigate** is a question to answer before writing code, because the fix depends on the
answer.

---

## P0 — the core claim does not currently hold in the deployed system

### 1. Wire caveat evaluation into the deployed PEP

**The single most important item on this list.** `Pipeline.__init__` defaults `caveats_for`
to `lambda _token: ()`, and neither [`scripts/pep_service.py`](../scripts/pep_service.py)
nor [`scripts/serve_pep.py`](../scripts/serve_pep.py) passes one. So `decide(...,
caveats=())` runs on every request and **no token caveat of any kind is enforced**.

Measured: `agt-doc-reader`, holding `ScopeSubset{invoice:read, vendor:read}` and
`BudgetCeiling(spend_bdt, 0)`, successfully initiated a payment (`200`,
`status: accepted`). Two sibling agents also read resources their scope subset excluded.

- **Where:** `packages/agentiam-pep/src/agentiam_pep/pipeline.py:193,268`; the composition
  roots in `scripts/`.
- **Blocked on:** item 2 — there is no way to recover caveats from a received token yet.
- **Interim option worth costing:** for a chain *this* process minted, the SDK already
  carries the caveats it created (STATUS gap 2). Investigate whether the PEP can be handed
  those for the demo path while the parser is built, and be explicit that it does not
  generalise to a received token.
- **Done when:** the three C-cases in the test report return `403 SCOPE_NOT_GRANTED` /
  `BUDGET_EXCEEDED`, and a regression test asserts a deployed-shaped PEP refuses an
  out-of-scope call.

### 2. Build the Datalog→caveat parser (STATUS gap 2)

Already tracked, but its recorded impact is **understated** and should be corrected first —
see item 3. Everything below depends on it: enforcement (item 1), real agent identity
(item 4), naming the failing caveat in a decision record (T-019), and the identity tree
(T-045).

- **Note carried from STATUS:** whatever parses block source **must not trust it** — TM-24.
- **Investigate:** whether biscuit's own authorizer can carry the scope/amount facts at
  request time so the token's `check if` statements evaluate natively, instead of parsing
  block source back into `Caveat` objects. That would be a smaller and safer change than a
  parser, if it works. Verify against the installed `biscuit-python`, not the docs.
- **Done when:** a `VerifiedToken` obtained from an untrusted third party yields its true
  effective bound, and the console can display it.

### 3. Correct STATUS gap 2's impact statement

Gap 2 currently reads as a *reporting* limitation — "the console cannot show a true
effective bound". The measured behaviour is that the deployed PEP **enforces nothing**,
which is a security property, not a display one. The distinction matters because gap 2 is
what a reader consults to decide whether attenuation works.

- **Done when:** gap 2's "impact if left" names non-enforcement in `pep_service.py`, and
  `README.md`'s attenuation claim is scoped to `agentiam-core` until item 1 lands.

---

## P1 — visible in the demo

### 4. Give agents real identities instead of `agt-depth-{N}`

`pep_service.py:382` derives `agent_id=f"agt-depth-{token.depth}"` and `role` from a single
configured default. Every sibling therefore shares one id and every `role` reads
`"unknown"`. `DEMO.md` beat 2 ("root spawns 3 sub-agents, each node's scopes visibly
smaller") cannot be demonstrated.

- **Blocked on:** item 2.
- **Done when:** `/v1/tree/{task}` returns one distinct node per real agent, with the role
  the parent assigned at `attenuate()` time.

### 5. Make the identity tree fail loudly, and render regardless

Independent of item 4 and cheap. With duplicate ids, `d3.stratify()` throws
`ambiguous: agt-depth-1`; the template catches it, `return`s, and leaves a blank canvas
while the status indicator still reads "Connected". The page looks like it is working.

- **Where:** `console/templates/identity_tree.html`, the `try/catch` around `stratify`.
- **Do:** key the hierarchy on something already unique in the payload — the terminal
  `block_id` is per-agent and present — and surface a failure in the UI instead of only
  `console.error`.
- **Done when:** the current five-node payload draws five nodes, and an unstratifiable
  payload shows a visible error rather than an empty page.

### 6. Make lease size configurable, and decide what an over-lease request should do

`DEFAULT_LEASE_SIZE` is `5000.0000` and `ServiceSettings.from_env()` reads **no override**
for it, unlike every other setting. A request larger than the lease is refused with
`LEASE_UNAVAILABLE` and retrying never helps (verified: 3 attempts, 3 s apart, all `429`),
because a top-up refills to the lease size rather than to cover the request. A deployed PEP
therefore cannot authorize a single payment over 5,000 BDT no matter the mandate —
`DEMO.md` beat 4's 50,000 ceiling is unreachable.

- **Do now:** read `AGENTIAM_PEP_LEASE_SIZE` in `from_env()`.
- **Investigate:** whether a single request exceeding the lease should trigger a top-up
  sized to the request. That interacts with spec 04 §4.1's blast-radius reasoning and
  STATUS gap 8, so it is a design decision, not a patch. T-015 (adaptive lease sizing) is
  where it most likely belongs.
- **Done when:** the lease size is configurable, and the over-lease case is either handled
  or documented as a deliberate bound with a reason.

### 7. Build T-057's demo seed script

There is no committed way to get the system into a state where any of the console pages
show data. Every page renders correctly and is empty on a fresh install, which is what made
the two P0 defects invisible until this pass. The scenario used for the test report lived
in a scratchpad and is not reproducible from the repo.

- **Do:** one command that creates a mandate and budget, mints a root token, attenuates a
  realistic delegation tree, and drives enough traffic to populate decisions, budgets,
  audit, and the tree.
- **Done when:** `make demo-seed` (or equivalent) leaves every console page showing real
  data, and `DEMO.md`'s beats can be walked without hand-built tokens.

---

## P2 — correctness and operations debt

### 8. Test that the deployed PEP primes its lease pool

The priming bug (fixed in `5c1f845`) was invisible to the suite:
`tests/unit/test_pep_service.py` passed identically before and after — 22 either way. The
fix is currently as untested as the bug was.

- **Done when:** a test asserts a service built by `build_service()` holds a lease for
  `spend_bdt` after startup, and fails if the `prime()` call is removed.

### 9. Schedule `reap()` (STATUS gap 27)

Already tracked and unchanged by this pass — recorded here only because item 6's top-up
behaviour and gap 27's stranded-lease reclamation are the same subsystem, and whoever picks
up one should look at the other.

### 10. Audit authentication and routing refusals

An unauthenticated request returns a `decision_id`, but
`/v1/audit/search?decision_id=…` returns `{"results":[],"total":0}` and the chain total does
not move. Refusals before token verification take `_refuse` rather than
`_record_and_refuse`, so they never reach the ledger.

Two consequences: credential probing leaves no audit trail, and the `decision_id` handed to
a caller cannot be looked up by the operator they would quote it to.

- **Investigate first:** whether this is deliberate. Recording pre-verification refusals
  makes the chain writable by an unauthenticated caller, which is a denial-of-service and
  a chain-growth concern — that may be exactly why it is built this way. If so, the fix is
  to **stop returning a `decision_id`** for unrecorded refusals rather than to record them.
  Check the threat model and spec 08 before changing either side.
- **Done when:** a refusal either appears in the chain or does not claim an id that implies
  it does.

### 11. Fix the status codes

`401` is returned for an unmapped route (a routing decision, not an authentication one) and
for `TOKEN_REVOKED` / `ANCESTOR_REVOKED` (the token authenticated fine; it is no longer
authorized). `403` fits both. Low effort; misleads any client that retries on `401`.

- **Check:** whether `tests/` or the SDK pin the current codes before changing them.

---

## P3 — verification gaps in this pass

### 12. Verify the narrowing-only approval invariant against a live request

The manual pass could **not** confirm "approve narrows, never widens" (EC-A09) end to end:
the auth gate returns `401` before the widening check runs, so the check was never reached.
It is covered by `tests/integration/test_escalations_api.py`, but the property `DEMO.md`
leans on hardest has never been demonstrated against a running system.

- **Needs:** the Keycloak login path wired far enough to obtain a session cookie for an
  approver on the allowlist.
- **Done when:** a live `POST /v1/escalations/{id}/approve` asking for more than was
  requested is refused, with the refusal shown in the console.

### 13. Exercise the console pages that this pass could only check over HTTP

The decisions and identity-tree pages hold open `EventSource` connections, so headless
screenshot capture never terminates and they were verified via their APIs rather than
rendered. Budgets, overview, policy, audit and escalations were confirmed visually.

- **Done when:** the two SSE pages are confirmed rendering live rows, ideally with a
  browser-driven check that can run unattended.

---

## Suggested order

1, 2 and 3 travel together and unblock 4. 5, 6 and 7 are independently useful and can start
immediately — **7 first** if the goal is to make the system demonstrable again, since it is
what turns every other item on this list into something you can see rather than read about.
