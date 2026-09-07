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

### 17. The authority block's own budget ceiling reports `BUDGET_EXHAUSTED_CAVEAT`

Spec 02 §7's table separates the two: a `BudgetCeiling` **caveat** is
`BUDGET_EXHAUSTED_CAVEAT`, and the **authority budget** is `BUDGET_EXHAUSTED_MANDATE`. A root
token that asks for more than its mandate grants is refused by the authority block's own
`check if requested("spend_bdt", $v), $v <= …`, and comes back as `BUDGET_EXHAUSTED_CAVEAT`.

The cause is that `tokens._FACT_REASONS` maps a failed check to a reason code by the fact it
quantifies over — `requested(` → `BUDGET_EXHAUSTED_CAVEAT` — and cannot tell block 0 from
block *n*. `AuthorityFailure` already carries the block number, so distinguishing them is
mechanical.

Observed live in the demo scenario, seq 12: *"root attempts more than the mandate grants"*,
`BUDGET_EXHAUSTED_CAVEAT`, `failing_caveat: null`. The null is correct — it is not a caveat,
which is exactly the point — so the record contradicts itself in a small way: a code naming a
caveat, next to a field correctly saying there wasn't one.

Both codes map to 429, so no client sees a different status. What changes is what an operator
reads: "the token narrowed itself" versus "the mandate never granted this", which have
different fixes.

- **Investigate first:** whether spec 09 §11's reachability table (`§7`) assumes the current
  mapping anywhere, and whether `BUDGET_EXHAUSTED_MANDATE` becoming reachable by a second
  route needs a note there. Item 11's lesson applies — read the spec section that governs a
  reason code before changing it.
- **Done when:** a refusal by the authority block's own budget check reports
  `BUDGET_EXHAUSTED_MANDATE`, a caveat ceiling still reports `BUDGET_EXHAUSTED_CAVEAT`, and a
  test covers both against a real chain.

### 18. The root node in the identity tree has no name of its own

`GET /v1/tree/{task}` reports the depth-0 node as `agt-depth-0`, role `unknown`, beside five
real names. That is *correct* — a root token has no attenuation block, so it declares no
`agent()` and no `role()`, and item 4's fallback deliberately says "not stated" rather than
inventing one. But on screen, next to `agt-doc-reader` and `agt-payer`, it reads like the bug
item 4 just fixed rather than like the absence it is.

The honest name for the root is the *principal* — the human the mandate was issued to
(`kc:…`), which `DecisionRecord.principal_id` already carries. Whether the tree should render
that, or render "the mandate holder" as a distinct node kind, is a console decision.

- **Note:** the PEP must keep doing what it does. Inventing an `agent_id` in `principal_for`
  is what spec 01 §6.1's fallback rule exists to prevent; this is about how the console
  renders a node that honestly has no agent name.
- **Done when:** the depth-0 node is legible as "the principal, acting directly" rather than
  as a missing label, without the PEP asserting an identity the token does not carry.

### 19. Two log-assertion tests go vacuous when every suite runs in one process

`test_compile_nl_to_policy_does_not_log_the_statement_verbatim` and
`TestLimitDetailLogging::test_the_body_reaches_the_log_on_a_retry` fail under
`pytest tests` (everything in one process, integration included) and pass under
`pytest tests/unit`. Reproduced on a clean tree at `1c07c93` as well as on the current one,
so this predates the current work and is not a regression.

**Not a CI risk**, checked rather than assumed: `ci.yml`'s `quality` job runs
`-m "not integration and not e2e and not chaos and not perf"`, and the integration/e2e/chaos
jobs each select a single marker, so the suites never share a process there. This only
appears locally.

**The reason it is worth an item anyway.** The failure is `caplog.records` coming back
**empty** — running one integration module first is enough (`test_oidc_login.py` reproduces
it). The NL-compiler test's first two assertions are

```python
assert statement not in combined
assert "alice@example.com" not in combined
```

and both pass trivially against `""`. Those are the assertions that enforce rule 10 and
NFR-5. The test only fails because a *third* assertion checks that the expected digest line
is present — so the guard survives by luck of having been written with a positive assertion
next to the negative ones. A test whose security claim can silently become vacuous should
say so itself.

- **Investigate:** what empties `caplog` — a handler or `propagate` flag left changed by an
  earlier module is the obvious candidate, and `logging.disable`/`basicConfig` appear nowhere
  in first-party code (grepped), so it is coming from a dependency's import or fixture.
- **Done when:** the two tests pass in a single-process full-suite run, **and** the negative
  assertions cannot pass against empty captured output — assert the record exists first, so a
  future capture failure is a failure rather than a silent pass. Worth grepping for the same
  `assert X not in caplog` shape elsewhere while there.

### 20. `performance.md`'s numbers do not include the block-source parse

`serve_pep.py` is the harness every number in [`benchmarks/performance.md`](benchmarks/performance.md)
comes from, and it hardcodes `agent_id="agt-perf"` and passes no `caveats_for`. The *deployed*
PEP (`pep_service.py`) now does neither: `principal_for` reads the token's identity out of
`Biscuit.block_source()` and `caveats_for` reads its caveats (ADR-057). So the benchmarked PEP
and the deployed PEP no longer do the same work per request, and the published NFR-2 figure
does not include the difference.

**Measured, so the size of the gap is known rather than guessed** (CPython 3.12, this host):

| chain depth | one `token_identity()` / `token_caveats()` |
|---|---|
| 0 (root) | ~125 µs median |
| 1 | ~160 µs |
| 2 | ~195 µs |
| 3 | ~227 µs |

The pipeline resolves the principal **once** per request and reads the caveats once, so a
depth-3 request pays roughly 450 µs, not the ~900 µs three calls would have cost (there is a
test pinning the once-per-request property). Against NFR-2's 8 ms p99 budget that is
comfortable, and the demo stack measures 1.4 ms median / 1.8 ms worst end to end. **NFR-1 is
unaffected** — the parse happens in the pipeline, not inside `decide()`, which is why
`test_the_whole_decision`'s `p99 < 1000 µs` assertion did not move and would not have caught
this either way.

- **Why this was not just fixed here:** `serve_pep.py`'s own docstring says the committed
  numbers depend on it staying exactly as it is, and changing it invalidates
  `performance.md`, `pb2-breakdown.json` and `nfr2-load.json` — which then need a real
  re-measurement run, not a re-render. That is a benchmarking pass, not a side effect.
- **Investigate:** whether the harness should mirror the deployed composition root, or
  whether `performance.md` should report both configurations and say which one a reader
  should believe for a production deployment.
- **Done when:** the published NFR-2 number reflects the work a deployed PEP actually does,
  or the document states plainly that it does not and by how much.
