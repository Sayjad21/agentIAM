# Development Journal

What each ticket did, what turned up while doing it, and what changed as a result.

`DECISIONS.md` holds the formal ADRs. This is the narrative around them — including the
findings that did *not* need an ADR, and the mistakes worth remembering.

---

## The one habit that shaped everything

Before writing any spec, I checked its claims against a running system.

That sounds like overhead for a documentation ticket. It was the opposite. From T-002 through
T-014 it found **nine design errors**, every one of which would otherwise have surfaced later as
a failing property test — or worse, as a *passing* one written from the same wrong premise. It
also turned up a security finding no amount of brainstorming was going to produce (TM-24).

Twice the thing measured was a *specification of ours* rather than a library: spec 04's `ACQUIRE`
formula cannot pass its own ticket's acceptance test, and spec 04's `LEDGER_COMMIT` statement
order is a TOCTOU race. Writing a spec first is what makes the project defensible; it does not
make the spec right.

The pattern each time was identical: the design was defensible on paper, and wrong against the
library. Reading harder would not have caught any of them.

| Where | What the paper design said | What measurement showed |
|---|---|---|
| Spec 01 | Caveats check the token's own scope facts | Checks are existential; the narrowing did nothing |
| Spec 01 | Depth comes from a `depth(n)` fact | A depth-9 chain authorized successfully |
| Spec 01 | Budget ceilings can be strings | Datalog cannot compare strings numerically |
| Spec 02 | One clause form fits all caveats | Half fail closed, half fail **open** |
| Spec 04 | The seven lease operations are complete | `leased` went negative in 55 of 400 interleavings |
| Spec 03 | `TimeWindow` narrowing is interval containment | Refuses the most common attenuation there is |
| SDK | `copy_context()` carries identity into a thread pool | Two threads entering one `Context` raise `RuntimeError` |
| SDK | Escaping a `role` correctly is enough | `block_source()` renders it back **unescaped** (TM-24) |
| Spec 04 | `ACQUIRE` clamps the grant by `max_fraction` | All 50 concurrent callers get a grant; `available` never reaches 0 |
| Spec 04 | Dedup check, then lock the lease | TOCTOU: concurrent duplicates both pass the check, crash on the PK |

---

## M0 — Repository bootstrap

**Commit:** `b46f8cc`

The working directory held three planning documents and no code. Two things in them conflicted
with how the project would actually be built, and both had to be resolved before the first line
of code rather than worked around afterwards.

**There was no team.** The orchestration guide was built around three parallel role tracks. Its
milestone ordering was not merely redundant for one person — it was actively wrong. It put
Docker's full eight-service stack, Keycloak, Ollama and Grafana in M1, because a dedicated
infrastructure owner had nothing else to do. Solo, that is one to two weeks of environment
debugging before any protocol code exists, and it de-risks nothing: the real risk lives in the
attenuation algebra and the lease protocol, neither of which needs more than a database.

Inverted it (**ADR-001**): `docker-compose.yml` grows one service at a time, each added by the
first ticket that needs it. M1 is Postgres and Redis. Ollama and Keycloak arrive in M5, the
observability stack in M6. The correctness core became reachable in days instead of weeks.

**The repository had to carry no AI attribution.** The plan literally instructed creating a
`CLAUDE.md` at the root and included an appendix of agent prompt templates. AgentIAM's IP claim
is *100% own development, all core code original* (§14.4, NFR-10). The rules in that appendix
were good engineering discipline regardless of who types, so they became
`ENGINEERING-RULES.md`, written as project standards — which is what they are.

---

## M1 — Foundation

### T-001 · Scaffold, tooling, CI

**Commit:** `4a0367f` · uv workspace, five package stubs, ruff + mypy --strict + pytest,
pre-commit, GitHub Actions, Makefile, compose with Postgres 16 and Redis 7.

**The purity guard is the substantive piece.** `agentiam-core` must perform no I/O — every
correctness claim the project makes is a claim about that package, and the claims only mean
something if it is deterministic and testable in isolation.

The obvious implementation is to import the package and check what it pulled in. That only sees
module-level imports. A violation would realistically appear as a lazy `import httpx` inside a
function body, which an import check never sees. So the guard is a **static AST walk** over
every source file, catching lazy, conditional, aliased and in-class-body imports alike. It also
catches wall-clock reads, since the clock is injected.

A guard that has never fired is not a guard. It has detector self-tests over nine violation
forms and four pure-code forms — the last matter because a check that flags *correct* code gets
disabled. Importing `datetime` for annotations passes; calling `.now()` fails. Then I verified
it end to end by dropping a file importing httpx, redis and sqlalchemy and calling
`datetime.now()` into the package: both tests failed, and passed again on removal.

CI runs it as a separate job so a violation appears in the checks list rather than buried in a
test log.

**Two environment decisions.** `make` does not exist on Windows, so the Makefile stays
authoritative (CI runs it on Linux) and `make.ps1` mirrors its targets (**ADR-003**). And Ruff
0.16 formats Python inside Markdown — it wanted to rewrite the `Budget` and `DecisionRecord`
blocks in `PLAN.md`. Excluded `*.md` (**ADR-004**): those blocks are specification prose, never
compiled, and a formatter overruling a spec is the tool winning an argument it should not be in.

Cold start measured 69s with image pulls, 6.3s warm, 16s on CI — against a 90s budget.

---

### T-002 · Token format spec

**Commit:** `872f375` · [`specs/01-token-format.md`](specs/01-token-format.md)

The acceptance criteria asked for a worked 3-level chain **with byte counts**. Rather than
derive them, I built the chain against `biscuit-python` and measured. That turned a
documentation ticket into the most productive one of M1.

#### Finding 1 — biscuit checks are existential

This is the single most important thing learned in the project.

`check if scope($s), ["invoice:read"].contains($s)` asks *"does there exist a `scope` fact in
this list?"* — **not** *"is the scope being requested in this list?"*. Written against the
token's own grant facts, it passes as soon as **any** granted scope matches.

Measured: a token granting `invoice:read` and `vendor:read`, narrowed by a caveat naming only
`invoice:read`, still authorized `vendor:read`. The narrowing did nothing at all.

Implementing `PLAN.md` §6.1 literally would have produced a token that *appears* to enforce
restrictions and does not. **ADR-005**: token blocks carry only the grant; the verifier supplies
the request context; every check constrains those facts.

#### Finding 2 — a block's own `depth` fact is attacker-controlled

Combined with existential semantics, `check if depth($d), $d <= 8` is satisfied by the authority
block's own `depth(0)` regardless of how deep the chain runs. Measured: a depth-9 chain
authorized successfully.

Depth now comes from `block_count - 1`, computed by the verifier. A `declared_depth` fact
survives for audit and console rendering, explicitly marked as not usable for authorization.

