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

### ~~3b. The mandate's budget check was existential~~ — **DONE**

Fixed spec-first. `mint_root` now emits **one check per dimension, naming it literally** —
the form `BudgetCeiling` already compiled to — instead of a single check over a ranging
`$dim`. Spec 01 gains §2.3, which records the measurement and, in the same place, why the
obvious alternative was rejected: `reject if requested($dim, $v), budget($dim, $max),
$v > $max;` gets the quantifier right and the absence semantics wrong, because `reject if`
is vacuous when the fact is missing, so an omitted dimension would become unconstrained.

Also measured: the ranging form allowed an **omitted** dimension too, so §2.2's fail-closed
guarantee never held for this check either. Six regression tests cover both properties, and
they authorize a real biscuit rather than reading facts back — the bug was invisible to
fact extraction, since `scaled_budget` reported the right ceilings the whole time.

Cost: +220 base64 characters on the authority block, measured by minting the same mandate
both ways. Deepest permitted chain is 4,892 characters, still 60% of the hard limit, so
§9's conclusions and ADR-006's deferral of T-010 are unchanged.

Two tests were relying on the bug and now test what they claim: `test_a_policy_denial_is_403`
and the e2e slice's `test_the_denial_never_reached_the_tool` both used an amount over the
mandate's own budget, so they were refused at step 4 and never reached Cedar.

---

## P1 — visible in the demo

### 4. Give agents real identities instead of `agt-depth-{N}`

**Downgraded by item 5.** The tree renders correctly now — it keys on the token's terminal
block id, which is unique — so this is a labelling problem, not a blank-canvas one. `DEMO.md`
beat 2's *shape* is demonstrable today; the three sub-agents are just all called
`agt-depth-1`.

`pep_service.py:382` derives `agent_id=f"agt-depth-{token.depth}"` and `role` from a single
configured default, so every sibling shares one name and every `role` reads `"unknown"`.

- **Blocked on:** item 2.
- **Done when:** `/v1/tree/{task}` returns one distinct node per real agent, with the role
  the parent assigned at `attenuate()` time.

### ~~5. Make the identity tree fail loudly, and render regardless~~ — **DONE**

Two changes, and the first turned out to be exact rather than a workaround.

**Key on the terminal block id, not `agent_id`.** A token's block chain already identifies
an agent uniquely — `block_ids` is the chain root-first, so `block_ids[depth]` is this
agent's own block and `block_ids[depth - 1]` is its parent's. Biscuit block ids are
content-addressed, so uniqueness is structural rather than hoped for. That also replaced
the previous parent *inference* (a prefix search over `block_ids`) with a direct lookup —
the search was both slower and the thing collapsing siblings onto one node.

**Surface a failure instead of returning.** `catch(e) { console.error(...); return; }` is
what made a broken tree look like an empty one, under a status indicator still reading
"Connected". There is now a visible banner, and a missing depth-0 node says so too.

Verified by replaying `updateTree()` against the exact five-node payload that used to
throw `ambiguous: agt-depth-1`: **5 descendants drawn**, shaped root → three siblings →
one grandchild, which is `DEMO.md` beat 2.

**This downgrades item 4.** Real agent names are now a *labelling* problem — the tree
renders correctly without them, where before the duplicate ids stopped it drawing at all.

### ~~6. Make lease size configurable~~ — **DONE**, and the second half deliberately not

`AGENTIAM_PEP_LEASE_SIZE` now overrides the 5,000 default. Set-but-unparseable is an error
rather than a silent fall back: a typo would otherwise take effect as the number nobody
wrote, and this module's whole posture is that a misconfigured PEP refuses to start. Five
tests, including one asserting the value reaches `PoolSettings` — a setting that parses and
is never used would be the same bug wearing a different hat.

**The investigation the item asked for, answered: do not make an over-lease request
top up to fit.** It looks like the obvious completion and it is not mine to make:

- Spec 04 §4.1 sizes a lease to bound what one PEP crash can strand. A request-sized
  top-up removes that bound exactly when the request is largest — the case the bound
  exists for.
