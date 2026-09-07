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

### ~~2. Build the Datalog→caveat parser (STATUS gap 2)~~ — **DONE**

`agentiam_core.datalog` reads a token's rendered block source back into caveats and identity
facts. Spec 02 gains §11; STATUS gap 2 is closed. Enforcement was already closed by item 1 and
this changes nothing about it — biscuit's authorizer is still what refuses a request.

**Two measured facts made the obvious implementation the wrong one**, and both were checked
against the installed library rather than taken from the notes:

- **`block_source()` normalizes.** Whitespace collapses and facts are grouped ahead of checks
  regardless of how the block was built. So the grammar to recognize is biscuit's *output*
  grammar, not `to_datalog()`'s — a recognizer proved against the compiler's output would be
  proved against text it never receives. Every round trip in the tests mints, attenuates,
  verifies and reads back through a real biscuit.
- **`block_source()` escapes nothing**, which TM-24 already said, plus two things it did not:
  a `;` inside a string survives rendering, and a `\n` in a literal renders as a **real
  newline**. Statements can be split neither on `;` nor line by line, so `_statements()`
  tracks quote state.

**The rule that carries the security weight is what happens to a statement it cannot read.**
It is reported, never dropped — a caveat this build cannot recognize is a restriction the
token *has* and the fold does not, so a bound computed without it is an upper bound. Dropping
it would overstate authority, which is the one direction a display path must never get wrong.
`TokenAuthority.complete` carries that, and the docstring says a consumer displaying the bound
must display it too.

TM-24's note was the design constraint, not a footnote. An identity fact appearing twice —
the exact signature of a value that broke out of its own string literal — refuses the field
rather than picking one, and a label that would fail `validate_label` is refused on the way
*out* as well as in, which is what extends T-011's mitigation to a token this system did not
mint. Injection cannot widen a bound in any case, and the ADR says why rather than hoping: the
fold intersects and takes minima, so a smuggled clause can only add an apparent restriction.

48 tests. Three consumers wired, all three previously blocked on this.

### ~~4. Give agents real identities instead of `agt-depth-{N}`~~ — **DONE**, minus one field on purpose

`pep_service.principal_for` reads `agent()` and `role()` off the token's terminal block. Verified
against the running demo stack, which is the form item 4's "done when" asks for:

| agent_id | role | depth |
|---|---|---|
| `agt-depth-0` | unknown | 0 |
| `agt-doc-reader` | reader | 1 |
| `agt-negotiator` | worker | 1 |
| `agt-payer` | payer | 1 |
| `agt-settlement` | payer | 2 |
| `agt-subcontractor` | payer | 3 |

Three of those used to be `agt-depth-1`, and every role read `unknown`. `agt-depth-0` and its
`unknown` role are correct rather than left over: a root token has no attenuation block, so it
declares no agent and no role, and the fallback says "not stated" instead of inventing one.

**`role` deliberately does not become Cedar's `principal.role`, and this is the finding of the
item.** It looked like the obvious completion and it is an escalation. A block's `role` is
written by the delegating parent; the demo's own corpus bundle grants `invoice:write` on
`principal.role == "senior"` and forbids critical resources without it. Sourcing it from the
block lets any agent that can attenuate name its own child `"senior"` and pass both guards —
`declared_depth`'s mistake (ADR-005) one field over, against a policy this repo ships. Spec 01
§6.1 already said so in a sentence easy to read past: `role(name)` is *"for the console and
audit"*. So the parent's claim travels as `AgentPrincipal.declared_role` → `DecisionRecord.role`
→ the tree, and Cedar keeps seeing configuration. A test reads the `Agent` entity at the FFI
boundary and pins its attribute set, so adding `declared_role` there fails rather than ships.

`agent_id` *is* parent-asserted too and is used anyway, because it is the only place a
sub-agent's identity exists. What makes that safe is that the name labels a node the chain
already identifies cryptographically; and because it reaches the Cedar entity uid, a block that
names it ambiguously falls back rather than letting the crafted block choose.

**Two things this uncovered, both fixed here.**

- **`failing_caveat` was blocked twice.** Spec 09 §4 said the missing parser was why the field
  was always `None`. Supplying the caveat list showed the pipeline also never carried the
  `CaveatRef` `decide()` returns onto the record at all. Verified live: five caveat-caused
  denials each naming their caveat kind and chain position, and the two non-caveat denials
  correctly naming none. No test could have found the second block without first building the
  parser.