#### Finding 3 — a string budget ceiling cannot be compared

§6.1 illustrated `budget("spend_bdt", "500000")`. Datalog cannot compare strings numerically, so
the token could not enforce its own budget caveat offline — which is the only reason to carry
it. Money became integers scaled by 10⁴, matching `NUMERIC(20,4)`, with every dimension on the
same scale so one comparison rule covers all of them.

#### Finding 4 — a check whose fact is absent *fails*

Found while measuring, not while designing. The L1 and L2 tokens denied everything until the
request context supplied all five budget dimensions. A `check if requested("tool_calls", …)`
with no matching fact denies outright.

So the verifier must emit a `requested()` fact for **every** dimension on every call, defaulting
to zero. Omitting one is not "no constraint" — it is a denial. That rule later became a
constructor-level validator on `RequestContext`, so the mistake cannot be made quietly.

#### The measurements

| Depth | Base64 chars |
|---|---|
| 0 (root) | 1,404 |
| 2 | 2,472 |
| 6 | 4,120 — crosses the warning threshold |
| 8 (`max_depth`) | 4,940 |

Growth is ~410 base64 bytes per attenuation block. At the *maximum permitted depth* a token
reaches 60% of the 8 KB hard limit, so the reference-handle path (T-010) cannot trigger within
the allowed range — a stronger reason to defer it than the original guess (**ADR-006**). Two
live consequences remain: the 4 KB warning fires at depth 6 so EC-T11 stays testable, and the
`Authorization` header nears nginx's 4 KB default line size around depth 5, which T-018 must
raise deliberately rather than discover.

---

### T-003 · Caveat language and attenuation semantics

**Commit:** `f26e10c` · [`specs/02`](specs/02-caveat-language.md),
[`specs/03`](specs/03-attenuation.md)

#### Finding 5 — the two clause forms fail in opposite directions

Biscuit offers `check if` and `reject if`, and they behave oppositely when the fact they
constrain is absent: `check if` **denies**, `reject if` **passes**.

Picking one form for all caveats breaks half of them, in opposite directions:

- As `check if`, an `ArgPredicate` on `payment.amount` denies an `invoice:read` call that
  carries no such argument. Measured.
- As `reject if`, a `TimeWindow` is vacuous whenever the verifier omits `time()` — **the token
  never expires**.

The second fails *open*, which is the worse one and the harder one to notice. **ADR-007**: the
form follows the fact, not the caveat. `ToolAllow` is the deliberate exception — it constrains
an optional fact but uses `check if`, because a call with no tool identity must not satisfy an
allow-list.

#### Finding 6 — `RequiresApproval` cannot be a Datalog clause

Its outcome is `escalate`, and a biscuit authorizer only answers allow or deny. Encoding it as a
check turns every escalation into a silent denial, contradicting §6.6 and demo Beat 6.

It compiles to a **fact**, read via `block_source()` and evaluated in Python (**ADR-008**). I
verified this is not a weaker guarantee: `authorizer.query` returns authority-block facts but
**nothing** for block facts, so block facts are inert to the Datalog engine in both directions.
That is the same scoping which stops fact injection from widening authority.

The cost is that core's evaluator must agree with the Datalog compilation on allow/deny for
every input — a security property, since the PEP trusts Datalog for the decision and the
evaluator for the explanation.

#### The counterexamples

Spec 03 states all ten invariants formally and gives **14 counterexamples** across INV-1, INV-5
and INV-9 — written as the shapes T-009's generators must be able to produce, since a strategy
that cannot reach them passes vacuously. They were the specification for the strategy audit six
tickets later.

#### The distinction worth being able to state

Spec 03 §2 separates three enforcement layers that are easy to conflate:

| Layer | Enforces | If it breaks |
|---|---|---|
| Biscuit's append-only structure | INV-1, 2, 3, 4, 8 | Catastrophic |
| `narrows()` at mint | EC-T17/18/19 | Confusing tokens, not insecure ones |
| The ledger | INV-5 | Overspend across siblings |

**The security boundary is the first, not `narrows()`.** A `narrows()` bug produces a misleading
token, not an over-privileged one. Worth being able to say plainly when a judge asks.

---

### T-004 · Lease protocol spec

**Commit:** `7a9e9a3` · [`specs/04-lease-protocol.md`](specs/04-lease-protocol.md)

The acceptance criteria asked for a written safety argument. Rather than assert one, I built a
model of the protocol, ran random interleavings, then **removed each guard to check it was
load-bearing**. A guard whose removal changes nothing is not protecting anything.

Two of the five turned out to protect something other than their name suggests.

#### Finding 7 — a gap in the §6.4 pseudocode

`RELEASE`, `REAP` and `REVOKE` each return a lease's full outstanding amount to the pool. The
pseudocode never says what happens when a `LEDGER_COMMIT` for that lease arrives *afterwards* —
a crashed PEP's buffered batch, or a partitioned PEP reconnecting. Applied normally it
decrements `leased` a second time for budget already returned.

Measured: **`leased` went negative in 55 of 400 random interleavings.** Not a corner case — any
`REAP` racing an in-flight commit reaches it, which is precisely the crash scenario the protocol
exists to survive.

**ADR-009**: such commits are rejected and recorded as reconciliation anomalies. The pool
invariant is preserved by construction and the divergence is surfaced loudly rather than
silently corrupting the ledger.

#### Finding 8 — idempotency does not protect what the plan implies

§6.4 groups idempotency with the safety properties. But a replayed commit does
`committed += a; leased -= a`, which conserves `committed + leased` exactly. Measured: 400
interleavings with replay on and idempotency off produced **zero** safety violations.

What it protects is the books. One real spend of ৳30 delivered three times:

| | `committed` | `outstanding` |
|---|---|---|
| idempotent | 30 | 70 |
| not | **90** | **10** |

The mandate looks exhausted early, the PEP loses budget it never used, and the audit ledger
records spend that did not happen.

**This has a concrete consequence: P-12 written the natural way — against `Σ committed ≤ total`
— would pass while the books were threefold wrong.** It must assert accounting equality
instead (**ADR-010**).

#### Two things the plan did not state

**Safety under crash and partition depends on bounded clock skew.** If the reaper reclaims while
a lagging PEP still spends, the same budget exists twice. Measured with no margin: the lease was
reaped and re-issued while still in use. PEPs now expire early at `expires_at − S`, the reaper
reclaims late at `expires_at + S`, and `ttl` must exceed `2S`.

**The ledger clamps the PEP's reported `actual`** to the lease's outstanding, so safety does not
depend on PEP correctness. That matters because the PEP is the component most exposed to a
compromised agent.

---

### T-005 · Domain models

**Commit:** `7122cf0` · `models.py`, `errors.py`, `hashing.py` — first implementation code.