- STATUS gap 8 already records that `ACQUIRE` does not clamp by `max_fraction`, so there
  is *no* blast-radius bound beyond TTL today. Adding request-sized top-ups on top of a
  missing clamp compounds two problems rather than fixing one.
- Both belong to **T-015** (adaptive lease sizing), which is where the formula actually
  applies and where spec 04 §12 says the algorithm lives.

Raising the lease is the operator's lever until then, which is what makes `DEMO.md` beat 4
reachable again. It is a real trade — more stranded budget on a crash — and the variable's
documentation says so rather than presenting it as free.

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

### ~~9. Schedule `reap()`~~ — **DONE** (STATUS gap 27)

Spec 04 §4.6's own pseudocode says `REAP() # background, every TTL/4`, and nothing
anywhere called it outside a test — confirmed by grepping every non-test call site. So a
lease stranded by a hard-killed PEP stayed `ACTIVE` and its budget stayed `leased`, not
lost (`committed` is untouched) but never reclaimed either.

It lives in the **control plane's** lifespan, not the PEP's, for two reasons: the ledger is
the control plane's, and the PEP that died is precisely the one that cannot reap its own
lease. Default 15 s — TTL/4 against the PEP's 60 s default, asserted against that constant
rather than hardcoded twice. `AGENTIAM_CONTROLPLANE_REAPER_INTERVAL_S=0` disables it for a
deployment sweeping some other way.

Two design points worth keeping:

- **Off by default in `create_app`, on in `create_app_from_env`.** A background task
  retiring leases underneath a ledger test would make it flaky in a way that reads as a
  ledger bug. Same split `pep_service.py` already uses.
- **A failed sweep logs and continues.** An unreachable Postgres is the outage the request
  path already fails closed on; killing the control plane over it would take the console
  and the escalation queue down too. Tested: it retries rather than giving up after the
  first failure.

Seven tests, including one that watches it actually sweep on a 10 ms interval and stop on
shutdown.

### ~~10. Audit authentication and routing refusals~~ — **DONE**, as the alternative

The item said to investigate whether the omission was deliberate before changing either
side. It is, and structurally so: a `DecisionRecord` requires `principal_id`, `task_id`,
`agent_id`, `depth` and `token_chain_ids`, every one read off a **verified** token. A
refusal that happens before verification has no truthful value for any of them, and
inventing one would put an unauthenticated caller's guess into the hash chain. Recording
them would also let anyone who can reach the PEP grow the chain without presenting a
credential.

So the fix is the one the item named as the alternative: **stop returning a `decision_id`
that resolves to nothing.** Spec 09 §11.3 assumed every refusal has a record — it says
`decision_id` "ties the refusal to the audit record" — and now states the exception and
why. `trace_id` still travels, which is the question a client can actually get answered
here.

Measured before the change: an unauthenticated request returned `decision_id: b67e507f-…`
and `GET /v1/audit/search?decision_id=b67e507f-…` returned `{"results": [], "total": 0}`
with the chain length unchanged — an identifier the client could quote to an operator who
would then find nothing, which reads as a *lost* record rather than an absent one.

One existing test asserted the old contract (`test_every_refusal_carries_a_decision_id`,
using a request with no token at all — the very case). Split into three: a recorded
refusal names its record, a pre-verification refusal omits the id and keeps `trace_id`,
and an unmapped route does the same because routing is decided before there is a token.

### ~~11. Fix the status codes~~ — **investigated, no change. I was wrong.**

All three cases I filed are deliberate, and spec 09 §11 says so in text I had not read
when I wrote the item. Recording that rather than changing code to match a complaint that
does not survive contact with the reasoning.

- **`TOKEN_REVOKED` / `ANCESTOR_REVOKED` → 401.** §11.1: *"A revoked token returning
  something distinctive tells a holder of a stolen token that the theft was noticed."* It
  is a deliberate mitigation of TM-01's accepted bearer-replay risk. The `reason_code` in
  the body does distinguish them, for the agent the token was issued to; the status line is
  what a passive observer sees. Changing it would leak exactly what §11.1 is hiding.