- **The SSE tree diff collapsed siblings.** `build_tree_diff` keyed on
  `(agent_id, block_ids[0])`, and `block_ids` is root-first — so the second half is the *root*
  block, identical for every node in a task, leaving `agent_id` to carry the key alone. Under
  the old naming, three depth-1 siblings produced **one** diff entry: the stream animated one
  in and dropped two. The initial `snapshot` event sends the full list, so the first paint was
  right and only later updates were wrong, which is how it survived item 15's live check. Now
  keyed on the *terminal* block — the same key the console's d3 tree uses, and for the same
  reason.

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

**My verification here was not good enough, and item 15 caught it.** Replaying
`updateTree()`'s stratify block against a real payload proved the *keying* was fixed and
nothing else — the page still drew no nodes at all, for two further reasons found only by
driving the real thing. Both are fixed and written up under item 15. A harness that
replays one function is evidence about that function, not about the page.

**This downgrades item 4.** Real agent names are a *labelling* problem now — the tree
renders, and the labels on it read `agt-depth-N`.

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

### ~~7. Build T-057's demo seed script~~ — **DONE**

`scripts/seed_demo.py`, in two phases because the PEP binds its mandate at boot (ADR-056):

- **`--out /secrets`** runs as a one-shot compose service between `migrate` and `pep`.
  Creates the pool, mints the chain, writes the tokens beside the bootstrap credentials.
  Idempotent, since compose re-runs one-shot services on every `up`.
- **`--drive`** sends the scenario's traffic after the stack is healthy. `make demo-seed`.

**The mandate id is a fixed constant.** Compose has to name it in the PEP's environment
before the seeder has run, so the two must agree; `test_demo_compose.py` checks that they
do. The old all-zeros placeholder is gone, and the PEP now `depends_on` the seed — without
that ordering it primes against a pool that does not exist, warns, and refuses every
budgeted request, which is precisely the empty console this item exists to end.

Measured on a fresh `down -v` → `build` → `up --wait`: **stack healthy in 26 s** (NFR-8's
budget is 90 s), then 12 calls producing **7 allows, 3 SCOPE_ATTENUATED_AWAY, 2
BUDGET_EXHAUSTED_CAVEAT, 1 POLICY_DENIED**. Afterwards: 6 nodes in the identity tree
(root → three siblings → depth 2 → depth 3), `committed` moving on the budget dashboard,
and a 12-record audit chain that verifies.

Two things the scenario forced out that are worth recording:

- **`vendor:read` was unroutable.** The corpus policy permits it, the demo mandate grants
  it, the stub tools app serves `/vendors/{id}` — and `serve_pep.ROUTES` mapped nothing to
  it. So the negotiator could not make a single call, and never appeared in the tree, which
  is derived from decisions. Route added; the load generator does not touch it, so PB-2 and
  NFR-2 are unaffected.
- **A depth-3 agent is what produces a policy refusal.** The mandate's ceiling and the
  bundle's are both 500,000, so no *amount* can be refused by one and not the other — a
  line captioned "the policy forbids" was actually being refused by the token's own budget
  check. The corpus bundle's `principal.depth <= 2` is the only condition a valid token
  cannot also violate on its own, so the chain now runs one level past it. A test asserts
  the scenario keeps reaching all four refusal layers.

### ~~8. Test that the deployed PEP primes its lease pool~~ — **DONE**

The priming bug (fixed in `5c1f845`) was invisible to the suite:
`tests/unit/test_pep_service.py` passed identically before and after — 22 either way,
because `Service` exposed the revocation set, the policy engine and the drift oracle but
not the pool. Nothing here could see the thing that was broken.

Six tests, and `Service` now exposes the pool — which is what its own docstring says it is
for. Verified by removing the `prime()` call and re-running: two of them fail.

Two of the six arrived later, from a real regression. `prime()` **raises** as well as
returning `False` — `ACQUIRE` does `scalar_one()` on the budget row, so a mandate with no
row raises `NoResultFound` — and handling only the `False` return turned that into a boot
failure. `docker-compose.demo.yml` points the PEP at exactly such a placeholder mandate on
purpose, so the container stopped starting and CI's demo-stack job timed out. Fixed in
`c3ccfa4`; both exception paths are now covered.

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

### ~~14. Verify the narrowing-only approval invariant against a live request~~ — **DONE**

EC-A09 holds against a running control plane. The session cookie is forged with the
deployment's own secret, exactly as `tests/integration/test_escalations_api.py` does —
which is not a shortcut past the auth gate but the only way *through* it to the check
under test. Keycloak decides who a principal is; this is about what an authenticated
approver may then do.