Three decisions worth recording:

**Money rejects `float` rather than coercing it.** Pydantic would take `500000.0` happily, and by
the time the imprecision matters it is three services downstream in a ledger that no longer
balances. `int` and `str` are accepted — both are exact.

**`RequestContext` validates that every budget dimension is present**, turning finding 4 into a
constructor error at the call site rather than a confusing denial far from its cause.

**`DecisionRecord` cross-validates outcome against reason code** — an allow must carry `OK`,
anything else must not. That is P-18 enforced by the type, not only by a test.

#### The self-inflicted one

A PowerShell round-trip of mine read two files as ANSI and wrote them back as UTF-8, replacing
each multi-byte character with one confusable character per byte — 232 markers in spec 01, and a
*doubly*-encoded layer in a test module. I repaired it by peeling the encoding layers byte by
byte rather than hand-editing the visible characters, which would have left the underlying bytes
wrong.

Since this project must carry Bengali company names and ৳ amounts through the whole pipeline
(EC-T16), and the corruption still parses and still passes every other test, the scan is now a
test — verified to fire on a planted violation. A genuine hazard for this repository, not just
cleanup after a mistake.

#### A counting error I introduced

Spec 02 said "eight caveat types" over nine subsections. `PLAN.md` line 1010 resolves it: the
eight are the types compiling to a Datalog *clause*, which excludes `RequiresApproval` since
ADR-008 made it a fact. Nine types, eight clauses. Also, spec 01's illustrative intent hash was
65 characters — not a valid SHA-256 — and is now the real digest of the demo task description,
which makes the worked example reproducible.

---

### T-006 · Threat model

**Commit:** `c565367` · [`threat-model.md`](threat-model.md)

23 threats against a requirement of 12. **15 mitigated, 5 partial, 3 accepted risks**, every
partial and accepted one stated with its bound.

Four threats (TM-19…TM-22) are not in `PLAN.md` §12's catalogue. They came out of the
measurement work in T-002…T-005 rather than from brainstorming — a caveat that appears to
enforce and does not, a clause form that fails open, the late-commit double-decrement, and clock
skew past the allowance. All four are mitigated in the specs but have **no test yet**; §6
records them as concrete additions to T-051 rather than leaving the gap implicit.

§4 lists the six assumptions the security actually rests on. **A1 is load-bearing**: biscuit
scopes block facts so a later block's facts are invisible to earlier blocks' checks. If that
stops holding, INV-1 collapses and any child could widen authority by adding a fact. It must be
re-verified on every `biscuit-python` upgrade.

---

## M2 — Token layer

### T-007 · Biscuit mint and verify

**Commit:** `a073b3e` · `tokens.py`

**`verify()` is deliberately not authorization.** It answers whether the token is authentic,
in-window and structurally sound; whether a *particular call* is permitted needs request context
and belongs to T-019. That split is what makes precise reason codes possible — biscuit reports
only *that* authorization failed, never which clause, so distinguishing `TOKEN_EXPIRED` from
`TOKEN_NOT_YET_VALID` has to happen against facts read back from the authority block.

Which required a small spec change: `not_before`/`expires_at` were checks but not *facts*, so a
verifier could not read the window back. Now both — facts for reading, checks for enforcing.

**Resolved spec 01's open question 1.** Biscuit supports a strict `<` on date terms, so the
exclusive expiry boundary is enforced by the token itself; the Python pre-check the spec had
hedged toward is unnecessary. Measured across the boundary: one second before expiry authorizes,
*exactly* `expires_at` does not, one second after does not.

Also measured: **minting is not byte-deterministic** — biscuit embeds a fresh next-block key. A
test asserts it, because assuming otherwise is reasonable and wrong, and a future test asserting
byte equality would fail mysteriously.

The malformed-authority-block tests mint biscuits *directly* with the root key and omit or
corrupt each mandatory fact — the case where a token carries a valid signature but was not
produced by our code, from a leaked key or version skew. Verification must reject it as
`MALFORMED_REQUEST` rather than raise `KeyError` or hand back a half-populated `VerifiedToken`.

---

### T-008 · Caveat DSL → Datalog

**Commit:** `1bfa35a` · `caveats.py`

The conformance suite is the point of this ticket. ADR-008 made the agreement between
`to_datalog()` and `evaluate()` a security property, so asserting it by re-implementing the
comparison twice in Python would prove nothing. Each of the 60 table cases is compiled into a
**real biscuit attenuation block**, authorized against a real request context, and the
authorizer's verdict compared to `evaluate()`.

22 Datalog forms confirmed before writing, including the two that were open: the empty set
literal `[]` parses and denies everything (so EC-T14 needs no special case), and quote/backslash
escaping holds inside set literals.

**A layout question resolved.** Spec 02 described a `Caveat` Protocol with methods, but
`PLAN.md` §5 puts models in `models.py` and DSL translation in `caveats.py`. Kept the plan's
split and made the operations module-level functions, with exhaustiveness enforced by structural
pattern matching plus `assert_never` — the same guarantee a Protocol gives, without behaviour on
the data types.

**One test worth calling out.** Scope strings are pattern-validated so they cannot carry a
quote, but `ArgPredicate.path` is free text and can carry attacker-influenced content. The
hostile-path test tries to close the Datalog literal and inject a policy
(`n"); allow if true; //`), then verifies escaping held by compiling into a real biscuit block
and checking the caveat still binds to its own path. That is A-31 at the Datalog layer.

---

### T-009 · Attenuation and the narrows() algebra

**Commit:** `ff7bb5e` · `attenuation.py` — flagged in the roadmap as the most important ticket
in the project.

The properties are written against real tokens: every authority comparison mints a biscuit,
appends real blocks, and runs a real authorizer. Comparing two Python functions would only prove
they agree with each other, not that what biscuit enforces is narrowing.

#### Finding 9 — `TimeWindow` narrowing, found by INV-9

Spec 03 defined it as interval containment. Read literally, with an absent bound meaning
unbounded, a child adding only `not_after` looks wider on the lower side and is refused — and
that is *the most common attenuation there is*.

It is wrong because a `TimeWindow` compiles to one clause per bound. A child supplying only
`not_after` asserts nothing about the lower bound, and the ancestor's clause still applies.

The obvious repair — treating an absent bound as "inherited" inside `narrows()` — breaks
antisymmetry: an upper-only and a lower-only window become mutually narrowing without being
equal, so the algebra stops being a partial order, which this ticket explicitly requires.

**ADR-011**: a two-sided window decomposes into two atoms, one per side, each its own
comparability slot. **Any future caveat compiling to more than one clause needs the same
treatment.** Nothing in the spec text looked wrong; only the property test found it.

#### The strategy audit

