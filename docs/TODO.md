# TODO

Work queued out of the manual end-to-end pass ([`manual-test-report.md`](manual-test-report.md)),
ordered by what it costs to leave undone. Items that already have a home in
[`STATUS.md`](STATUS.md) §3 say so rather than restating them — where this list disagrees
with a gap's recorded impact, that disagreement is itself an item.

Each entry says what to do, where to look, and what "done" means. Anything marked
**Investigate** is a question to answer before writing code, because the fix depends on the
answer.

---

## P0 — the core claim (resolved, with one new defect behind it)

### ~~1. Wire caveat evaluation into the deployed PEP~~ — **DONE**

Closed by `authorize_request()`: the PEP now evaluates the token's own Datalog on every
request, so every `check if` and `reject if` in every block binds. Verified against a live
PEP — the four cases that failed in the manual pass (C2, C3, C4, and the D2/D5 ceilings)
now refuse, naming the block and quoting the check.

**The investigation in item 2 is what made this cheap, and its answer changes item 2.**
The caveats compile to Datalog that already lives inside the token; they never needed to be
parsed back into `Caveat` objects to be *enforced*. `verify()` reads facts out and
deliberately never calls `authorize()`, so nothing evaluated them. Calling it is the whole
fix. Cost: ~102 µs median on the hot path, `decide()` p99 now 395 µs against NFR-1's 1 ms.

Left open deliberately: `BUDGET_EXHAUSTED_CAVEAT` maps to HTTP 429, which reads as
"retry later" for a ceiling that is permanent. Folded into item 11.

### 2. Build the Datalog→caveat parser (STATUS gap 2) — **scope reduced**

**The investigation is answered: enforcement does not need this.** Biscuit's own authorizer
evaluates the token's checks natively once the request facts are supplied, measured against
the installed `biscuit-python` — so item 1 closed without a parser, and a token received
from a third party is now enforced exactly as well as one this process minted.

What still needs the parser is **display**, which is what STATUS gap 2 actually describes:
naming the effective bound in the console, and the identity tree's per-agent detail. That
is a real gap and a much smaller one than "attenuation does not work".

- **Note carried from STATUS:** whatever parses block source **must not trust it** — TM-24.
- **Done when:** a `VerifiedToken` obtained from an untrusted third party yields its true
  effective bound for the console to display. Enforcement no longer waits on it.

### ~~3. Correct STATUS gap 2's impact statement~~ — **no longer needed**

Gap 2's text — a *reporting* limitation — is now accurate again, because item 1 closed the
enforcement half by a different route. Worth a line in `STATUS.md` recording that biscuit's
authorizer is what enforces attenuation, so the next reader does not re-derive it.

### 3b. New, found by item 1's investigation: the mandate's budget check is existential

`mint_root` emits `check if requested($dim, $v), budget($dim, $max), $v <= $max;`. `check
if` passes when *any* binding satisfies the body, and ADR-007 requires every dimension to
be supplied — so `requested("tool_calls", 0)` against its own ceiling satisfies the check
and an over-budget `spend_bdt` never decides it. Measured: supplying only the offending
dimension denies; adding one satisfied dimension flips it to allow.

Mitigated in practice — `decide()`'s step 4 comment already delegates the mandate ceiling
to the ledger that issues the lease, and the lease pool does enforce it. But the token does
not enforce it standalone, which matters for the offline-verification claim.

- **The fix is a quantifier, not a value:** `reject if requested($dim, $v), budget($dim,
  $max), $v > $max;` fails if *any* binding matches, which is the universal reading.
- **Touches the token format**, so it is spec-first: spec 01 §5 documents the six checks.
- **Note:** the per-caveat `BudgetCeiling` is *not* affected — it names a literal dimension,
  so its `$v` ranges over one fact and it is universal by construction. Verified.

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

Three cases, all the same shape — the status tells a client to do the wrong thing:

- `401` for an **unmapped route**. That is a routing decision, not an authentication one.
- `401` for `TOKEN_REVOKED` / `ANCESTOR_REVOKED`. The token authenticated fine; it is no
  longer authorized. `403` fits.
- `429` for **`BUDGET_EXHAUSTED_CAVEAT`** (found while fixing item 1). `429` means "retry
  later", and a caveat ceiling is permanent for the life of that token — retrying can only
  ever fail. `LEASE_UNAVAILABLE` is the one that genuinely is retryable, and the two now
  share a status, which is exactly the distinction a client needs.

- **Check:** whether `tests/` or the SDK pin the current codes before changing them.

### 12. Make the SBOM reproducible across platforms

`generate_sbom.py` builds the component list from the resolved environment, so it differs
by host OS: the committed file carries `uvloop` (Linux-only) and a Windows run produces
`colorama` + `pywin32` instead. 136 components versus 137.

CI is unaffected — it runs on ubuntu and matches the committed file — so this is not a red
build. What it does mean is that **`make security` cannot pass on a Windows dev machine**,
and the `--check` gate silently only holds for one platform, which is not what a byte-exact
determinism gate is supposed to mean.

Two commits already fixed OS-dependent SBOM problems (`feae378`, `0601b04`), so this is the
third of the same family and worth fixing at the root rather than again.

- **Investigate:** whether to resolve from `uv.lock` (which carries markers for every
  platform) rather than from the installed venv. That makes the SBOM a function of a
  committed file, which is what the rest of the `--check` family already is.
- **Done when:** `uv run python scripts/generate_sbom.py` agrees with the committed file on
  Linux and Windows alike.

### 13. Give `performance.md` a CI drift check (STATUS gap 24)

Already tracked, and this pass made it sharper: `decide()`'s median moved 151.3 → 264.1 µs
and nothing in CI would have noticed. `generate_benchmark_results.py --check` exists and is
called by no job, `Makefile` target, or `make.ps1` target.

Gap 24 explains why a byte-exact check is wrong here — PB-2's timings vary run to run by
design — so this needs a tolerance band or a structural check, which is the design decision
gap 24 left to T-053.

---

## P3 — verification gaps in this pass

### 14. Verify the narrowing-only approval invariant against a live request

The manual pass could **not** confirm "approve narrows, never widens" (EC-A09) end to end:
the auth gate returns `401` before the widening check runs, so the check was never reached.
It is covered by `tests/integration/test_escalations_api.py`, but the property `DEMO.md`
leans on hardest has never been demonstrated against a running system.

- **Needs:** the Keycloak login path wired far enough to obtain a session cookie for an
  approver on the allowlist.
- **Done when:** a live `POST /v1/escalations/{id}/approve` asking for more than was
  requested is refused, with the refusal shown in the console.

### 15. Exercise the console pages that this pass could only check over HTTP

The decisions and identity-tree pages hold open `EventSource` connections, so headless
screenshot capture never terminates and they were verified via their APIs rather than
rendered. Budgets, overview, policy, audit and escalations were confirmed visually.

- **Done when:** the two SSE pages are confirmed rendering live rows, ideally with a
  browser-driven check that can run unattended.

---

## Suggested order

**Item 1 is done**, which took items 2 and 3 with it — 2 shrank to a display concern and 3
became unnecessary. What is left in P0 is **3b**, the quantifier bug that fixing item 1
uncovered: small, well understood, and spec-first because it changes what `mint_root`
writes into a token.

After that, **4 needs 2**, and **5, 6, 7 are independent and can start any time** — 7 first
if the goal is to make the system demonstrable again, since it is what turns every other
item on this list into something you can see rather than read about.