| approve request | result |
|---|---|
| widen the amount, 75,000 requested → 90,000 approved | **400** — "approval would grant amount 90000, above the requested 75000.0000" |
| widen the scopes, adding `invoice:write` | **400** — "approval would grant ['invoice:write'], which was not requested" |
| narrow, 75,000 → 50,000, scopes unchanged | **200** |
| any of the above with no session | 401 — the gate that hid all of this |

**Two things the exercise turned up.**

A *separation of duties* control I did not know existed and which is not in `DEMO.md`:
approving is refused with "cannot approve their own agent's escalation" when the approver
is the principal that raised it. My first run had them as the same identity, so both
widening cases were refused for the wrong reason and passed — the same shape of masking
that hid this invariant in the first place. Worth putting in the demo script; it is a
control a bank CTO would ask about.

And a small defect, filed as item 16.

### ~~15. Exercise the console pages that this pass could only check over HTTP~~ — **DONE**, and it found two bugs

`chrome --headless --screenshot` waits for the load event, and both pages hold an
`EventSource` open forever, so it never writes a file — which is why they had only ever
been checked through their APIs. Driving Chrome over the DevTools Protocol instead
(navigate, settle, `Page.captureScreenshot`, plus `Runtime.evaluate` to read the DOM and
`Runtime.exceptionThrown` to catch errors) works, and the DOM probe matters more than the
picture: a screenshot proves the page painted, not that it painted the right thing.

**Decisions** was fine: 13 rows, `live`, empty state hidden, each refusal naming the block
and check that caused it, sub-millisecond latencies.

**The identity tree had never rendered a single node.** Two bugs, stacked behind the one
item 5 fixed:

1. `d3.linkHorizontal()`'s `.x`/`.y` accessors receive each *endpoint*, not the link, so
   `.x(d => d.source.y)` dereferenced twice and threw `TypeError: Cannot read properties
   of undefined (reading 'y')` on the first link. That happens *before* the node join, so
   no node was ever created — the tree drew its edges and nothing else.
2. With that fixed, all six nodes existed and none were visible. `nodeEnter.transition()`
   faded them in and `nodeUpdate.transition()` moved them; an element runs one unnamed
   transition at a time, and starting the second **cancels** the first, so the fade never
   ran and they stayed at the `opacity: 0` they were created with.

Neither is visible from the API, and neither is visible from replaying `updateTree` — the
re-run works precisely because the enter selection is empty by then. Only the real page
under a real browser shows it.

Verified after both fixes: `nodes: 6, visible: 6, labels: 6, links: 5`, no exception, and
the picture is root → three siblings → depth 2 → depth 3 with circles, labels and budget
bars. `DEMO.md` beat 2, on screen, for the first time.

**Not committed:** the CDP driver lives in the session scratchpad. Making it a permanent
check means either a browser-automation dependency (Playwright pulls a browser download)
or a hand-rolled CDP client in CI, and neither is worth it for two pages until something
else needs a browser. Worth revisiting if a third SSE page appears.

---

## Found since

### ~~16. A duplicate escalation returns 500, not 409~~ — **DONE**

409 now, naming the escalation that is in the way — in the message and as
`existing_escalation_id` in the body, since reading it is the caller's next action. Seven
tests against real Postgres; verified by reverting the fix, at which point all seven fail and
the twenty-five that were already there still pass.

**Not absorbed the way a duplicate revoke is**, and the difference is the decision worth
recording (ADR-058). Spec 07 §9 makes `POST /v1/revocations` idempotent on `block_id`, which
is correct *there*: a repeat revoke asks for a state the table already holds, so returning the
existing row answers the caller truthfully. A second escalation may name different scopes, a
different amount and a different reason, and returning the first as though it answered this
request would report a grant nobody asked for.

Which constraint fired is inferred from **what is in the table** — after a failed insert,
`create` looks the `decision_id` up; a row means `uq_escalations_decision_id`, and anything
else re-raises unchanged rather than being reported as a duplicate it is not. String-matching
a constraint name out of an asyncpg error wrapped by SQLAlchemy's adapter would couple this to
two layers' formatting, and the lookup costs nothing on the path that succeeds.

**The other `POST` routes checked, as the item asked.** `/v1/revocations` is already
idempotent by design. `/v1/audit/verify` and the two `/policy/*` console routes create
nothing. `.../approve` and `.../deny` are guarded by `SELECT ... FOR UPDATE` and already map
their conflict to 409. This was the only one.

---

### ~~17. The authority block's own budget ceiling reports `BUDGET_EXHAUSTED_CAVEAT`~~ — **DONE**