The roadmap's note on this ticket was: *check the property-test strategies, not just that the
tests pass — a weak strategy passes vacuously.* So
[`tests/property/test_strategies.py`](../tests/property/test_strategies.py) is sixteen audits
asserting the generators actually reach the shapes spec 03 §6 requires: all nine kinds, every
arg operator, empty scope sets, zero ceilings, all three window shapes, Bengali text, and —
most importantly — that **strictly-narrowing pairs occur in both directions** for `ToolDeny` and
`RequiresApproval`.

Those two are where the order runs backwards. A generator that never produced a strict subset
for them would let an implementation with the comparison inverted pass everything.

The audits earned their place immediately: one caught that `gt` was never generated when
sampling the union, because ArgPredicates are only a ninth of the draws.

#### Three test bugs of my own

The properties surfaced them: two tests built chains past `max_depth` and then blamed `verify()`
for rejecting them, and the operator audit sampled the wrong strategy. Worth recording because
the first instinct on a red property test is to suspect the implementation, and twice out of
four times here it was the test.

#### One flake, chased down

A single failure appeared once during development and did not reproduce. It was a stale
falsifying example replayed from `.hypothesis` against code that had just changed. Cleared the
database and confirmed clean across three fresh full runs with zero examples recorded. Recorded
here rather than left as an unexplained one-off.

### T-011 · SDK: identity propagation and holder-side attenuate

The last M2 ticket, and the first code outside `agentiam-core`. Three deliverables: a
`contextvars`-based identity that follows an agent across tasks, an `attenuate()` an agent can
actually call, and a `@requires_scope` decorator.

#### The probe came first, and changed the design

Five questions asked of the interpreter before any of it was written:

| Question | Answer |
|---|---|
| Does a task see its parent's identity? | Yes — copied at **creation**, not at first await |
| Does a child task's change leak to its parent or siblings? | No. This is where isolation actually comes from |
| Does `await coro()` without a task wrapper? | **Yes.** A bare `set()` inside rewrites the caller |
| Does `loop.run_in_executor` propagate? | **No** |
| Does `asyncio.to_thread`? | **Yes** |

Two of those changed what got built. The bare-coroutine leak is why the module exposes no
unpaired setter at all — `use_identity` is a scope that always resets, because between two agents
holding different tokens an unpaired `set` is a privilege escalation with no attacker in it. And
the split between `to_thread` and `run_in_executor` means the thread boundary is narrower than
"threads": worth documenting precisely rather than overstating.

#### The obvious thread-propagation fix crashes under concurrency

The idiomatic way to carry context into a worker thread is `contextvars.copy_context()`, bind the
snapshot to the callable, call `Context.run` in the worker. Measured before adopting it: entering
**one `Context` object from two threads at once raises `RuntimeError: cannot enter context ... is
already entered`**. Sequential reuse is fine. Concurrent reuse — which is the ordinary way to use
a pool — is not.

That failure is timing-dependent. It would have passed a casual test and failed under load, in
the SDK, in someone else's application. `bind_identity()` captures the identity *value* instead
and re-installs it per invocation, so there is no shared object to enter twice (ADR-012). The
rejected design is exercised directly in a test, with a `threading.Barrier` forcing the overlap,
so the guard is demonstrated rather than asserted.

#### What the SDK is actually for

Core's `attenuate()` checks a proposed caveat against the *authority block's* grant, because that
is all a `VerifiedToken` can recover. Enough to stop a child adding a scope the mandate never
carried. Not enough to stop a **grandchild re-widening back to a scope its parent gave up** — the
authority block still lists it.

The SDK minted those intermediate caveats, so it is the one component that still knows them. It
passes them down as `ancestor_caveats`, and the re-widening is refused. The test that matters
sits next to one that calls core directly on the same token and shows it accepts — the guard
proved load-bearing rather than assumed.

Worth being precise about the severity: the widened token could never have *exercised* the
recovered scope. Biscuit's block scoping (assumption A1) prevents that. What it would have
carried is a caveat that lies about its own authority, and a console that renders the lie.

#### A rendering hazard, found by asking one question of the library

`role` and `agent_id` become Datalog string facts. Both specs had left the same question open for
this ticket — *should `role` be a closed enum for console rendering?* Probing the rendering path
to answer it turned up something better.

`quote_string()` escapes correctly. A crafted role cannot forge a fact inside a signed token.
But `block_source()` renders the string back **unescaped**: a role of `x"); admin(true); //`
renders as block text that re-parses into a genuine second `admin(true)` fact. The same path
exists in the authority block through `principal_id`, which the issuance service will populate
from Keycloak claims.

Every planned consumer of block source is a display or re-parsing path — the console's caveat
chain, the audit explorer, the Datalog-to-caveat parser both need. Escaping correctly in each is
three chances to get it wrong.

So: not an enum (that would make adding a role a protocol change, and would have hidden this
rather than fixed it). A banned-character class instead, applied in core where minting happens:
quotes and backslashes break the round trip, C0/C1 controls break the line structure, and bidi
overrides reorder rendered text without changing a byte. Everything else passes, so a Bengali
role name renders as itself — worth keeping for a Bangladesh submission. That is ADR-013 and
threat TM-24, and it gave `tests/security/` its first occupant.

It is a display bug, not an escalation, and the tests say so explicitly. A threat model that
oversells its findings is worth less than one that bounds them.

#### One test bug, again

The depth chain delegated by denying `tool.N` at each level and `attenuate()` refused it.
`ToolDeny` narrows by **superset** — dropping a denial is widening — so each level has to deny
everything its parent did, plus one. The algebra was right and the test was wrong, which is now
three tickets running.

---

## M3 — Ledger and leases

Where the budget stops being a number in a token and becomes a row that two processes can fight
over. Everything before this was provable by reading; nothing here is.

### T-012 · Budget schema and migrations

`budgets`, one Alembic migration, and the pool invariant `committed + leased <= total` as a
database `CHECK` rather than application logic. Putting it in the schema means the invariant
holds against a buggy migration, a manual `psql` session, and any future service that writes the
table without going through the ledger code.

Two scoping questions had to be answered before the migration could be written, and neither was
answerable from `PLAN.md` §7's data-model block alone (ADR-014).

**Only `budgets` ships.** §7 lists `budgets`, `leases` and `reservations` together, but every
test that would give the other two meaning is assigned to T-013 and T-014 by spec 04 §16.
Shipping empty tables would mean guessing at columns a later ticket might reshape, for no test
this ticket can write.

**`mandate_id` gets no foreign key.** There is no `mandates` table anywhere in the repository —
T-005 built `Mandate` as a pure Pydantic model, and no ticket in `PLAN.md` §9 yet owns persisting
one. Inventing a schema for it here, ahead of the ticket that owns it, risks a second migration
to reshape it later. Recorded as gap 7 rather than left implicit: a budget row can name a mandate
that was never issued, and nothing catches it. The pool invariant does not depend on the FK — it
is a property of one row, not a join.