- **`BUDGET_EXHAUSTED_CAVEAT` → 429.** §11.2 chose 429 over the semantically apter 402
  because proxies and load balancers do not treat 402 consistently, and states that *"the
  distinction that matters — no budget versus no authority — is carried by the reason code,
  which every response has."* My objection (a caveat ceiling is permanent, so "retry later"
  misleads) is real but narrower than it looked: `Retry-After` is deliberately unset, so
  nothing tells a client the wait is finite, and a token with a different ceiling is a new
  token — which is equally true of the 403 cases. Not worth splitting the row for.
- **Unmapped route → 401 `MALFORMED_REQUEST`.** This one I had to reason about rather than
  read. Route extraction runs at step 1, *before* verification at step 2, so no identity has
  been established when the refusal happens. 403 would assert an identity the PEP has not
  checked; 404 would let an unauthenticated caller enumerate which routes exist. 401 is the
  weakest claim of the three and the message is explicit ("an unmapped route is an
  unreviewed route").

**The lesson worth keeping:** two of these were filed from a live response body without
reading the spec section that governs it. Check the spec before filing a status-code
complaint — the reasoning is usually already written down.

### ~~12. The SBOM's platform dependence~~ — **DONE**, but not the way I filed it

I filed this as "make the SBOM reproducible across platforms, probably by resolving from
`uv.lock`". Reading `generate_sbom.py`'s docstring first would have saved that: resolving
from the installed environment is a **measured, deliberate** choice, root-caused twice —
two independent fresh Ubuntu containers produce byte-identical output, a long-lived local
venv does not, and the difference tracks the host OS rather than venv staleness. Changing
it would have fought a decision made with evidence.

**The real defect was the failure message, and it was worse than the drift.** On Windows
the script said:

    docs/evidence/sbom.json: OUT OF DATE.
    Re-run with --write and commit the update.

Both sentences false, and the second actively harmful: doing what it said commits a
137-component Windows SBOM over the 136-component Linux one and breaks CI's security-scan
job — for a developer who followed the tool's own instruction. `.gitattributes` records
that development happens on Windows, so this was aimed squarely at the usual case.

Now: `NOT CHECKED` and exit 0 off the reference platform, so `make security` passes where
it cannot verify anything; `--write` refuses unless `--force`. Eleven tests, covering both
branches of the platform check so neither can regress on a host that cannot reach the
other.

### ~~13. Give `performance.md` a CI drift check~~ — **DONE** (STATUS gap 24)

Gap 24 held this back on a premise that turns out not to apply. It says a byte-exact
check is the wrong instrument because "PB-2's raw timings vary run to run by design, so a
naive check would fail on ordinary noise". That is true of *re-running the benchmark* and
not true of `--check`, which re-renders from the **committed** `pb2-breakdown.json` and
`nfr2-load.json` and compares to the committed Markdown. It is a pure function of files
already in the tree.

The noise lives in *producing* the JSON, which the `quality` job's benchmark step does —
so the check goes in the `evidence-pack` job, which never runs a benchmark and therefore
never rewrites the JSON underneath itself. Same job already does the same thing for the
evidence pack, which folds `performance.md` anyway.

Gap 24's other half — catching a genuine *regression* — was already covered and I had not
noticed: `test_the_whole_decision` asserts `decide_total` p99 < 1000 µs against NFR-1's
budget, and CI runs it. So the two concerns were always separable; only one of them was
unguarded.

Also adds the `benchmarks` target to `Makefile` and `make.ps1`, the other two places gap
24 named as having no caller.

This is the check that would have caught wiring the token's Datalog into `decide()`:
the median moved 151.3 → 264.1 µs and nothing would have noticed the document still
claiming the old figure.

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

**P0 is clear.** Items 1 and 3b are done, 2 shrank to a display concern, 3 became
unnecessary.

What is left: **4 needs 2**, and **5, 6, 7 are independent and can start any time** — 7
first if the goal is to make the system demonstrable again, since it is what turns every
other item on this list into something you can see rather than read about. 8 is small and
closes a real hole: the lease-priming fix currently has no test.