Spec first, per item 11's lesson, and the spec was already unambiguous: spec 02 §7 maps a
`BudgetCeiling` *caveat* to `BUDGET_EXHAUSTED_CAVEAT` and the *authority budget* to
`BUDGET_EXHAUSTED_MANDATE`. `_first_failure` was reading only the fact a failed check
quantifies over, and `requested(` is the same fact in both — **the block is what tells them
apart**.

Two of the seven codes turn on the block, not one: `operation(` in block 0 is the
grant-membership check, so a miss is `SCOPE_NOT_GRANTED` rather than `SCOPE_ATTENUATED_AWAY`.
The other four mean the same thing wherever they sit.

**The investigation the item asked for, answered.** Spec 09 §7's reachability table *did*
assume the old mapping — it listed `BUDGET_EXHAUSTED_MANDATE` as step 7 only. It now names
both routes, with a new §7.1 on why they are the same answer to an operator (the mandate does
not allow this) reached by different mechanisms: the ledger bounds the *pool* across requests,
the token's ceiling bounds a *single* request (spec 02 §4.2). Both codes are 429, so no client
sees a different status; a test pins that so a relabelling can never become a contract change.

**Why this was the one that surfaced.** Five of block 0's six checks are shadowed on the
`decide()` path by Python re-implementations that refuse first. Nothing re-implements the
per-request ceiling, so it is the only authority check biscuit actually gets to refuse — which
is why it reached a live decision record wearing the wrong label. There is now a test asserting
exactly that shadowing, because the docstring claims it. The mapping is applied **by block
rather than by caller** regardless: `authorize_request` is public and its codes must be right
for anything that calls it, not only for the path that currently shadows five of them.

Verified live: the demo scenario now reports five distinct outcomes where it reported four —
`BUDGET_EXHAUSTED_MANDATE=1` split out from `BUDGET_EXHAUSTED_CAVEAT=1`. The seed's own
refusal-layer test asserts the fifth, so the scenario cannot lose it silently.

### ~~18. The root node in the identity tree has no name of its own~~ — **DONE**

`TreeNode.is_principal` — a **computed** field, not a stored one, because it *is* `depth == 0`
and a second copy could disagree with the first. Structural rather than a string match on
`agt-depth-0`: depth 0 means no attenuation block, and an `agent()` fact only ever lives in one.

The console names the depth-0 node by its `principal_id` with the subtitle `principal`, and
the PEP is unchanged — inventing a name there is what spec 01 §6.1's fallback rule exists to
prevent, so the naming belongs on the display side where it is a rendering choice rather than
an identity assertion. `agent_id` stays `agt-depth-0` in the data: it is the audit key, and
`/v1/tree/{task}/blocks/{agent_id}` looks up by it.

**Driving the real page found a second defect, which is why it was worth driving.** A Keycloak
subject is `kc:` plus a UUID — 39 characters. Labels are centred on the node, so it rendered
252 px wide against ~91 px for `agt-doc-reader`, and its left edge landed at **x = -4**:
clipped off the canvas. Both `agent_id` and `principal_id` are free text up to 128 characters
(spec 01 §6.1), so this was reachable for a long agent name too and is not specific to the
principal. Labels now truncate at 20 characters, with the full value in the `<title>` tooltip
and in the detail panel.

Verified under a real Chrome over the DevTools Protocol, not replayed: 6 nodes, no error
banner, root reading `kc:11111111-1111-11…` / `PRINCIPAL` at x = 58 and clear of the depth-1
column at x = 327, with the five agents on their own names and roles.

### ~~19. Two log-assertion tests go vacuous when every suite runs in one process~~ — **DONE**, and it was our bug

Root cause found, and it is first-party: the alembic env called
`fileConfig(config.config_file_name)`, whose `disable_existing_loggers` **defaults to `True`**.
Running any migration therefore set `disabled = True` on every logger that already existed and
was not named in `alembic.ini`. Alembic's generated template ships that default, so it is a bug
the scaffold hands you rather than one anybody wrote.

Diagnosed by instrumenting rather than guessing: `isEnabledFor(INFO)` came back **False** while
`logger.level == 20` and `getEffectiveLevel() == 20` — which is `Logger.disabled`, not a level
problem. One keyword fixes it.

**It was never only a test problem.** `scripts/run_load_test.py` migrates and then measures, in
one process — so the harness behind `performance.md` was switching off the PEP's own logging
before taking a reading. A warning during a load run could not have reached anyone.

**And the vacuity was hardened separately, because the root cause is not the only way to get
there.** Three negative assertions could pass against empty captured output, including the
runtime half of the secret scanner — the automation of rule 10 and NFR-5, which would have
driven four log sites, captured none, and reported clean. Each now asserts something *was*
captured first. `test_the_body_reaches_the_log_on_a_retry` needed no change: its assertion is
positive, so it already failed loudly.