### T-013 · ACQUIRE, RELEASE, REAP

The three ledger-side operations, each inside `SELECT ... FOR UPDATE`.

#### A spec formula that cannot pass its own ticket's test

Spec 04 §4.1 computes `grant := min(requested, available, max_fraction × available)`.
`PLAN.md` §6.4 has no third term. Spec supersedes plan on protocol detail, so the clamp was
implemented first — and then measured against T-013's own acceptance bar.

With `max_fraction = 0.25` and 50 concurrent callers each requesting 1 against a total of 10, the
clamp binds once `available < 4`, and past that point every grant is a quarter of what is left.
`available` shrinks by a factor of 0.75 per call and **never reaches zero**; it only lands on
`0.0000` when `NUMERIC(20,4)` rounding forces it, around the 40th call. All 50 callers get a
nonzero grant. `PLAN.md` §9's stated bar — *exactly 10 succeed, 40 get Insufficient* — is
unreachable with the clamp applied to a caller-supplied amount, for any numbers large enough to
make the test meaningful.

Not a rounding corner. The clamp's ordinary behaviour on the exact scenario the ticket specifies.

The resolution was to read spec 04 §12 back: `max_fraction` belongs to *adaptive lease sizing* —
it bounds a size the ledger itself computes, not a second cap on an amount a caller explicitly
asked for. §4.1 folding it into `ACQUIRE` conflates the two. So `ACQUIRE` uses `PLAN.md`'s
simpler form, and `max_fraction`'s real job — bounding what one PEP can strand on a crash — is
unenforced until T-015 (ADR-015, and gap 8). Rule 9 says never weaken a test to make it pass: the
code was changed to match the plan, and the incompatibility written down.

#### The guards, removed one at a time

Both the `FOR UPDATE` serialization and the clock-skew margin were verified by deleting them and
watching the tests fail. A guard whose removal changes nothing is not protecting anything, and
this is the third ticket where that check earned its keep.

### T-014 · RESERVE, COMMIT, LEDGER_COMMIT

#### Where the PEP-side operations live

Spec 04 §4.2 and §4.3 describe `RESERVE` and `COMMIT` as PEP-side: no network, no ledger
mutation, no lock, touching only in-memory state. So they went into a new `agentiam_pep.lease` —
not `db/ledger.py` beside the `AsyncSession`-taking functions, where a future edit would reach
for a session that should not exist, and not `agentiam_core`, which holds immutable value types
rather than mutable per-process runtime state (ADR-016).

`reconciliation_anomalies` is a table with no entry in `PLAN.md` §7 at all. It exists because
spec 04 §11's late-commit rule — itself found by model-checking the plan's original pseudocode in
T-004, not present in it — requires the rejection to be recorded, and nothing else in the schema
does. TM-21, the threat that model-check produced, is closed here.

#### A TOCTOU race in the spec's own statement order

Spec 04 §4.4 writes `LEDGER_COMMIT` as: check whether the reservation is already settled, *then*
`SELECT lease FOR UPDATE`. Implemented literally, the dedup check runs against an unlocked read,
so two concurrent commits carrying the same `reservation_id` — a retried batch racing itself,
exactly what the guard exists for — can both see "not settled" before either takes the row lock.

**Measured: reproduced on all three attempts.** Ten concurrent `ledger_commit()` calls, same
lease and reservation id. It is not a silent double-apply — the `reservations` primary key catches
the second insert — but it surfaces as an unhandled `UniqueViolationError` escaping the function
instead of the clean idempotent `False` the caller is promised. A batched-commit worker retrying
in a loop would crash rather than degrade.

Taking the lock first fixes it: every concurrent commit against the same lease now serializes
before either re-reads `reservations`. The external contract is unchanged; only the order of two
reads moved, so this is a deviation from the pseudocode's statement order, not from the protocol
it describes (ADR-017). The ADR exists mainly to stop a future edit "correcting" the code back to
the literal spec order and silently reintroducing the crash — and it was verified the way this
repo verifies things: literal order restored, test failed with the exact error, lock-first
restored, suite green.

### Found while integrating M3: none of it ran in CI

Picking the work back up, the whole suite passed — 788 tests. It also **deselected 45**, which is
every integration test in the repository, because `make test` and the CI workflow both exclude
the `integration` marker and nothing else ran them.

So the 50-concurrent-acquire test, the `FOR UPDATE` serialization proof, and the `LEDGER_COMMIT`
dedup race that ADR-017 exists to prevent a future edit from reintroducing were running only when
somebody remembered to run them by hand, on a machine with Docker. Three tickets of ledger
correctness, gated by nothing.

That is worse than an untested guard, because the tests exist and look like coverage. A race test
that runs only on request will rot silently, and the first sign of it will be a production
double-spend rather than a red check.

CI now has a fourth job that runs them. No compose services are needed — the fixtures use
testcontainers, which starts its own `postgres:16-alpine` against the runner's Docker daemon.

Two smaller things fell out of the same pass. `.\make.ps1 help` had been printing one character
per line since T-001, because PowerShell flattens nested `@(...)` literals and `$_[0]` was
indexing a string rather than a pair. And testcontainers cannot start at all on Docker Desktop
for Windows — its Ryuk reaper sidecar never gets a published port — which is why
`test-integration` sets `TESTCONTAINERS_RYUK_DISABLED` on the Windows shim only. Linux CI does not
need it, so it stays out of the authoritative Makefile.

---

### T-016 · The invariant checker

The tool that runs on screen during Beat 4 while three sub-agents spend concurrently against a
ceiling a judge just set. `ROADMAP.md` states the bar without flinching: *a checker never tested
against a real violation is decoration*.

#### Is it doing anything the database isn't?

`PLAN.md` names two invariants, and the first — `committed + leased <= total` — has been a
database `CHECK` since T-012. Re-asserting in Python what the schema enforces is usually waste,
so the first thing was to find out whether the checker had a job at all.

| Injection | Result |
|---|---|
| `UPDATE budgets SET committed = total + 1` | **Refused** — `IntegrityError` |
| `UPDATE budgets SET committed = committed + 10` | **Accepted.** `Σ reservations` still 40, `committed` now 50 |

There is the job. A `CHECK` compares three columns of one row; it cannot see a sum across
`reservations` and `leases`. The books invariants have no schema backing whatsoever, and the
second `UPDATE` is what a double-applied commit or a hand-repaired row actually looks like on a
bad night. ADR-010 said it a milestone earlier — idempotency protects the books, not the pool —
and it turned out nothing at all was protecting the books.

So the checker asserts four things rather than two. The third is not in `PLAN.md`: `leased` must
equal the outstanding total of *active* leases, derived by reading every write path in
`ledger.py`. It is the invariant a missed decrement in `_retire` breaks, which is the ADR-009 and
TM-21 failure shape. The fourth separates a negative `leased` from a generic pool overflow,
because negative `leased` is the specific thing model-checking produced back in T-004 and it
deserves its own name in the alert.

#### The failure mode that isn't "misses a violation"

Everything is read in **one** SQL statement, and that is a correctness requirement rather than a
performance one. The three quantities are compared against each other, so they have to come from
one snapshot. Read them in three statements and an `ACQUIRE` landing between two of them reports
a violation that never existed — and a checker that cries wolf gets muted, which ends in exactly
the same place as a checker that never fires. The integration suite sweeps 25 times against four
concurrent workers to keep that honest.

Measured: 3–5 ms for a sweep over 500 budgets, against an acceptance bar of *detect within one
second*. About 200× headroom.

#### Found by running it, not by testing it

The loop swallows errors so a chaos run can continue through CH-1 (Postgres down) — and swallowed
only `SQLAlchemyError`. Running the CLI as a real process against a dead port showed a bare
`ConnectionRefusedError` escaping and killing it: the failure happens below the dialect, so
SQLAlchemy never wraps it. The script crashed on precisely the condition its own docstring
claimed it survived.

Worth dwelling on, because the unit tests were green. They tested the rendering, the exit codes,
and the sweep against a real ledger; none of them started the process and pointed it at nothing.
Three tests now do, and removing `OSError` from the except clause fails all three.

The same run also surfaced that the script had invented `AGENTIAM_DATABASE_URL` while Alembic
and `.env.example` use `DATABASE_URL` — a second name for one thing, found by trying to use it
rather than by reading it.

### T-017 · Sibling budgets, and the end of M3

INV-5 is the one invariant the token format deliberately does not carry, and the reason the
ledger exists at all. A parent hands the same ৳50,000 ceiling to three children. Every token is
individually valid and correctly authorized. Together they can spend ৳150,000, and no amount of
static inspection prevents it, because nothing about any single token is wrong.

`PLAN.md` §6.3 calls the two-mitigation answer a publishable observation — most capability-token
literature does not address quantitative resources shared across siblings. Both mitigations ship
here.

#### The dynamic half already worked

Probed before writing anything: three separate PEP instances, each with its own engine, each
asking for ৳100 against a pool of ৳150. Grants came back `50 / 100 / 0`, summing to exactly 150.
`SELECT ... FOR UPDATE` was already doing the job T-013 built it for.

Which produced the first finding, and it is about the spec rather than the code. Spec 04 §13 and
§15 both recorded that outcome as **"granted 100 / 50 / 0"**. The probe returned `50 / 100 / 0`.
Neither is wrong — which caller gets the full amount depends on which transaction takes the row
lock first — but stated as a sequence it reads like a guarantee, and a test written faithfully
from the spec would have asserted the scheduler and flaked in CI at some unhelpful hour. The spec
now states what is actually guaranteed: the grants sum to exactly what the pool had.

#### The static half needed a schema change

"Each child gets its own budget row" turns out not to be additive. Measured: a second row for the
same `(mandate_id, dimension)` is refused outright by `uq_budgets_mandate_dimension`. So T-017
changes a constraint T-012 established — the pool uniqueness becomes **partial**, scoped to rows
with no parent, and a second constraint keeps one allocation per child.

The design question worth recording is where the allocated money lives. Budget promised to a
child is unavailable to the parent, exactly as leased budget is, so the tempting move is to
increment `leased` and add no column at all.

That is wrong, and wrong in a way that only surfaces a ticket later. T-016's checker asserts
`leased == Σ outstanding of active leases`. Overloading `leased` with allocations breaks that on
the very first split — and the natural next step would be to relax the assertion, which is rule 9
running backwards. Two meanings, two columns: `allocated` joins the pool invariant as its own
term, and the checker gains a fourth thing to verify (ADR-019).

#### Two bugs found by the test fixtures, not by the tests

Neither was in the code under test, and both were mine.

**The downgrade could not delete what it had created.** `DELETE FROM budgets WHERE
parent_budget_id IS NOT NULL` fails on `leases_budget_id_fkey` the moment a split has been spent
against. It surfaced as a teardown error, because the integration fixture actually runs
`downgrade` after every test — a detail that earns its keep here. The deletes now run in
foreign-key order, and the docstring says plainly that going below this revision destroys real
spend records, because it does and there is no version that does not.

**A test that corrupts the schema has to put it back.** T-016's tests drop `ck_budgets_invariant`
to inject a pool violation the constraint would otherwise refuse, and left the rows dirty,
relying on teardown dropping the table. That was fine until 0004's downgrade started *re-creating*
that constraint on the way down — at which point Postgres correctly refused to add a constraint
some existing row violated, and five tests failed with eight cascading teardown errors, none of
them anywhere near the code being changed.

The migration was right and the tests were wrong. They now go through a fixture that repairs the
rows and restores the constraint in a `finally`.

That is the third time in this project a red result turned out to be the test rather than the
implementation. It is worth having a rule about by now: read the failure before touching the
code, and check whether the thing that broke is the thing under test.

#### M3 exit gate

All lease operations working · P-10 green · the invariant checker proven against a real violation
· the three-sibling test passing under both mitigations, with three PEP instances. 932 tests.

---

## M4 — PEP and the first end-to-end slice

`ROADMAP.md` Part 2 flags this as the hardest stretch in the project, and T-023 as the moment
everything first works together. M4 opens with the plumbing.

### T-018 · PEP skeleton and reverse proxy

Five acceptance criteria: transparent proxying of GET/POST/streaming, header and trailer
handling, an upstream timeout and retry policy, `httpx` connection pooling, and
`/healthz` `/readyz` `/metrics`. Each was measured against the running stack before anything
was designed, which turned out to matter for four of the five.

#### One criterion cannot be met on this stack

There is no trailer support anywhere between the socket and the handler. `httpx.Response`
exposes no attribute for them, `starlette.responses` contains no trailer-related name, and
uvicorn's httptools implementation never mentions the word. The PEP therefore cannot read
trailers from an upstream, and could not emit them if it had them.

That is ADR-020, and the honest framing matters: this is not a gap more work would close, it is
absent from every layer underneath. The `Trailer` *header* is dropped as hop-by-hop — announcing
trailers that will not be forwarded is worse than saying nothing — and the criterion is recorded
as consciously unmet rather than quietly four-fifths satisfied.

#### The proxy bug that fails loudly, and the three that fail quietly

Forwarding response headers verbatim is the default mistake. Measured against a real uvicorn
pair, it breaks in two quite different ways.

Quietly: the upstream's `date` and `server` arrive alongside the proxy's own, so the client gets
**two of each**.