Verified: 2,627 tests pass in a single-process full-suite run, which is the configuration that
used to produce the two failures.

### ~~20. `performance.md`'s numbers do not include the block-source parse~~ — **DONE**, as the documented alternative

The item offered two completions; this is the second, and the first is not honestly available
here. Re-pointing `serve_pep.py` at the deployed composition invalidates `pb2-breakdown.json`
and `nfr2-load.json` together, and the replacement run would come from this Windows laptop
rather than whatever host produced the committed figures of 2026-08-18 — swapping a stable
measurement for a non-comparable one is worse evidence, not better.

So `performance.md` now states the gap, with the numbers, in three block quotes under the NFR-2
tier explanation. It is written into `generate_benchmark_results.py` rather than the Markdown,
so the drift check (item 13) keeps it attached to the JSON it qualifies and a future
re-measurement drops it by editing the generator.

What it says: the harness hardcodes its principal and supplies no caveat reader; one
`token_identity()`/`token_caveats()` costs ~125 µs at depth 0 rising to ~227 µs at depth 3; the
pipeline resolves each once per request, so a depth-3 request pays ~0.45 ms beyond what tier 3
reports, against an 8 ms budget. **NFR-1 is untouched** — the parse is in the pipeline, not
inside `decide()`, which is why the PB-2 breakdown needs no such note.

- **Still open, deliberately:** the re-measurement itself. It wants the same host as the
  committed figures, and it is a benchmarking pass rather than a side effect of another
  ticket. Filed as item 21 so it is not lost in a closed item's prose.

---

### 21. Re-measure NFR-2 — **half done**: the harness now matches, the numbers do not yet

**Done (ADR-062).** `scripts/serve_pep.py` composes the PEP the way `scripts/pep_service.py`
deploys it — it reads the agent identity and the caveats off the token instead of hardcoding
one and skipping the other. `TestTheHarnessMatchesTheDeployedComposition` compares the two
pipelines *by behaviour*, because source text cannot tell a wired hook from a mentioned one,
which is how the drift survived review. Verified by reverting each half separately.

`create_app` also puts the pipeline on `app.state` now. It was a closure variable visible to
nothing outside its composition root, and that has cost this project three times — an unprimed
lease pool (item 8), an unwired caveat reader (item 4), and this.

**Still open: the measurement itself.** The committed figures were taken before the harness
changed, so they understate it by the ~0.45 ms a depth-3 request now pays.
`performance.md` says exactly that, with the numbers, instead of the earlier note claiming the
harness was deliberately left alone — which stopped being true.

- **Why it is still its own sitting:** it rewrites `pb2-breakdown.json` and `nfr2-load.json`
  together, and both have to come from one host in one run or the before/after comparison
  measures the hardware rather than the change. A full run is 2 profiles × 3 repeats ×
  3 tiers × 20 s of held load plus setup.
- **Watch out for:** `pytest -m perf` rewrites `pb2-breakdown.json` as a side effect (gap 24,
  item 13), and a run whose py-spy step fails can leave `.perf-profile-*` scratch files in
  `docs/benchmarks/` — now gitignored, because that directory is tracked evidence and
  `git add -A` would have swept them in.
- **What a smoke run showed** (5 s, 1 repeat, 100 RPS, this host, harness already matching):
  enforcement p50 1.79 ms / p99 6.829 ms, inside NFR-2's 8 ms budget. One short run is not
  the three `PLAN.md` §13.1 asks for and is recorded here as a sanity check, not as evidence.
- **Done when:** both JSONs are re-measured in one sitting and `performance.md`'s three
  "predate a change" block quotes come out of `generate_benchmark_results.py`, because they
  no longer describe anything.

---

### ~~22. Both nightly CI failures~~ — **DONE**

`1c07c93` — the commit this session started from — had a **green** push run and a **red**
nightly one. Two jobs only the nightly reaches were failing, and had been.

**`Chaos scenarios` — a drift check that could never pass.** The job ran `pytest -m chaos`
and then `generate_chaos_results.py --check`. The scenarios *rewrite*
`docs/benchmarks/chaos/*.json` as they run — fresh `run_id`, new `started_at`, different
`duration_s`, different event timings — so the step compared the committed Markdown against
JSON that had just changed underneath it. Reproduced locally: 10 scenarios pass in 2m20s and
leave all ten JSON files modified, after which `--check` reports the table stale.