Loudly, and this is the one that settled the design: a compressed upstream read with httpx's
*decoding* iterator, with `content-encoding` and `content-length` forwarded, produces

    RemoteProtocolError: peer closed connection without sending complete message body
    (received 0 bytes, expected 52)

The headers described 52 gzipped bytes; the proxy sent 920 decoded ones. Reading with
`aiter_raw()` instead — bytes untouched, `content-encoding` still true — makes it correct. So
the PEP forwards raw and strips `content-length`, and the rule has a measurement behind it
rather than a citation.

`Set-Cookie` forced a smaller decision: response headers are carried as a list of pairs and
assigned to `raw_headers` after construction, because Starlette's `headers=` takes a mapping and
a mapping keeps only the last of a repeated header. Collapsing two cookies into one is the kind
of bug that surfaces as "users get logged out sometimes".

#### Retries multiply the timeout

`httpx`'s transport-level `retries` covers connection establishment only — which is what makes
it safe, since nothing has been sent yet and nothing is replayed. It also means `retries=2` with
a 2 s connect timeout takes **6.56 s** to give up on a refused port, measured. A setting that
reads as "two seconds" is six and a half.

`PepSettings.worst_case_connect_s` exists to make that arithmetic visible instead of emergent,
and a test asserts the default stays under five seconds. NFR-1 budgets the in-process decision
at p99 < 1 ms; a gateway that holds a request for six seconds on a dead upstream has spent that
budget several hundred times over.

#### A test that could not have failed

The streaming test was written through `httpx.ASGITransport`, like every other test in the file,
and it passed. Then it kept passing when the proxy was changed to read the entire body into
memory before sending any of it.

`ASGITransport` coalesces a response body into a single chunk even when the app genuinely
streams — measured against the upstream directly, with no proxy involved: one chunk. So a
chunk-count assertion made through it cannot distinguish a streaming proxy from a buffering one,
and the test was asserting nothing.

It now binds a real port. Over a socket the same upstream delivers three chunks about 250 ms
apart, and switching the proxy back to a buffering read fails both that test and the compressed-
body one. Two guards, both demonstrated.

The general shape is worth keeping: a test double fast enough to use everywhere is usually
eliding something, and the thing it elides is often the property under test.

#### Enforcing nothing, loudly

T-018 forwards every request, token or not. The decision pipeline is T-019 and the extractor is
T-020.

A component called a *policy enforcement point* that enforces nothing is worse than no gateway,
because it looks like protection. So the gap is stated where it can be seen at runtime —
`/readyz` reports `enforcing: false` — and a test class named `TestEnforcementIsNotWiredYet`
pins the current behaviour so that T-019 has to change it deliberately. The tests fail the moment
enforcement lands, which is the intended way to find out that it did.

### T-019 · The decision pipeline

The ticket that turns the gateway into an enforcement point, and where NFR-1 gets its number.

Spec `09-decision-record.md` was written first, as the rule requires. Its substantive
contribution is not the record's shape — `DecisionRecord` has existed since T-005 — but the
**precedence contract**: which cause to name when several are true at once.

#### Ordering is the whole design

`PLAN.md` §3.2 principle 4 says *every deny is explainable — a decision record names the exact
caveat, policy statement, or budget that caused it*. That is only meaningful once the pipeline
agrees in advance which of several simultaneous failures to report.

Spec 09 §3 settles it: **the first failing step wins, in step order**. The alternative — evaluate
everything, report the most severe — sounds more informative and is worse. It lets a revoked
token be reported as `SCOPE_NOT_GRANTED`, and the operator spends an afternoon adjusting scopes
on a credential that was killed hours ago.

Three sub-rules earn their place:

* **Deny beats escalate** (INV-8). The caveat list is scanned to the end even after an escalation
  is found, because approval cannot grant authority the token does not have. Escalating a
  request a later caveat forbids asks a human a question with only one correct answer.
* **Chain order decides.** Root-first, so the reported cause is the broader restriction.
* **Drift never denies.** A heuristic over natural language that can deny is one that can deny a
  legitimate payment at three in the morning. It escalates or it flags.

The fail-closed rule has one deliberate exception, and it is the same reasoning inverted: an
*unavailable* drift oracle does not deny, because failing closed on an advisory heuristic would
let its outage stop every payment in the system.

#### NFR-1, measured

**Mean 5.2 µs, median 4.7 µs, p99 well inside the 1 ms budget** over ~20,000 rounds with warm
oracles. That is roughly 200× headroom against `PLAN.md` §17 R-2's trigger (*p99 over 2 ms by M8
means porting this module to Rust*), so that risk can be considered closed rather than pending.

The benchmark asserts on p99 rather than printing it. `pytest-benchmark` reports a table nobody
reads in CI; computing the percentile from `benchmark.stats.stats.data` and asserting on it makes
the number a gate.

#### The benchmark ran nowhere

Which turned out to matter, because `make bench` pointed at `tests/perf/` — an empty directory —
so it collected nothing, and CI mentioned `perf` only in an exclusion list. NFR-1 would have been
measured by a test that never executed.

Exactly the shape of the integration-test gap found while picking M3 back up, and fixed the same
way: `bench` now selects by marker rather than directory, and the quality job runs it. Marked
tests live next to the code they measure.

#### A skipped test is not a test

The `ANCESTOR_REVOKED` case was written against a root token, which has exactly one revocation id
— so it skipped, quietly, while spec 09 §7 claimed the code was reachable. It now builds a real
attenuated chain. INV-10 (no resurrection) is the invariant behind it, and it deserved better
than a green tick on a skip.

#### Found while finishing: INV-1's property test is flaky

Not caused by this ticket, and worth more attention than it has had.

Chasing an intermittent `make check` failure — seen three times across T-011, T-018 and T-019 and
each time dismissed as unreproducible — a loop over the property suite reproduced it on the tenth
attempt:

    FlakyStrategyDefinition: Inconsistent data generation! Data generation behaved
    differently between test cases. Is your data generation depending on external state?

> **Superseded — the paragraph below is wrong.** It is left standing because the record of a
> mistaken diagnosis is worth more than a tidy page. The correct account is *Gap 13* further
> down; ADR-021 carries the measurements. In short: the ephemeral-key fact is true, the
> inference from it is false, and the actual cause is a 1 ms wall-clock limit inside
> `biscuit-python` being read as a denial.

The external state is entropy. `attenuate()` mints with a fresh ephemeral key on every call — a
fact already written down in this project's own notes — so when hypothesis replays a choice
sequence to shrink a failure, it does so against a *different child token*. The test cannot
shrink reliably, and its falsifying examples cannot be taken at face value.

That matters more than an ordinary flake. INV-1 is the central security property, and P-01 is the
project's headline answer to *how do you prove this?*. The example it printed —
`child authorized what the parent did not` — has **not** been shown to be spurious. It is
recorded as `STATUS.md` gap 13 and is the next thing to fix, ahead of new features.