This is gap 24's lesson, and `performance.md` already had it applied — `ci.yml` even spells
out why that check lives in `evidence-pack`, "a job that never runs a benchmark and so never
rewrites the JSON underneath itself". The reasoning was written once and not generalised. The
chaos check now sits beside it, which also means it runs on every push rather than only
nightly; a table that only drifts nightly is reported a day late.

The rule is a test now, not a comment: `tests/unit/test_ci_workflow.py` asserts that no job
both regenerates an artifact's inputs and byte-checks that artifact, that every check still
runs *somewhere* (the tempting fix for an impossible check is to delete it), and that none of
them sit in a conditional job. Verified by putting the old placement back — two of the five
fail.

**`Security scanning + SBOM` — gitleaks, nine findings, all false positives.** Reproduced with
the pinned scanner over all 107 commits:

| Finding | What it actually is |
|---|---|
| `8aba07e3…` ×2 | An Ed25519 **public** key. Published by design, and it has to be a real curve point rather than arbitrary hex because `PublicKey.from_bytes` rejects anything else — which is what makes it look secret |
| `eyJhbGciOiJIUzI1NiJ9…` | The canonical jwt.io example token, planted **deliberately** in `test_secret_scanning.py` so the scanner's positive path is proven to fire |
| `Ed25519PrivateKey` ×6 | Not a key. The `cryptography` **type name**, matched in fixture signatures like `def test_x(self, key: Ed25519PrivateKey)` |

The same public key was *also* hardcoded as `AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY` in two
tests, and that shape is one a scanner *should* flag — waiving it by value would have taught
the repo to wave it through. Those now generate a throwaway key per run, so the private-key
spelling is gone from the tree; the waivers cover the copies history still holds, which no
working-tree change can reach. Re-scanned: **no leaks found**, 107 commits.

- **Worth keeping:** neither job runs on a push, so both had been red for an unknown number of
  nights with every push showing green. `PLAN.md` §13 schedules chaos nightly on purpose and
  that is right; what was missing is that nobody was reading the result. A red nightly is only
  useful if someone looks.

### 23. One unexplained integration-job failure, watch for a second

`ec616d1`'s CI run failed `Ledger against real Postgres` at the `Integration tests` step.
Recorded rather than dismissed, because a first-time failure with no obvious cause is the
kind of thing that reads as noise until it is a pattern.

**What rules out the commit.** `ec616d1` changed `ci.yml`, `.gitleaks.toml`, two docs files
and three files under `tests/unit/` — nothing the integration job runs. The immediately
preceding push (`8965b89`) and the immediately following one (`2348909`, an actions bump)
both passed the same job on the same integration code. The job had been green for ten
consecutive runs before it.

**What was tried locally.** Four full `pytest -m integration` runs, 253 passing each; three
repeats of the race-prone modules (`test_ledger`, `test_ledger_commit`, `test_escalations`,
`test_lease_pool_crash`), 35 passing each. `pytest-randomly` is not installed, so CI and
local run in the same order — the ordering hypothesis is out. Not reproduced.

**Why it was not diagnosed further.** The Actions log endpoint needs authentication and the
unauthenticated check-run annotation carries only `Process completed with exit code 1`, so the
failing test is not identifiable from outside. `gh` is not installed on the development host.

- **If it recurs:** read the log from the Actions UI first — the test name is the whole
  question, and everything above is an attempt to answer it without one. The plausible
  candidates are the testcontainers fixtures (a slow Postgres start on a loaded runner) and
  the concurrency tests, which is where a real race would surface.
- **Worth considering either way:** `pytest -p no:randomly` appears throughout this session's
  local commands and does nothing, since the plugin is absent. Either install it — order
  dependence is a real class of bug and item 19 was one — or stop passing the flag.

---

## Found in the second manual pass (2026-09-07)

A hand-driven pass over the running demo stack — real biscuits, real Postgres, real Cedar,
real Chrome. 33 control-plane cases, 18 PEP cases, 7 console pages, plus revocation, audit,
custody, SSE, metrics and the activation gate. Written up in
[`manual-test-report.md`](manual-test-report.md) §8. Five anomalies; the first is critical.

### ~~24. The PEP stops authorizing every budgeted request 60 seconds after boot~~ — **FIXED**

**Critical, and fixed in this pass.** The deployed PEP primed one lease at startup and never
renewed it. Sixty seconds later — the default TTL — every request that spends anything was
refused `LEASE_UNAVAILABLE`, permanently. Reads still worked, so the service looked alive.

**Observed on the live stack** before the fix, after bringing it up and reading the console
for a couple of minutes:

```
POST /proxy/payments  {"amount":"100.0000"}   ->  429 LEASE_UNAVAILABLE   (x8, over 16s)
select … from leases  ->  1 row: granted 5000.0000, settled 0.0000, state 'expired'
select … from budgets ->  total 500000, committed 0, leased 0
```