The lesson is about the three earlier dismissals rather than the bug: "did not reproduce" was
recorded honestly each time, and never followed up. An intermittent failure in the one test that
carries the security claim deserved a loop the first time.

---

### Gap 13 · Two flakes, and three wrong diagnoses

The one item marked ahead of new features, because it sat under the project's central security
claim. It took four measurements to get right, and three of those were needed to unsay something
already written down.

#### First, the question that actually mattered

Not *why is the test flaky?* but **is INV-1 violated?** The suite had printed
`child authorized what the parent did not`, and no amount of "hypothesis was confused" makes that
safe to ignore.

Answered twice, independently. A hand-rolled brute force with `random` and a real authorizer:
zero violations. Then the same property driven through this project's own strategies but
*collecting* violations into a list instead of asserting — no exception, so no shrinking, no
replay, nothing for hypothesis to be confused by. Zero again, across roughly 15,000 request
contexts. **INV-1 stands.** Everything after this is about why a sound property reported
otherwise.

#### The diagnosis that was already written down, and was wrong

T-019 recorded the cause as entropy: `attenuate()` mints with a fresh ephemeral key per call, so
a replayed choice sequence meets a different child token. It is a tidy story and the underlying
fact is true (spec 01 §4).

It is also false. 200 re-mints of identical inputs produced identical token sizes and identical
authorization results. The mint is deterministic in every way the test can observe. The claim had
by then reached four places, including a pushed commit message.

#### What the running system said instead

`biscuit-python` 0.4.0's authorizer defaults to `max_time = 1 millisecond`, and it is **wall
clock, not work**:

```
AuthorizerBuilder("allow if true;").limits()
  max_facts = 1000 · max_iterations = 100 · max_time = 0:00:00.001000
```

The property harness caught every exception from `authorize()` and returned `False` — a design
that cannot distinguish *this token does not permit that* from *the CPU was busy*.

**And here the second wrong claim went in.** Instrumenting the test to log what it swallowed, the
first campaign showed 5,618 exceptions and **zero** limit errors, which looked like a clean
refutation. It was a sample-size artefact: the larger campaign found **2 in 42,014**, both inside
`verify()`. A 0.005% event is precisely what a small sample reports as absent, and that rate is
also why three earlier `make check` failures were each honestly recorded as unreproducible — and
why 30 runs of `test_attenuation.py` on its own still give 0 failures. One file does not generate
enough CPU contention to lose a millisecond.

#### Proving it, rather than arguing it

Waiting for a 1-in-21,000 event to recur is not a method. Injecting one
`AuthorizationError("Reached Datalog execution limits")` at a chosen call is, and the calls
alternate child-check, parent-check:

| Injection lands on | Result |
|---|---|
| a child check | passes — a spurious denial of the child asserts nothing |
| **a parent check** | **10 of 10 produce the false `child authorized what the parent did not`**; 9 of those 10 also report `FlakyStrategyDefinition` |

Timeout on the parent check → `False` → the assertion fires → hypothesis shrinks and replays →
the timeout does not recur → the loop no longer exits early → the interactive draw count differs
→ `FlakyStrategyDefinition`. Two symptoms, one cause, demonstrated rather than inferred.

So the printed counterexample was spurious after all — but only after the property itself had
been checked directly, which is the right order.

#### A product bug wearing a test bug's clothes

`_authorizer()` runs on every `verify()`, which is the PEP's hot path, and both real timeouts
were inside `verify()`. Measured on a depth-8 chain, an `authorize()` costs 290 µs quiet and
478 µs under 24-way contention — against a 1 ms cap, under 2× headroom before anything
adversarial. One millisecond is also the same order as NFR-1's *entire* decision budget. A
hot-path library whose internal timeout equals the system's latency target will fire under
exactly the load that target exists to describe.

Limits are now set explicitly everywhere an authorizer is built: 250 ms, 10,000 facts, 1,000
iterations (ADR-021, TM-25). `test_tokens.py` pins the library defaults so an upgrade that moves
them fails a test, and proves the constant is load-bearing by driving it to zero.

#### The fix that mattered most was the one that made the bug legible

Raising the limit removes the cause. But the reason this survived three tickets is that the
failure was *unreadable*: `is your data generation depending on external state?` points nowhere
near a library timeout. Two harness changes fix that, and they would have been worth making even
if the limit had been fine:

* Execution-limit errors are **re-raised**, never read as denials.
* INV-1 draws its mandate, caveats and probes as **one composite value** instead of interactively
  through `st.data()` inside a loop the assertion can exit early. Fixed draw counts mean a
  mid-loop failure shrinks properly instead of reporting inconsistent generation.

Injecting the same fault into the fixed harness now reports `biscuit_auth.AuthorizationError:
Reached Datalog execution limits` with a traceback to the line.

#### The second flake, which had been counted as the first

`tests/property/test_strategies.py::test_zero_ceilings_occur` drew 400 caveats from the nine-kind
union and asserted one was a `BudgetCeiling` of value 0. A 400-draw union holds 23–49 ceilings,
so the assertion was a coin flip: **3 misses in 60 campaigns**, and **2 of 30** runs of that file
at HEAD. Sampling the kind directly: 0 of 60.

That ~5% is the "residual rate" previously attributed to INV-1. Two intermittent failures in one
directory read as one intermittent failure, and the arithmetic quietly stopped making sense —
which is a signal worth heeding sooner.

The file's own docstring names the hazard, for a *different* test it had guarded against exactly
this. `strategies.py` even ships `caveats_of_kind()`, documented as *"drawn directly rather than
filtered out of the union."* Everything needed to avoid it was already there (ADR-022).

#### What to take from this

Three wrong diagnoses, and every one was defensible on paper. The distinguishing feature of the
right one is not that it was cleverer; it is that it came from instrumenting the running system
and then injecting the fault to close the loop.

The project's own rule — *check the claim against the running system before writing it down* —
was followed for the code and skipped for the diagnosis. A commit message is a claim too.

---


## What the numbers look like

| | |
|---|---|
| Tickets complete | 17 of 53 in scope (61 defined, 8 deferred) |
| Milestones | M1, M2, M3 complete; M4 started (specs 05-09 outstanding) |
| Tests | 1075, all passing (992 plus 82 integration, plus the NFR-1 benchmark) |
| Coverage on `agentiam-core`, `agentiam-sdk`, `agentiam-pep` | 100% of statements |
| ADRs | 22 |
| Specs written | 5 of 9 |
| Design errors caught before implementation | 12 |
| Wrong diagnoses written down before being measured | 3, all in gap 13 |
| Threats found by measurement | 7 (TM-19 through TM-25) |