**This is why the demo appeared to work**: `make demo-seed` runs within seconds of
`up --wait`, inside the first TTL. A judge who brings the stack up, reads the console, then
tries a payment would have seen every payment refused.

**Mechanism.** `check()` correctly refuses an expired lease and calls
`_maybe_schedule_topup`, which was guarded by

```python
if held.lease.remaining_local > held.lease.granted * self._settings.low_water:
    return
```

The lease expired **unspent**, so `remaining_local` was still the full 5,000 against a
low-water mark of 1,250 — the guard returned and no ACQUIRE was ever issued. A lease leaves
service two ways, by draining or by ageing out, and the guard only asked about the first. The
comment on `check()`'s refusal branch describes exactly this closed loop and says it was
"measured, then fixed here"; that fix covered the drained half and fell straight through the
expiry half.

**The fix** adds the same "no longer usable" test `check()` already refuses on, so the two
cannot disagree about whether a lease is worth holding. Replacing an expired lease is safe:
`_acquire` RELEASEs the old one and the ledger's `release()` is a no-op for a lease already in
a terminal state (spec 04 §3).

**Verified live on a rebuilt stack** — idle 75 s past the TTL, spending nothing:

```
attempt 1: 429 LEASE_UNAVAILABLE      <- correct: at that instant there is no usable lease
attempt 2: 200 OK                     <- the refusal scheduled the ACQUIRE
leases:  granted 5000.0000, settled 100.0000, state 'active'   (a new row)
budgets: committed 1700.0000, leased 4900.0000                 (reconciles exactly)
```

Five tests, and the first refusal is asserted as *correct* rather than papered over —
refusing once is right, refusing forever is the defect. Verified by reverting the guard: four
of the five fail.

### ~~25. The SDK's own intent header can never match a demo-minted mandate~~ — **FIXED**

`scripts/seed_demo.py:109` mints `intent_hash=hashlib.sha256(DEMO_INTENT.encode()).hexdigest()`
— a plain SHA-256 of the text. Spec 06 §1 says the intent is bound "using canonical JSON
serialization and SHA-256 (`agentiam_core.hashing.canonical_json`)", and `PLAN.md` §493 says
"sha256 of canonicalized description". The PEP agrees with the spec: given
`AgentIAM-Task-Intent` it computes `hash_object(text)`.

So the two disagree, and the SDK is on the losing side — `client.py:118` sets exactly that
header. **Observed, same token, same text:**

```
no intent header                              -> 200 OK       (token's own hash is used)
AgentIAM-Task-Intent: <the exact minted text> -> 403 INTENT_MISMATCH
x-agentiam-intent: sha256(text)               -> 200 OK
```

An agent using the official SDK, asserting the correct intent, is refused every call. The
demo only works because `seed_demo.py --drive` sends neither header.

`seed_demo.py` is the only place in the tree computing an intent hash this way — everything
else uses `hash_object`. So the seed is the deviation, not the spec.

- **Done when:** the seed mints with `hash_object`, an SDK client asserting the mandate's own
  intent text is authorized, and something asserts the two agree so they cannot drift again.

### ~~26. `email:send` is unroutable~~ — **FIXED**, and it uncovered item 29

The stub tools serve `POST /email/send`; the corpus policy has
`permit(… "email:send" …) when { !resource.is_external }` plus a `forbid` on critical
resources; the tool catalogue describes `email_internal` and `email_external` with
`is_external` set. `serve_pep.ROUTES` maps nothing to `email:send`, so a call returns
`401 MALFORMED_REQUEST` — an unmapped route.

Same shape as the `vendor:read` gap item 7 found and fixed. Lower severity: the demo mandate
does not grant `email:send`, so nothing can reach it today and no demo beat depends on it.
What it costs is that `!resource.is_external` — the one policy condition that keys on a
*resource* attribute — and the `external_emails` budget dimension have no end-to-end path.

- **Done when:** a route maps `email:send` to the stub's endpoint, or the corpus and tool
  catalogue stop describing a scope the deployment cannot route. Whichever is chosen, the
  three places that mention email should agree.

### ~~27. Chain-of-custody on an unknown task returns 200 with an empty list~~ — **FIXED**

`PLAN.md` §11.7 EC-A05: *"Custody query on an unknown action | 404 with a clear message."*

```
GET /v1/audit/custody/00000000-0000-4000-8000-000000000000
  -> 200 {"task_id":"00000000-…","entries":[]}
```

For a real task it is correct — 55 entries, in order. Only the miss is wrong, and it is the
same shape as item 10: handing the caller something that looks like an answer and resolves to
nothing. An operator cannot tell "this task did nothing" from "this task does not exist".

- **Note:** the endpoint keys on **task_id**, not the decision id its name suggests. Passing a
  decision id also yields `200 {"entries":[]}` — the same indistinguishable answer, which is
  how the confusion survives.
- **Done when:** an unknown task is a 404 naming what was not found, a real task is unchanged,
  and a test covers both.

### ~~28. `DEMO.md`'s F-2 drill scripts a failsafe that does not exist~~ — **FIXED**

F-2 says: *"Ollama slow or down → Template fallback engages automatically (T-031). The flow is
identical. Narrate: 'the template fallback just activated — this is a production-grade
failsafe.'"*

**T-031 is deferred**, and `STATUS.md` line 112 says so plainly: *"F-2 has no implementation
while it is deferred."* `tests/chaos/test_ch08_ollama_down.py` asserts its absence in two
places. So the runbook tells a presenter to narrate, under pressure, a feature the project
knows it has not built.

Observed with Ollama unreachable, which is the demo stack's default:

```
POST /policy/compile  ->  200, panel reads
                          "Error: Ollama network error: All connection attempts failed"
```

`STATUS.md` is also slightly off in the other direction — it says "beat 5 hangs", and what
actually happens is a prompt, clear error. Failing visibly is the better behaviour; both
documents just describe something else.

- **Done when:** F-2 describes what the system does — surface the error and move on — or
  T-031 is built and F-2 becomes true. Either way `DEMO.md` and `STATUS.md` should not
  disagree about a drill the presenter is meant to rehearse.

### 29. The deployed PEP has an empty tool catalogue, so every resource rule is inert

**Found while fixing item 26**, and more consequential than the item that turned it up.

`scripts/pep_service.py` builds `CedarEngine(bundle)` — with no `tools=` argument.
`CedarEngine.__init__` does `self.tools = dict(tools or {})`, and `_facts_for` falls back to
`_UNKNOWN_TOOL`, whose attributes are deliberately the *safe* end of every axis:
`sensitivity="low"`, `is_external=False`.

So in the deployed PEP **every tool looks low-sensitivity and internal**, whatever the
catalogue says. Two rules in the shipped corpus policy turn on exactly those attributes:

```cedar
forbid(principal, action, resource)
when { resource.sensitivity == "critical" && principal.role != "senior" };

permit(principal, action == Action::"email:send", resource)
when { !resource.is_external };
```

The first **never fires**. `payment_api` is `sensitivity: "critical"` in `CORPUS_TOOLS`, and
ADR-057 keeps `principal.role` at the configured `"agent"` — so with a real catalogue every
payment through `payment_api` would be forbidden. Measured directly:

```
empty catalogue (as deployed)   payment:initiate via payment_api -> allowed=True
CORPUS_TOOLS wired              payment:initiate via payment_api -> allowed=False
```

**This is why it must not simply be wired.** Handing `pep_service.py` the corpus catalogue
would refuse every payment in the demo — the beat the whole scenario is built around. The
three facts are in tension and only two can hold at once:

1. `payment_api` is `critical` (`CORPUS_TOOLS`, and 5 corpus cases assert the forbid).
2. `principal.role` is organization-asserted and defaults to `"agent"` (ADR-057, and
   deliberately *not* the token's parent-asserted role).
3. Payments succeed in the demo.

The corpus tests pass because they construct their own `CedarEngine(bundle, tools=TOOLS)`
with a `"senior"` principal where the case needs one. Nothing asserts that the *deployed*
engine sees the same catalogue, which is how the gap survived.

- **Investigate first, and this is the whole ticket:** which of the three gives. Plausible
  answers — the demo mandate's agents genuinely should carry a role that satisfies the
  forbid, and `AGENTIAM_PEP_DEFAULT_ROLE` should be set accordingly in
  `docker-compose.demo.yml`; or `payment_api` is not `critical` and the corpus is wrong; or
  the forbid wants a different predicate. Do not pick by what makes the demo pass.
- **Done when:** the deployed PEP evaluates policy against the same tool catalogue its
  bundle was written for, a test asserts the deployed engine's catalogue is non-empty and
  matches the bundle's, and the demo's payment beat still works *for a stated reason* rather
  than by the attribute being absent.
- **Related:** item 26 mapped `email:send` to `email_internal` and `email_external` so
  `!resource.is_external` has two sides to distinguish. Until this item is closed, both sides
  report `is_external=False`, so that condition is still always true in the deployed PEP.
