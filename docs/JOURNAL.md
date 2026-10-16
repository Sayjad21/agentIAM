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

> **Corrected at T-024.** The measurement is accurate; the conclusion drawn from it was not.
> `decide()` was benchmarked with a *fake* policy engine, because no real one existed — step 5
> was a dictionary lookup. Cedar costs about **80 µs**, so the real decision is nearer 85 µs and
> the headroom is about **12×, not 200×**. R-2 is comfortable, not closed. Spec 05 §6 carries
> the numbers.
>
> Worth noting how the error happened: every number in the paragraph was true. What was wrong
> was treating a benchmark of four real steps and one stub as a benchmark of the pipeline.

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


### T-020 · Where an HTTP request becomes a decision

The extractor is step 1 of the pipeline and the only step that reads an untrusted wire format.
It looks like plumbing. It is where the most interesting threat so far was found.

#### The mapping table is not the interesting part

`PLAN.md` asks for a config-driven `(method, path)` → scope mapping, JSONPath argument
extraction, an `arg_digest`, and deny-by-default on an unmapped route. All of that is
straightforward, and spec `10-scope-extraction.md` writes it down.

The part worth reading is §5, and it came out of a probe rather than the plan.

#### The PEP can authorize a value the upstream never acts on

The gateway forwards the **original bytes**. It does not rewrite the query string or the body.
So two parsers read every request: ours, and the upstream's. **Measured**, for
`amount=1&amount=999999`:

| Parser | Value |
|---|---|
| `dict(starlette.QueryParams(...))` | `999999` — last |
| `urllib.parse.parse_qsl(...)[0]` | `1` — first |
| Go `net/http` `Form.Get`, Java `getParameter` | first |
| PHP | last |

A caveat `amount <= 5000` checked against `1` passes, and a Go upstream executes `999999`.
Nothing downstream detects it: the token was valid, the caveat was satisfied, and the decision
record is honest about what *it* saw. Duplicate JSON object keys are the same shape —
`json.loads` silently keeps the last.

That is TM-26, and it is TM-24's family: a string that means one thing where it is checked and
another where it is used. The first was found by asking a library what `block_source()` does with
a quote; this one by asking what `dict(QueryParams)` does with a repeated key.

**The answer is to never pick a winner.** Where a source expression could resolve to more than
one value, the request is denied. Choosing first-wins or last-wins would be choosing an upstream
to agree with, and the upstream is not ours to choose. The refusal applies only to names the
mapping constrains — a repeated `tag=` that nothing reads is not ambiguous in any way that
matters, and denying it would break ordinary clients for nothing.

#### Two more places the two views can diverge, both closed the same way

* **Percent-encoding.** `compile_path` leaves a matched parameter encoded — `/invoices/a%2Fb`
  yields `a%2Fb` — while the upstream reads `a/b`. Path and query values are decoded **once**, so
  our view matches. Once, because repeated decoding is its own smuggling primitive.
* **Unicode.** NFC, the same rule `hashing.py` already applies, so two visually identical
  arguments cannot produce two different digests or two different caveat outcomes.

#### Guessing a type is a security decision

The first draft inferred numeric-ness from the text: if it parses as a number, scale it. The
counterexample is ordinary rather than adversarial — an `account_id` of `"0012"` extracts as
`12`, and a caveat comparing it as a string stops matching the value the upstream uses. Silent,
and it fails open.

So the type is **declared**: `body.amount:number`, `query.limit:number`. Nothing is inferred
(ADR-024).

That raised a question the draft ADR answered confidently and wrongly. `ArgPredicate` compiles to
`reject if arg(p, $x), <negated>` so it is vacuous when the argument is absent — so if a numeric
comparison against a *string* term merely failed to match, the reject would never fire and a
mistyped argument would fail **open**, not closed. Measured before writing it down:

| Term | Result |
|---|---|
| `100` numeric, under the ceiling | allow |
| `50000001` numeric, over | deny |
| `"100"` **string**, under | **deny** |
| `"50000001"` **string**, over | **deny** |
| absent | allow |

Both string rows deny, so forgetting the annotation costs a false *denial* — while the inference
rule would have cost a false *authorization*. The asymmetry is the whole argument for requiring
the annotation, and it now has a test (`TestArgTermTyping`) rather than a confident sentence.
The same test pins the vacuity of an absent argument, which is what spec 10 §2 relies on when it
declines to deny.

#### A promise from T-018 that had to become conditional

T-018 said *"nothing is buffered, so a large upload costs the PEP a constant amount of memory."*
True, and incompatible with reading JSON arguments out of the body.

Measured: `Request.json()` then `stream()` replays the body byte-identically; `stream()` then
`json()` raises `RuntimeError: Stream consumed`. So extraction is possible, must precede
forwarding, and buffers while it does.

The guarantee is now conditional and says so (ADR-023): routes with no `body.` source stream
unchanged; routes with one read at most 1 MiB and deny above it, because an unbounded read on an
attacker-chosen path is TM-14 reintroduced at the gateway. T-018's docstring was amended rather
than left standing — a claim that was true when written and is now conditional is worse than one
never made, because a reader who checks the docstring and not the ADR gets the wrong answer.

#### Enforcement does not turn on here

`CONTEXT.md` and `STATUS.md` gap 11 both said T-020 would wire `decide()` into the gateway and
flip `enforcing: true`. Reading `decide()`'s signature says otherwise: it needs a `BudgetOracle`,
which is T-021's local lease pool, plus policy and revocation oracles that do not exist yet.

Wiring it now would mean shipping allow-all stubs for three of five decision inputs in order to
report `enforcing: true` — which is worse than the honest `false` that is there today, and
exactly the kind of thing gap 11 exists to complain about. T-020's acceptance criteria are
entirely about the extractor; none of them mention the gateway. So the note was wrong, and gap 11
now names **T-021** as where enforcement actually begins.

#### Closed on the way past

TM-25's residual, recorded in ADR-021 the day before: an exceeded Datalog limit escaped `verify()`
as a raw `biscuit_auth.AuthorizationError` carrying no reason code, so the PEP would have turned a
transient CPU stall into an unexplained 500 rather than a named deny. It is now
`VerificationLimitError` carrying `VERIFICATION_LIMIT_EXCEEDED`.

That is one new entry in a closed enum, so it needs `PLAN.md` §6.9 to say so — the supersession
note is there, and the test that pins the enum against the plan was updated rather than deleted.

---


### T-021 · The lease pool, and four tests that proved nothing

The component that makes NFR-1 possible. `reserve()` is synchronous and touches nothing but
memory, so a tool call never waits on the ledger; acquiring, topping up and releasing all happen
off that path. Spec 04 already specified the protocol, so the interesting part of this ticket was
not the design.

#### Four of five guards were not guards

The suite passed on the first run: 26 green. Then, following the standing rule that *a guard
never seen to fire is not a guard*, each guard was removed in turn and the suite re-run.

| Guard removed | Result |
|---|---|
| single-flight top-up | **26 passed** |
| low-water comparison | 1 failed ✓ |
| shutdown drains in-flight top-ups | **26 passed** |
| `ttl > 2S` configuration check | **26 passed** |
| lease-state check in `aclose()` | **26 passed** |

Four tests were passing for reasons unrelated to what they claimed to test.

* **Single-flight.** Ten reserves of 8 against a 100 lease cross the 25 mark exactly once — the
  first nine are above it. The test never had two concurrent crossings, so it could not tell a
  single-flight implementation from one without.
* **Shutdown drains top-ups.** The gate was released *before* `aclose()` was called, so nothing
  was ever in flight. It now asserts `not closing.done()` while the `ACQUIRE` is blocked, which
  is the actual claim.
* **`ttl > 2S`.** Nothing constructed an unsafe configuration at all. There was no test.
* **`aclose()` idempotency.** This one was subtler: the test was fine, but the guard being
  removed was the wrong one. Idempotency comes from the lease-state check, not from the `_closed`
  flag — the flag's early return was doing nothing, and was deleted rather than tested.

All five now go red when their guard goes. The lesson is not that the tests were careless; it is
that *green on the first run* is the least informative signal a test suite emits, and the
removal sweep costs about ten minutes.

#### Three probes, three things that would have cost an afternoon

**`asyncio.get_running_loop()` raises outside a coroutine** — including from a worker thread.
`reserve()` is deliberately synchronous, so it may be called from either place, and a top-up
cannot always be scheduled. It therefore schedules when it can and never requires that it can:
a pool that refuses to spend budget it demonstrably holds, because it could not arrange to fetch
more, has the failure backwards (ADR-025).

**`signal.SIGKILL` does not exist on Windows.** `PLAN.md` words the acceptance criterion as
"tested with SIGKILL". Written literally, that test imports fine in Linux CI and fails on the
development host. `Popen.kill()` is the portable spelling — `SIGKILL` on POSIX,
`TerminateProcess` on Windows — and neither gives the child a chance to clean up, which is the
only property the test needs.

**Patching `socket` around `asyncio.run()` breaks the loop, not the code under test.** Measured:
`ProactorEventLoop.__init__` calls `socket.socketpair()` for its self-pipe, so the patch fires
during loop construction and the test fails having proved nothing about `reserve()`. The patch
goes on *inside* the test body, after pytest-asyncio has built the loop. A second test asserts
the patched `socket.socket` actually raises, so the zero-network test cannot pass by having no
teeth.

#### The crash test kills something real

Graceful shutdown is tested against a fake ledger. The fourth acceptance criterion is what
happens when there *is* no shutdown, and it cannot be faked: a real child process acquires a real
lease against a real Postgres and is killed mid-sleep. Nothing runs `RELEASE`.

The lease stays `active`, the budget stays stranded, and a reap **before** `expires_at + S`
correctly reclaims nothing — the skew margin, load-bearing again. Past the margin the full pool
is spendable without anyone having asked. The 80-second worst case from spec 04 §7 also gets an
arithmetic test, so the number quoted in the spec, the threat model and the pitch cannot drift
from the parameters that produce it.

#### Answering a question rather than deferring it

Spec 04 §17 Q2 asked whether heartbeat-based early reclaim is worth building. It is not, and the
reason is better than "no time": a heartbeat replaces a reclaim rule derived from the ledger's
own `expires_at` — issued once, immovable — with one derived from message arrival, which has no
bounded lateness. A live PEP whose heartbeat is delayed by a GC pause gets its lease reclaimed
underneath it, which is TM-22 arriving through a new channel. The margin `S` bounds clock
disagreement; nothing bounds queueing delay. So the heartbeat would need its own grace period,
which is a TTL by another name.

#### Enforcement still does not turn on, and this is the second time that has moved

Gap 11 said T-019, then T-020, and now T-023. That is worth stating plainly rather than editing
quietly.

The pool supplies the `BudgetOracle` `decide()` requires, so the *budget* half is genuinely ready.
What is still missing is a `RevocationOracle` and a `PolicyEngine`. An empty revocation set is
arguably honest — nothing can revoke yet, so nothing is revoked — but an allow-all policy engine
is not: it reports that policy was evaluated when no policy exists. Shipping both to make
`/readyz` say `enforcing: true` would buy a true-looking flag with two fail-open stubs, which is
the exact complaint gap 11 exists to record.

T-023 is the end-to-end thin slice and depends on T-018 through T-022 — it is where the wiring
was always going to belong. The earlier notes were guesses written before `decide()`'s signature
was read.

---


### T-022 · The emitter, and an ADR that found its own bug

Step 10 of the pipeline. It runs *after* the decision, so nothing in it can change a verdict —
with one deliberate exception, which is the whole ticket.

#### Deny is the default, and that is the unusual part

`PLAN.md` sets the back-pressure default to **deny**: when the audit buffer is full the request
is refused. A system that cannot record what it authorized should not authorize. The pitch is
chain of custody and NFR-6 makes the ledger tamper-evident, which is worth nothing if records can
quietly go missing under load.

`BLOCK`, one of the three options the plan names, is not implemented at all. `emit()` is
synchronous and runs inside the ASGI event loop, so "blocking" there does not stall one request —
it stalls the loop, and with it every other in-flight request, `/healthz`, and the lease pool's
top-up tasks. A slow audit sink would become a total outage. `DENY` fails the same requests,
per-request, with a reason code, while the process keeps serving and keeps draining. An option
that is never the right choice is a trap in a config file, so it is absent rather than
discouraged (ADR-026).

#### Writing the ADR found the bug

The entry claimed a full buffer denies rather than loses records. Setting that down next to the
code made it obvious that a *failing* sink took the other path: the first implementation counted
a failed batch and discarded it, so a broken ledger degraded to silently losing exactly the
records the deny policy exists to protect. The argument was one the code did not honour.

A failed batch is now retried and stays at the head of the queue while it is, so a persistently
broken sink fills the buffer and `DENY` starts refusing — a broken audit path stops authorization
the same way a saturated one does. `max_retries` bounds a poison batch after that, counted in
`lost_records`.

This is the second time this project has had a document catch a code defect rather than the other
way round, and both times it was because the document had to state a *consequence* rather than a
behaviour.

#### And the test found a second one

The new retry logic needed a test that a wedged sink stays wedged. It failed, because `flush()`
retried in a tight loop — spending the entire `max_retries` budget in microseconds against a sink
that had had no time to recover. That is not a retry; it is a spin with a counter attached.
`flush()` now gives each batch one attempt and leaves the pacing to the drain interval.

#### A hang is a worse failure than an assertion

Mutating the deny guard to a silent drop made the suite **hang** rather than fail. The gate that
stalls the fake sink was released after the `pytest.raises` block, so when the expected exception
did not arrive, the test aborted with the gate still shut and `aclose()`'s flush waited on it
forever.

A hang gives no name, no line, and in CI it burns the job timeout instead of reporting. Every
stalled sink now releases in a `finally` via a small `stalled()` helper, and the same mutation now
fails in 0.7 seconds with three named tests. Worth generalising: any test fixture that can block
should be arranged so that a *failing assertion* still unblocks it.

Eight guards were mutated in total; all eight now go red.

#### Two measurements about tracing

`opentelemetry-api` is a planned dependency (`PLAN.md` §4) and the collector is M6 (ADR-001), so
the API goes in now and the SDK arrives with T-049. Two things about that were worth measuring
rather than assuming:

**A no-op span is not free.** `start_as_current_span` plus one attribute costs **5.58 µs** even
with no SDK — the tracer is a `ProxyTracer` and the span a `NonRecordingSpan`, but the context
attach and detach are real work. Against `decide()`'s ~5.2 µs that roughly doubles the decision;
against NFR-1's 1 ms budget it is 0.56%. Both framings are true and the budget is the one that
matters, so tracing is on by default with a switch for T-053 to measure both. `emit()` plus a span
benchmarks at 6.3 µs, and the benchmark asserts p99 rather than printing it.

**A no-op span's `trace_id` is all zeroes**, with `is_valid` False. Handing that back as a
`DecisionRecord.trace_id` would put a correlation handle into every record that correlates every
decision to every other one — worse than none. `current_trace_id()` returns `None` there, and the
`trace_id` has to come from the request.

#### Spec 04 §17 Q3, answered

The `LEDGER_COMMIT` batching window: **64 records or 500 ms**. The interesting bound is the upper
one — a batch must land before its lease can be reaped, because a commit arriving after
reclamation is a late commit (§11), rejected and recorded as an anomaly with a real spend going
unrecorded. 500 ms against a 60 s TTL is a margin of 120×, which leaves that failure dominated by
process death rather than by the batching choice. Below about 10 ms the batching buys nothing and
the wakeups cost more than the writes they combine.

---


### T-024 · Cedar, and the benchmark that was measuring a stub

Taken out of order. T-023 is the end-to-end slice and the point where `/readyz` stops saying
`enforcing: false`, and after T-022 four of `decide()`'s five inputs were real. The fifth had no
implementation.

An empty revocation set is honest — nothing can revoke until T-038, so nothing is revoked. An
allow-all policy engine is not: it reports that policy was evaluated when no policy exists, which
is precisely the complaint `STATUS.md` gap 11 was opened to record. T-024's only dependency was
T-019, already done, so nothing was blocking it; the M5 grouping in `PLAN.md` §9 is milestone
tidiness, not a dependency. It moves ahead (ADR-027).

#### The measurement that changed a claim

T-019 recorded `decide()` at 5.2 µs and concluded *"about 200× headroom… R-2 can be considered
closed rather than pending."*

Every number in that sentence was right. The conclusion was wrong, because the benchmark ran
against a **fake** policy engine — step 5 was a dictionary lookup, since no real engine existed.
Cedar costs about 80 µs. The real decision is nearer 85 µs and the headroom is about **12×**.

R-2 (*p99 over 2 ms by M8 triggers a Rust port*) is comfortable, not closed. The T-019 entry now
says so. The failure mode is worth naming: benchmarking four real steps and one stub, then
describing the result as a benchmark of the pipeline. Nothing was measured incorrectly; the
scope of what had been measured was mis-stated.

#### Parsing once is not an optimisation

| Arrangement | Per authorize |
|---|---|
| Source string re-parsed every call | 167.7 µs |
| `PolicySet.from_str` once | **80.1 µs** |
| Policy set *and* entities pre-parsed | 61.7 µs |

The naive spelling spends 17% of NFR-1's budget re-parsing a bundle that changes hourly at most.

The third row is unreachable, and checking why was worth the ten minutes: `PLAN.md` §6.5 puts
`depth`, `task_id` and `role` on the principal entity, and those change per request. Moving them
into `context` to pre-parse a static entity set measured **78.5 µs against 83.0** — under 5%. So
the plan's entity model stands, on evidence rather than deference. Cedar's own model treats
entities as the durable graph and context as the request; 5% is not a reason to make
`principal.depth` unavailable to a policy author who reasonably expects it to be there.

#### Three decision values, and the obvious spelling is wrong

`cedarpy.Decision` has `Allow`, `Deny`, and **`NoDecision`** — the last returned when the policy
set fails to parse, with the errors in `diagnostics.errors`.

```python
if response.decision == Decision.Deny:   # NoDecision falls through as "not denied"
```

A corrupt bundle would stop the policy layer enforcing, silently, while everything still looked
green. The engine writes `allowed = decision is Decision.Allow` instead, so an unrecognised
outcome — including a fourth member some future Cedar release adds — fails closed. Bundles are
additionally rejected at *load*, which makes `NoDecision` unreachable in production rather than
merely handled.

Mutation-checked: flipping it back to `!= Deny` turns two tests red.

#### Money keeps its precision and its readability

The token layer scales money by 10⁴ because biscuit's Datalog compares integers. Copying that
into Cedar would make T-029's NL compiler emit `context.amount <= 1000000000` for *"no payments
over ৳100,000"*, and make a human reviewing a bundle do arithmetic to check it.

Measured: Cedar's `decimal` extension holds **exactly four decimal places** — `0.0001` accepted,
`0.00001` rejected. That is `NUMERIC(20,4)`, the same precision as everything else in this
system. So money crosses into policy with no scale conversion anywhere, and a policy reads:

```
context.amount.lessThanOrEqual(decimal("500000.0"))
```

The catch is method syntax: Cedar's `<=` accepts only `long`, `datetime` and `duration`, so
comparing a decimal with an operator is a type error. And a bare float in the request context is
rejected outright as `NoDecision` — correct for a system whose rule 6 says money never touches a
float, and already mapped to a denial.

#### The corpus

Thirty-two request/expectation pairs against one bundle shaped like an organization's actual
rules rather than a syntax demo: unconditional permits, role guards, amount and depth ceilings,
resource attributes, and `forbid` beating `permit` in both directions. Each row carries a *why*
column, because a corpus nobody can explain is a corpus nobody maintains when a policy changes.
Two meta-tests guard it — that it still has ≥30 rows, and that it still contains both outcomes,
since an all-allow corpus would pass against a broken engine.

---


### M4 · The audit chain, and the failure a hash chain cannot see

*The credit limit and chain of custody for AI agents.* The second half of that sentence had no
implementation until now — `chain_hash()` existed from T-005, with nothing calling it.

Not a numbered ticket; `PLAN.md` §9 lists it as an unnumbered M4 item. Spec 08 was written first
anyway, because chain of custody is a contract the pitch makes and NFR-6 is stated as a testable
claim.

#### Appending is a read-modify-write, and that is the whole problem

Computing a link means reading the current head hash and writing a record derived from it. Two
concurrent appends that both read head *h* both claim `prev_hash = h`, and one loses its `seq` to
a primary-key collision.

The surviving evidence is a chain with fewer records than were submitted — and **nothing in a
hash chain detects a record that was never written.** The chain verifies perfectly. That is a
worse outcome than a detected break.

So appends serialize on a single-row `audit_chain_head`, locked `FOR UPDATE`, the same shape
spec 04 §4.1 uses for the budget pool. A single row rather than `SELECT max(seq) FOR UPDATE`,
because an empty table has no row to lock: the first two concurrent appends would both find
nothing and both insert `seq = 1`. The lock has to exist before the first record does.

Shown to be load-bearing, per the T-013 precedent: a test drops the `FOR UPDATE`, widens the
window with a sleep, and asserts that fewer records survive than were submitted. It also asserts
that *some* writers failed — otherwise the window was too narrow and the test would be proving
nothing.

#### Head truncation

Deleting the newest records leaves a chain that verifies perfectly from genesis to whatever is
now the head. No hash detects it, because every remaining link is genuine.

This is inherent to a hash chain with no external anchor, and it is why `audit_chain_head` stores
`last_seq` rather than the verifier computing `max(seq)`. The head is the independent witness:
a head claiming five records when three are present is the only evidence the other two existed.
`verify_chain()` compares them and reports truncation by name.

Stated in spec 08 §7 as a limitation with its bound rather than left for a judge to find. The
residual — a database superuser can rewrite both the chain and the head — is operational, not
cryptographic, and external anchoring is future work.

#### What verification reports

NFR-6 asks for tamper detection. It would be satisfiable, and useless, by returning a boolean.
`verify_chain()` names the **first** inconsistent seq, because everything after a break is
untrustworthy by construction and an operator holding a broken chain needs somewhere to look.

Four shapes are detected and each has a test: an altered record, a mid-chain deletion, a
reordered `prev_hash`, and head truncation.

The CLI keeps *"the chain is broken"* and *"I could not look at the chain"* as different exit
codes. Collapsing them would hide a database outage behind a tamper alert, which is a bad night
for whoever is on call.

#### The seam to T-022

`LedgerAuditSink` is the `RecordSink` the emitter has been writing to a list in tests. It lives
in the control plane rather than the PEP because it owns a database session, and the PEP's
contract is that its hot path never touches one — the emitter's drain task is off that path,
which is exactly what makes that placement legal.

A failed write raises, deliberately. ADR-026 claims a broken audit path stops authorization
rather than silently losing records; a sink that swallowed its own errors would make that claim
false again. There is an end-to-end test for it: a sink that fails once, and the record still
arrives.

---


### T-023 · The thin slice, and a check that no code path ran

M4's exit gate, and the first point at which the project's one sentence is demonstrable rather
than asserted. A root biscuit goes through the T-018 gateway, the T-020 extractor, the T-019
pipeline, the T-024 Cedar engine, the T-021 lease pool against real Postgres, and the T-022
emitter into the real audit chain, with the M4 stub tools behind it. Nothing is faked except
the network, which is an in-process ASGI transport.

#### The finding: an authority check that nothing evaluated

Wiring the intent header produced a test that failed for the wrong reason — a *mismatched*
intent was allowed.

The token's authority block carries six checks (spec 01 §5), and biscuit's own authorizer
enforces all six. But `verify()` deliberately never calls `authorize()` — it extracts facts, so
that reason codes can be precise, which is the whole argument in its module docstring. So
**whatever `decide()` does not re-implement is enforced nowhere on the path the PEP runs.**

Measured side by side on the same token: `decide()` said ALLOW, biscuit said DENY.

Five of the six were covered — scope by `decide()`, the validity window and the chain's depth by
`verify()`, the mandate's budget ceiling by the ledger that issues the lease — and intent by
nothing. That is INV-7's binding and TM-10's defence against task redirection, both decorative
on the live path since T-019. Recorded as TM-27; `decide()` now checks it, ordered before the
caveat loop so a wrong task is reported as `INTENT_MISMATCH` rather than as whichever caveat
happens to fail.

The lesson is about the shape of the mistake rather than the bug. `verify()`'s split was a good
decision, made for a good reason, and it quietly moved responsibility somewhere nobody wrote
down. The audit that would have caught it is *for each check the token makes, name the code that
enforces it* — which is now four lines of comment above step 4.

#### Emitting before forwarding

Spec 09 numbers emit as step 10 and forward as step 8. The pipeline does them the other way
round, and ADR-026 forces it: a full audit buffer must **deny**, and a policy that denies after
the upstream has already acted is not a policy.

Everything the record contains is known once step 7 finishes — verdict, failing cause,
reservation, budget. What is *not* in it is the upstream's answer, and spec 09 §6 already says
that was never part of the decision. The cost is a record for a call the upstream may then fail;
the record says *this was authorized*, which stays true.

#### Two bugs the wiring found in code that already passed its own tests

**The emitter raced itself.** `flush()` and the background drain task both call `_write_batch`,
and both could claim the same pending batch — one finished it, the other asserted on a `None`.
Invisible until something ran a drain loop alongside an explicit flush, which nothing did until
now. Serialized with a lock.

**`request.url.query` is a `str`, not bytes.** The gateway called `.decode()` on it. Every app
test passed because they all build the app *without* a pipeline, so the enforcing branch never
ran. Fixed, and there is now a unit test that builds an enforcing app — faster than the e2e
slice and, more to the point, inside the suite `make test` actually runs.

#### The e2e marker ran nowhere

`make test` excludes it and so did every CI job. The slice would have been green on one machine
and watched by nothing.

This project has made that exact mistake twice: the 45 integration tests no job ran (gap 9), and
`make bench` pointing at an empty directory. Both were found by accident. There is now a
`test-e2e` target in both the Makefile and its PowerShell mirror, and a fifth CI job.

#### Gap 11 closes

`/readyz` reports `enforcing: true`, and the flag is **derived from the wiring** rather than
declared — an app built without a pipeline still reports `false`. A constant would let the flag
and the behaviour drift apart, which is precisely what gap 11 recorded.

`TestEnforcementIsNotWiredYet` was a tripwire: it failed the moment enforcement landed, which is
how the change was meant to be noticed. It is retired and replaced by `TestEnforcementIsOptOut`,
because the property it guarded — a transport that keeps saying it is a transport — is still
worth pinning.

The estimate for gap 11 moved four times before it landed. Three of those were guesses written
before reading `decide()`'s signature; the fourth was the decision to build T-024 first so the
slice would never wire enforcement around a stub.

---


### T-025 · Signed bundles, and the attack a signature cannot see

M5's first ticket. Its dependency, T-024, was already done because it was pulled forward.

#### The rollback attack is the interesting requirement

`PLAN.md` asks for *"an older signed bundle rejected because bundle version must increase
monotonically"*, and the reason that is a separate requirement from signature verification is
easy to miss: **an old bundle is correctly signed.** Verification has nothing to object to. An
attacker who captures a legitimately-published bundle and replays it — restoring a permission
that has since been removed — presents a perfect signature.

So the defence cannot be cryptographic. It has to be a monotonic counter the cache remembers.

That forced a small design split. `version` is a human label (`"2026-08-15.3"`) and is what
lands in `DecisionRecord.policy_version`. `serial` is the integer rollback compares. Two fields
for what looks like one concept, because **string labels do not order**: `"v10" < "v9"`
lexicographically, and a rollback defence that depends on how an operator names things is not a
defence. There is a test asserting that inequality, so the reason survives the next reader.

#### The library raises, and the API keeps that

**Measured:** `Ed25519PublicKey.verify` raises `InvalidSignature`; it does not return `False`.

`verify_bundle` does the same, deliberately. A boolean API invites `if verify(...)` being
written where `if not verify(...)` was meant, and the failure mode of that typo is *accepting
every bundle* — a total, silent loss of the property, from a one-character mistake that reads
fine. Raising removes the possibility.

Four tamper shapes were checked and all four raise: a flipped signature bit, an altered payload,
an empty signature, and a signature from a different key.

#### What gets signed is the part a library cannot decide

Rule 1 says never write your own crypto, and this ticket writes none. What it does decide is
*what the signature covers* — and getting that wrong is how a signature ends up covering
something other than what is enforced.

It covers the bundle's **canonical JSON**, the same canonicalization the audit chain uses, so
two encodings of one bundle produce one signature. Signing raw wire bytes instead would mean
re-serializing a bundle in transit invalidates it, and the first person to hit that concludes
the signing is broken.

Every field is covered except the signature itself, with a parametrized test per field.
`serial` most of all: leaving it outside the signature would let a rollback be presented under
a valid signature, which is precisely the attack above.

#### The one place this system does not fail closed

A rejected bundle leaves the **previous** one serving (`PLAN.md` §11 EC-P01) rather than
emptying the cache. Everything else here fails closed, and this does not.

The reason is that the alternative is worse. A forged bundle is evidence of an attack in
progress; responding by discarding the last known good policy would let an attacker disable the
policy layer *by sending garbage* — much cheaper than forging a signature. Keeping the previous
bundle means the attacker achieves nothing, and the staleness clock keeps running underneath, so
if the real bundle never arrives the PEP fails closed on age anyway.

There is a test named for that argument rather than for the mechanism, because the mechanism is
one `if` and the argument is the whole reason it is written that way.

#### Hot reload is one rebind

Verify and parse first, then replace a single reference. In-flight requests finish against the
engine they started with, which is the correct outcome rather than a compromise: a decision made
under bundle *n* was made under bundle *n*, and the record says so. There is a test that takes a
bound engine, swaps the bundle underneath it, and asserts the old one still answers the old way.

Nothing to lock on the read path — an attribute rebind is atomic under the GIL — and nothing can
observe a half-loaded bundle, because everything that can fail has failed before the swap.

#### Answering Q2 rather than deferring it

Spec 05 §9 Q2 asked whether `stale` should deny immediately or serve a grace window. It denies
immediately: a grace window is a second staleness limit with a friendlier name, and it turns the
failure mode into *policy silently out of date* rather than *policy refused*. The operator's fix
is identical either way, and `max_staleness` is already the knob — setting it to 600 s **is** the
grace window, stated once instead of twice.

---

### T-033 · Drift features, and three numbers that changed the design

Scoped down before it started. `PLAN.md` asks for six features feeding a calibrated logistic
regression, but T-034 (the dataset) and T-035 (the classifier) are both deferred — so the
model those six features feed will not exist for the submission. Three of the six also need
per-task history the PEP deliberately does not hold, against a statelessness choice that
ADR-035's whole intent binding rests on. So T-033 ships **f1, f2 and f5**, and ADR-036
records what deferring the other three costs.

The alternative — compute all six, with f3/f4/f6 returning a neutral constant — was rejected
because a recorded constant is indistinguishable in a dataset from a real observation of
zero. A missing feature is honest; a fabricated one is not.

#### Probing the embedding model found two defects in T-032

The habit paid again, and this time on shipped code rather than on a spec.

**Measured:** a cold embedding call against `nomic-embed-text` takes **14,244 ms**. The
oracle T-032 shipped has a 2 s timeout. So the first scored request after the model is
evicted does not run slowly — it times out and fails open, every time. Ollama drops an idle
model after roughly five minutes, so this is not a startup curiosity; it recurs all day.
And because `lru_cache` does not memoize exceptions, every request in that window re-paid
the full 2 s.

Drift, on a cold PEP, was not slow. It was **absent** — and nothing in the suite could see
that, because every drift test mocks the transport.

**Measured, and worse in aggregate:** constructing an `httpx.Client` costs a median
**724.7 ms** (p95 1,603.5 ms). T-032 built one inside every cache miss, on the asyncio event
loop, inside `decide()`. That is 37% of its own 2 s timeout spent before a byte went out,
and at p95 the construction alone nearly exhausts the budget. Hoisting it to one client per
process took a cache miss from 747.9 ms to **83.3 ms** — about 9x, for a change that looks
like tidying.

Neither number was guessable. "Cache the model at startup" reads like an optimisation in the
ticket; it is a correctness requirement, and the ticket does not say so.

#### f5 exists because of a measurement, not a hunch

The interesting result was negative. Against the task *"Pay invoice INV-2291 from vendor
Rahman Textiles for 45000 BDT"*, inflating the payment to 9,500,000 BDT — 211x — moved f2 by
**0.0102**, from 0.8139 to 0.8037. Aligned and drifted cases are separated by roughly 0.32.
The inflation is inside the noise.

**Semantic embeddings are near-blind to numeric magnitude.** No cosine feature can catch an
amount attack, however many of them are stacked. That is what makes f5 — plain symbolic
overlap between argument values and the task text, no model involved — the only one of the
three that sees it. The test named `test_f5_sees_the_amount_attack_f2_cannot` is the one that
justifies the feature's existence, so it carries the measurement in its docstring.

f5 also had to be careful in a way the one-line sketch in `PLAN.md` does not suggest. Numbers
compare by **value**, so the ledger's 10⁴ scaling and a thousands separator do not cause a
false mismatch — but `4500` must not match a task that says `45000`, which a naive substring
check would allow, and that is precisely the direction an attacker would push. Strings
compare as folded substrings, which is what keeps an account id of `"0012"` from matching a
task that says `12`.

#### A test for homoglyphs that tried to smuggle homoglyphs

Small, but the right kind of small. The NFKC test was written with literal fullwidth
characters, and `ruff` rejected it under RUF001. It was right to: this repository lost 232
characters to an encoding accident once, and a test *for* invisible-codepoint evasion is the
last file that should carry invisible codepoints. The string is now built with `chr(ord(c) +
0xFEE0)`, so the source stays ASCII and the comment says why.

#### What was deliberately left undone

Features are computed but not persisted. `DecisionRecord` already has a `drift_score` field
that `decide()` populates and the pipeline silently drops, so there is no honest place to put
a feature vector yet. Wiring both at once, on the ticket that fixes the record, is the smaller
change — and pretending the features are recorded when they evaporate would be exactly the
kind of claim this journal exists to catch.

---


## M5 — Policy activation, the NL compiler, drift v0, revocation

### T-026 · The activation gate, and a second gap it left behind without saying so

`operator activation gate for policy bundles` (ADR-030): a bundle pushed toward the PEP's
cache runs the full 51-case corpus, derived from the demo workflows, inside the activation
pipeline before it is allowed to replace the active one. Any failing case is a 409 and the
bundle never loads. The corpus lives once, in `agentiam_core.policy_testing`, not copied —
T-027 needed the same 51 cases for its authoring diffs, and two copies of "the test suite
that decides whether a policy is safe" is exactly the kind of duplication that drifts.

The gate itself was correct. What was not caught here, and only surfaced while working
T-030 four tickets later, is that nothing had actually wired it into the Control Plane's own
`POST /policy/activate` — the corpus ran in the PEP's activation path, and the console's own
"activate" button skipped it entirely. ADR-039 tells that story; it belongs there rather than
being anticipated here, since at T-026's own commit the gate did exactly what its acceptance
criterion asked.

---

### T-027 · The Cedar authoring UI, and moving a corpus so two things can't disagree

`agentiam-controlplane`'s Admin Console (ADR-031): edit a policy, run it against the corpus,
see a diff against what's live, all through FastAPI + Jinja2 + HTMX + Tailwind — the stack
`PLAN.md` §4.3 already settled on, because a second frontend language and a build step would
only pay for themselves if the identity-tree visualization (T-045's D3 work) needed React,
and Jinja handles that fine too.

The decision worth recording is the corpus move: it had been living in `agentiam-pep` for
T-026's activation gate. If the console kept its own copy for authoring diffs, "this policy
passes here" and "this policy passes at activation" become two different claims that happen
to usually agree — until someone edits one file and not the other. Moved to
`agentiam_core.corpus` instead, read by both. One source of truth, not two that are supposed
to match.

---

### T-028 · The Ollama client, and egress as something you can prove rather than promise

`ollama_client.py` (ADR-032): the compiler's first LLM client, wrapping `httpx.AsyncClient`
with the base URL hardcoded to `127.0.0.1:11434` rather than using the official `ollama`
package. The reason for raw `httpx` over the convenience package is that a hardcoded
localhost URL is a claim a reader can check by looking at one line, where a wrapped SDK
client's actual destination is one more thing to trust. `temperature=0`, a fixed seed, and
Ollama's `format` parameter constraining output to a JSON schema, for the determinism a demo
needs.

This decision did not survive contact with the timeout it also shipped with — see ADR-038,
folded into T-029 below rather than narrated twice — and the no-egress guarantee itself was
later superseded outright by ADR-040, once a real generation was measured against it. Both
are the honest continuation of this ticket, not a correction of something wrong at the time:
`qwen2.5:7b-instruct-q4_0` was not installed on the development machine when this shipped,
so nothing here had yet met a real inference call.

---

### T-029 · The compiler, and an instrument that measured nothing while claiming to

The commit shipped `compiler.py`, structured JSON-schema output via Pydantic
(`CompilerTestCase`/`CompilerOutput`, ADR-033), and a 30-case evaluation dataset. The
substantive story is what happened to that dataset afterward, and it is worth being blunt
about because two earlier documents — `STATUS.md` and `ADR-033` itself — both said the
compiler had been "evaluated against a 30-case corpus." It never had been.

**Dataset v1 could not be passed by any compiler, of any quality.** Of the 30 cases, the
positive principal id appeared verbatim in the English prompt in only 10 of them — the rest
needed the compiler to *infer* an identity Cedar had no entity for, because the harness
evaluated every case with `entities=[]`. Role and ownership checks had nothing to check
against. Separately, the harness itself passed bare names (`"admin"`) where Cedar needs
entity uids (`User::"admin"`), so every request returned `NoDecision` regardless of what the
model produced. **0 of 30, always, independent of the compiler.**

The fix was not a better prompt. It was an instrument that could tell the difference between
a broken harness and a broken compiler: `evaluate_compiler.py --validate` now runs every
case's own reference policy — no model involved — and must score 30/30 before a run against
a real model is worth starting. That is the harness checking itself, the same discipline
T-016's invariant checker and T-025's rollback test apply to their own claims.

**Then the second thing.** Dataset v2, self-validating, still returned inflated numbers on
an early pass. Three of the dataset's own cases had been pasted into `_SYSTEM_PROMPT` as
few-shot examples during tuning — meaning the compiler had, quite literally, been shown the
answer to three of the thirty questions on its own exam. The two figures that round produced
are not repeated here; they were real outputs of a real run and also not an honest
measurement of the compiler, which is the whole reason they are not worth citing.
`test_the_prompt_does_not_contain_any_evaluation_prompt` exists because the fix for a
failing case is always, every time, tempting to solve by showing the model that exact case —
and the temptation recurs on every prompt iteration, not just the one that caused it.

The clean measurement, once both were fixed: **27 of 30 (90%) on Gemini** (`passed 27 ·
wrong 1 · unparseable 2`, latency median 2.6 s, zero throttling), and **43% on the local
`qwen2.5:7b-instruct-q4_0` model** — the documented baseline the ADR-040 hosted-inference
migration is measured against, not a number to be embarrassed by, since `--validate` staying
free of model quota is what makes closing that gap later a matter of iteration rather than
another rationed measurement.

---

### T-030 · Verify-before-deploy, and a gate that only existed in the template

Two ADRs, one finding. ADR-034 sets the actual policy this ticket asks for: a
compiler-generated policy activates only when **both** the auto-generated test suite *and*
the 51-case master corpus pass — the corpus catches a regression against the invariants
every policy must keep, the auto-generated tests catch the LLM misunderstanding the one
prompt it was just given, and neither alone covers what the other does.

Building the diff-and-verify UI for that dual gate is what turned up ADR-039. `POST
/policy/activate` assigned the new source and returned 200 **unconditionally** — no corpus
run, no auto-test run, not even a check that the Cedar parsed. `can_activate` was computed
and handed to the *template*, so the gate that ADR-030 and ADR-034 had both already decided
existed only as a greyed-out button a direct `POST` walked straight past. The one test that
touched the endpoint asserted it *accepted* a new policy — which is to say, the missing gate
had its own passing test, documenting the gap as intended behaviour.

Fixed by making the endpoint itself parse and run the corpus before assigning anything,
returning 409 with the failing case names on any failure, previous policy left serving.
Worth noting what it deliberately still does not call: `agentiam_pep.activation
.activate_bundle`, which additionally verifies an Ed25519 signature and a monotonic serial —
gates that belong to real bundle publication, which the console's `DummyBundleStore` stub
does not do yet. The corpus gate is the one with something to check today.

One sub-decision earned its own line in the ADR: an empty policy source defaults to `""`
rather than being a required field, because a required field makes `httpx`'s dropped-empty-
form-value behaviour diverge from a real browser's `source=`, and the two paths need to
agree on refusing with 409 rather than one of them 422ing before the gate ever runs.

---

### T-032 · Drift detection, and a header for a fact the PEP is not allowed to remember

Spec 06 + `drift.py` (ADR-035). The problem statement has a contradiction sitting inside it:
scoring how far a request has drifted from its stated task needs the English task text, and
the PEP is stateless by design — it never looked the task up anywhere, and holding one more
piece of per-task state would be exactly the kind of cross-request memory `PLAN.md` §3.2's
"hot path never blocks on the network" principle exists to prevent.

The token already carries `intent_hash`, cryptographically bound at mint time. The SDK sends
the plain English alongside it, in a new `AgentIAM-Task-Intent` header; the PEP hashes what
it received and checks it against the token's own hash before trusting a word of it. A
mismatch means the caller is asserting an intent the token was never minted with, and the
request is refused before drift scoring ever runs — the header is not the drift signal,
it is what makes trusting the drift signal safe. Only once the hash matches does the plain
text become input to the embedding-based semantic oracle.

---

*T-033's own two findings against T-032's shipped oracle — the cold-embedding timeout and
the per-call `httpx.Client` — are narrated in that ticket's own section above, in
chronological order with the rest of M4. What T-033 shipped alongside those fixes, the
drift-modes wiring, is its own ticket, T-036:*

### T-036 · Wiring `off` / `log_only` / `strict` through, so the extractor's own routes decide

The half of drift detection with no ADR of its own, because nothing about it was left open
by a spec: spec 06 already names three modes and their meanings, and T-036's job is
threading the choice from a route's own extractor configuration through the pipeline
context into `decide()`'s drift evaluation, so an operator can turn drift off for a route
it does not make sense for (a read with no task-relevant amount) without touching code. The
three modes now reach every layer that needs to see them — extraction, the request context,
and the decision itself — rather than drift being a single global switch that a busy
low-risk route could not opt out of.

---

## M5 continued — Escalation persistence, revocation, and the two performance layers under it

### T-037 · The escalation workflow, split into a pure half and a half that needs custody

The pure workflow — `agentiam_core.escalation`, `request`/`approve`/`deny`, "the grant is
always ⊆ the request" as the one property the approver cannot violate no matter what they
type — shipped as its own commit and stopped there on purpose. The persistence half needed
Postgres, FastAPI, and four things `PLAN.md` §8 gestures at without fixing (ADR-041), and
conflating "these rules are correct" with "here is where the signing key lives" would have
made the first claim wait on the second.

Four custody questions, each answered as a stated stopgap rather than a silent choice:

**Who signs an elevated token.** Approval mints a fresh root-signed `Mandate` — never an
attenuation of the agent's own token, since elevation is explicitly not narrowing. No
issuance service exists yet to hold that key, so `AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY`
reads a hex Ed25519 key from the environment, the same shape `agentiam_pep.config` already
established for its own settings. The module docstring says stopgap in those words.

**Who may approve.** No session identity exists before T-043, so `AGENTIAM_CONTROLPLANE
_APPROVERS` is a fixed config list and the caller names which approver is acting in the
request body — until T-043 replaces that with a real one.

**What the request body actually needs to carry.** `PLAN.md`'s sketch is what a UI form
would submit; a `Mandate` needs `task_id`/`principal_id`/`intent_hash` too, which the
escalating decision's own context supplies automatically when the PEP opens the escalation
itself, and which a human hitting the endpoint directly has to supply by hand.

**How deep an elevated token can delegate.** Nothing in the plan fixes a number.
`ElevationGrant` carries what was approved but not a depth, so `max_depth = 1` — one
direct use, no children — became `ControlPlaneSettings.elevation_max_depth`'s default: the
narrowest reading consistent with "elevation is not a new root of a delegation chain."

The concurrency property — exactly one approver wins a race for one pending escalation — is
proven the same way T-013's `ACQUIRE` and T-014's `LEDGER_COMMIT` were: `SELECT ... FOR
UPDATE` before the pure `approve()`/`deny()` call, and ten concurrent approvers racing one
row in the integration suite. And the same ADR-026 reasoning reappears in a new place: a
failed escalation write fails the *request* closed, because a system that cannot record
that a human was asked must not tell the agent one was asked.

---

### T-038 · Revocation reaches every PEP, and the first real dependency on Redis

`specs/07-revocation.md` — the last spec gap `PLAN.md` names — plus the `revocations` table,
`/v1/revocations`, and `RedisRevocationSet`, the PEP-side consumer keeping `decide()`'s
synchronous `is_revoked()` fed. Two ADRs, because the ticket forced both a dependency
decision and four implementation choices spec 07 left open on purpose.

**Redis had been running since T-001 and nothing had ever imported it** (ADR-042) —
`docker-compose.yml`'s comment named the job, but `import redis` failed against the venv
until this ticket. `redis-py`'s own `redis.asyncio` has shipped pub/sub and pooling natively
since 4.2, so no separate `aioredis` package is needed; that project merged upstream.
Crucially, none of this touches `agentiam_core` — the purity guard still holds, because
`RevocationOracle.is_revoked()` stays a synchronous in-memory `set` lookup on the `decide()`
side regardless of what feeds it from a background consumer on the other side of that
boundary.

**Push is the latency optimization, pull is the correctness backstop** — spec 07 already
said so, and it is proven rather than assumed: one integration test points the consumer's
push connection at a dead port and shows pull alone still converges; another stops the real
Redis container mid-revoke and shows the row still persists in Postgres regardless. NFR-4
(<2 s p99 propagation) is measured against 3 real `RedisRevocationSet` instances, not
mocked ones — across five runs, p99 ranged from ~11 µs to ~16 ms, with the one 12 ms sample
a scheduler artefact rather than the norm. Loopback-only, so the number is a floor, not a
network-separated deployment figure — that caveat travels with it into every later ticket
that cites it.

ADR-043 records the four choices spec 07 left implicit: the revoke endpoint reuses the same
`approvers` config list ADR-041 introduced for escalations, rather than inventing a third
identity stopgap; `revocations.seq` is a Postgres `IDENTITY` column rather than a
manually-locked counter, because `block_id UNIQUE` already carries the concurrency proof and
`seq` only needs to be monotonic, not contiguous; the staleness boundary is inclusive,
matching `Escalation.is_expired()`'s existing convention rather than inventing a second one;
and the channel name is a literal string in both packages rather than a cross-package
import, because the control plane and the PEP are separate deployables that have never
imported from each other anywhere else in the codebase.

---

### T-039 · The Bloom filter that would have made the cache slower than no cache

`PLAN.md` asks for a counting Bloom filter in front of `RedisRevocationSet`'s exact set, for
O(1) negative answers at EC-R10's scale (10,000 revocations). The obvious choice —
`pyprobables`, the conventional pure-Python implementation — was probed before being
adopted, the same habit that has caught nine design errors and a security finding earlier
in this project.

**It failed on measurement, not on API completeness.** Against 10,000 128-hex-char ids — the
real shape of a biscuit revocation id — `pyprobables` measured **~92 µs per lookup**, while
a plain Python `set.__contains__` over the same 10,000 ids measured **~0.76 µs**. The
"performance layer" would have been roughly **900–1,200x slower** than the exact set it was
meant to sit in front of — a net loss on every single call, directly contradicting the one
sentence in spec 07 that names the filter's purpose (ADR-044).

`fastbloom-rs`, Rust-backed via PyO3-style bindings, measured **~0.25 µs per lookup** at the
same sizing — close to the plain-`set` floor, and still a real counting Bloom filter with
working `remove_str`, verified rather than assumed, so `PLAN.md`'s literal word "counting"
is honoured and not merely gestured at. `rbloom` measured even faster (~0.10 µs) but has no
counting/removal at all, which would have needed its own justification for calling it
something `PLAN.md` did not ask for; `bloom-filter2`, pure Python, shared `pyprobables`'
order-of-magnitude problem. The filter sits *inside* `RedisRevocationSet` rather than beside
it — a negative returns immediately, a positive falls through to the existing exact set,
which stays authoritative regardless of what the Bloom filter says, so a false positive
costs one extra lookup and never costs correctness. A 10,000-id property test proves zero
false denials, including a reachability audit confirming real Bloom collisions actually
occurred rather than the test passing vacuously because no collision was ever generated.

---

### T-040 · Twelve agents, three subtrees, and choosing a tree shape that makes "unaffected" unambiguous

The subtree-revocation e2e that closes M8, and the point where every prior claim about
revocation gets exercised through a real `attenuate()`-built chain rather than synthetic
block ids. Three things `PLAN.md` line 1160 names as a criterion — "revoke root → 12 agents
fail within 2 s, a sibling subtree unaffected" — without saying how to build or measure it
(ADR-045).

**The tree is three independent depth-4 chains, not one branching tree.** `12 = 3 × 4`
either way, but three separate chains under one root mandate guarantee, *by construction*,
that a sibling subtree shares no block id with the revoked one below the root — so
"unaffected" has exactly one meaning to verify, rather than depending on which two branches
of a binary tree happen to be picked. **One oracle, not three**: T-039's own NFR-4 test
already proved multi-instance propagation; repeating that here would re-prove convergence
instead of extending the claim this module actually makes, which is that a real chain's
`revocation_ids` carries the ancestor ordering `decide()` depends on. **Proof runs at
`decide()` directly, not through the HTTP stack** — T-040's dependencies are T-039 and
T-011's `attenuate()`, not the gateway, and policy/budget are stubbed to always allow so a
deny can only come from revocation, proving exactly the mechanism the ticket names rather
than incidentally testing eleven scopes' worth of Cedar authoring.

Revoking the root denies all 12 through the real pipeline (`ANCESTOR_REVOKED` for
descendants, `TOKEN_REVOKED` for the revoked block itself); revoking one subtree's own block
denies its 4 while the other 8 stay `ALLOW` throughout — the explicit negative test
`PLAN.md` calls out, because over-revocation is also a bug and nothing about a passing
positive test would have caught it. Measured propagation: **11–79 µs**, same loopback-only
caveat as T-039's number.

---

## M9 — Keycloak, and the session that had been missing since T-037

### T-043 · Real login, and closing the gap T-037 stated and left open

ADR-041 named this ticket's target explicitly, four tickets earlier: `POST
/v1/escalations/.../approve` and `.../deny` trusted a request-body `approver` field "because
no session identity exists yet (T-043)." This is that closure, and the interesting content
is a library choice plus four things the running system said that the plan did not.

**`authlib`, not a hand-rolled authorization-code flow.** Rule 1 forbids writing crypto, and
verifying an ID token's signature against a JWKS — plus getting state, nonce and PKCE right
— is that kind of code regardless of how small it looks.
`authlib.integrations.starlette_client.OAuth` does discovery, the redirect, the code
exchange and JWKS verification; `types-authlib` is a mypy-only dev dependency rather than a
blanket `ignore_missing_imports`, because one method (`create_client`) is genuinely
untyped and the rest is not.

**Four things found by running Keycloak rather than assuming its behaviour:**

* `testcontainers.community.keycloak` was already in the venv but unusable —
  `python-keycloak` had never been installed, so `from keycloak import KeycloakAdmin`
  raised `ModuleNotFoundError`. Fixed via the `testcontainers[keycloak]` extra rather than
  installing `python-keycloak` directly, so the tested version and the installed version
  are the same one.
* Keycloak 26 marks its own login-flow cookies `Secure; SameSite=None` **unconditionally**,
  regardless of the realm's `sslRequired: none` setting. A real browser on `localhost`
  sends them anyway — the W3C Secure Contexts spec treats loopback as trustworthy, and
  every major browser implements the exception — so the actual login flow is unaffected. A
  bare `httpx` test client does not implement that exception, which only mattered for the
  test harness, worked around there rather than by changing `auth.py`.
* `KeycloakAdmin.create_user` cannot pin a caller-supplied id — Keycloak always
  server-generates one. Only a realm **import** can fix a user's `sub` in advance, which is
  why the demo's two approvers live in `deploy/keycloak/realm-export.json` rather than
  being created through the Admin API.
* `httpx.ASGITransport` implements `AsyncBaseTransport`, not `BaseTransport` — it cannot
  mount into a sync `httpx.Client`. The OIDC test drives one client across both the
  in-process app and a real Keycloak container via `mounts`, so every request in that
  module has to be awaited, including the ones hitting the real container.

**The session mechanism is wired unconditionally, independent of whether login itself is.**
`SessionMiddleware` installs whenever `session_factory` and `escalation_settings` are both
supplied — the same condition that already mounts the escalation router — while
`OIDCSettings` independently and only controls whether `/auth/login` etc. are mounted at
all. The effect: `approve`/`deny` demand a real session unconditionally as of this ticket,
with no code path left that reads `approver` from the body, but a deployment or a test can
produce that session by any means that yields a validly-signed cookie, without needing a
running Keycloak — the same "wired means it does the thing, unwired means it visibly
doesn't" shape ADR-041 used for the approver list one layer down.

No `principals` table gets built, on purpose: `principal_id` is derived as `kc:<sub>`
directly from the verified ID token, the same shape every `principal_id` in the codebase
already had before this ticket (a caller-supplied string), so a table with nothing yet
reading from it would have been schema for its own sake.

---

## M10 — The console screens, and the question each one answers: did I build the console, or the truth

Five tickets so far (T-050 remains), one shape: each screen is a new query over tables that
already exist, wired to data already proven correct somewhere else in the codebase — not a
second accounting of anything, and not new correctness logic dressed as a UI ticket. T-049,
the observability ticket in the same milestone, follows this section rather than breaking
its flow, and is its own kind of screen for the same underlying claim.

### T-045 · The identity tree, and the screen `ROADMAP.md` says to over-invest in

`db/tree.py` builds `TreeNode`/`TreeBudget`/`TreeDiff` entirely from tables that already
exist — `audit_records`, `budgets`, `revocations`, `escalations` — with no new persistence
layer. `GET /v1/tree/{task_id}` serves a snapshot, `GET /v1/tree/{task_id}/blocks/{agent_id}`
looks up a structured caveat block id already sitting in a decision record's JSONB rather
than parsing raw Datalog fresh each time, and `GET /v1/tree/{task_id}/stream` pushes diffs
over SSE every 3 seconds. Pool budgets — the ones with no `agent_id`, shared across
siblings — are assigned to the root agent at depth 0, since a pool budget belongs to the
mandate as a whole and the tree has to put it somewhere a viewer can see it. D3 in a Jinja
template, per `PLAN.md` §4.3's reasoning, animating on mint and revoke — `ROADMAP.md` calls
this "the most important screen in the demo... visual polish is the deliverable, not a
bonus," and it is the one screen this project treats that way explicitly.

### T-046 · The live decision stream, and a hang built into its own first draft

`db/decisions.py` + `decisions_api.py`: `GET /v1/decisions` pages the audit chain,
`GET /v1/decisions/stream` is the *same* query filtered in SQL and pushed over SSE — the
stream reads the ledger's own durable record rather than a side channel, so a `seq` cursor
gives replay for free and survives a PEP restart for nothing extra. The actual content of
the ticket is `explain()`: a refusal names the exact failing caveat inline —
*"block 2's budget_ceiling caveat refused it: spend_bdt 60000 exceeds 50000"* — rather than
the generic "denied by policy" every other IAM product already says, which is precisely what
this project exists not to do.

It also built its own hang and found it before shipping: `request.is_disconnected()` never
fires under an in-process ASGI transport, so an unbounded generator ran forever and hung its
caller — the same trap that had left T-045's own SSE test a bare `pass` rather than a real
assertion. `MAX_STREAM_S` now recycles the connection and emits a `recycle` event, which
turns out to be correct in production too: a proxy kills an idle stream eventually anyway,
and `EventSource` reconnects on its own with the cursor resuming rather than replaying.

### T-047 · The budget dashboard, and a gauge that is the invariant rather than a second copy of it

`db/budget_dashboard.py`: the spend gauge draws a pool's `total` as `committed + leased +
allocated + available` — exactly the four terms of the pool invariant spec 04 §2.1 already
states, so the gauge *is* a picture of the invariant, and a test asserts the four actually
sum to the total rather than trusting that drawing them side by side makes it so. Lease
utilization — `settled / granted` over active leases — exposes the stranding failure mode
spec 04 §7 exists to bound, which the spend gauge alone cannot see: a pool can look
perfectly healthy while a PEP sits on a lease it never spends. T-016's checker runs live as
the page's green/red lamp, and — the point worth stating plainly — the lamp is verified
**red** on a deliberately corrupted ledger, not only green on a healthy one, because an
indicator only ever observed green is indistinguishable from a light that is simply painted
on.

### T-048 · The audit explorer, exposing a proof rather than rebuilding one

`db/audit_search.py` + `audit_api.py`: a new JSONB-filtered search over `audit_records`,
newest first — deliberately the opposite order from T-046's `read_since`, because a search
result is a snapshot a judge reads top to bottom once, not a feed a client resumes with a
cursor. The custody view and the "verify chain" button call T-023's `custody()` and
`verify_chain()` directly. No new tamper-detection logic was written for this ticket,
because none was needed: those two functions were already proven against real tampering,
deletion, reordering and head truncation when the audit chain itself shipped. `DecisionEvent`
(T-046) gained an additive `task_id` field so a search hit can link straight into its task's
custody chain without a second round trip — T-046's own tests kept passing unmodified,
confirmed rather than assumed.

---

### T-049 · Grafana dashboards, and the trace that had nowhere to go

*Still M10 — `ROADMAP.md`'s own "Milestone 6" resequencing calls this same set of tickets
something else; `STATUS.md`, this journal, and every commit subject use `PLAN.md`'s
numbering throughout, and T-049 is no exception even though it is the observability ticket
rather than another console screen.*

Two dashboards, committed as JSON, provisioned automatically: Decisions (rate, outcome mix,
reason codes) and Budgets (per-mandate spend, lease utilization) — the reduced scope
`PLAN.md` §21 already accepted, two instead of four. `docker-compose.observability.yml`
brings up the collector, Tempo and Prometheus alongside Grafana, deliberately its own file
rather than folded into the main compose file, since neither the control plane nor the PEP
runs as a container anywhere in this repository yet.

**Metrics go straight from each app to Prometheus.** The control plane's new `/metrics`
joins the PEP's own, which has existed since T-018; only traces cross the OTEL collector.
That is a narrower reading of `PLAN.md` §4.2's architecture diagram than "everything flows
through the collector," and the reason is concrete rather than stylistic: nothing in this
codebase uses the OTEL metrics SDK, both dashboards' numbers are already computed by
existing query functions, and routing already-Prometheus-shaped data through OTLP and back
would be a translation with no reader who needed it in a different shape (ADR-047).

**The actual find, though, was in code that had shipped two milestones earlier.**
`DecisionEmitter.decision_span()` has existed since T-022, benchmarked at 5.58 µs, unit
tested in its own file — and never once called from production code. Grepped the whole
tree to be certain rather than assumed. The consequence: `current_trace_id()`'s very first
read, at the top of `authorize()`, had never had a real span to read in any environment this
code has ever run in, so every `DecisionRecord.trace_id` that has ever been written falls
back to the decision's own id rather than carrying an actual trace correlation handle. A
span that costs 5.58 µs and produces nothing is exactly the kind of guard this project's own
standing habit exists to catch, and it sat unnoticed because nothing was asking the question
"is this actually wired in" until a ticket needed traces to show up somewhere real.

Closed by opening the span in the gateway, before `authorize()` runs rather than inside it —
`Pipeline.request_span()` — and holding it open across the upstream `httpx` call in a nested
child span, `Pipeline.child_span()`. That ordering is also the literal fix for the ticket's
other requirement, "decision spans linked to upstream calls": before this, there had never
been an ambient span for an upstream call to nest inside even if one had been opened.
`configure_tracing` installs a real SDK exporter only when an endpoint is explicitly
configured, so the 5.58 µs figure — and NFR-1 alongside it — is unaffected in every test and
every deployment that has not opted in.

---

### T-050 · The escalation queue, and the row that was already an approve screen underneath

*Still M10 — the last ticket in it. `ROADMAP.md`'s "Milestone 6" ends here too.*

The backend for narrowing had shipped at T-037 and never been exercised end to end.
`agentiam_core.escalation.approve()` already took `narrowed_scopes` and `max_amount`,
already enforced grant ⊆ request in both dimensions (`NarrowingWidensRequest`, EC-A09);
`ApproveRequest` already carried both fields; `POST .../approve` already ran under a real
OIDC session as of T-043. Checked all three against the running code before writing anything,
per this project's own standing habit — the alternative was rebuilding logic that already
existed and was already tested at the core level (`tests/unit/test_escalation.py`), which
would have been the ticket's whole risk if skipped.

What was missing was narrower than "a UI": T-037's own console page rendered the queue
read-only with two buttons that posted empty approve/deny bodies — a screen that could only
ever grant exactly what was requested, never less. The gap was the narrowing *inputs*, not
the narrowing *logic*. `escalations.html` gained a checkbox per requested scope and an
amount field capped client-side at the requested amount (`max`, `min="0"`), and `approve()`'s
`fetch` body now carries whatever the row's own inputs say — `narrowed_scopes` from the
checked boxes, `max_amount` from the field. Unchecking every scope or typing an amount above
the request disables the row's Approve button before the request is even sent, but that is a
convenience, not the gate: `NarrowingWidensRequest` on the server is still what an approver
cannot bypass by editing HTML, exactly as EC-A09 requires.

Two integration tests exercise the amount-narrowing path end to end for the first time —
`test_narrowing_beyond_the_request_is_400` already covered scopes, but nothing had posted a
`max_amount` through the actual HTTP router and minted a token from it before. A third test
asserts the console page actually emits the per-scope checkboxes and the amount field with
the requested value as both `value` and `max`, so a future edit that silently drops the
narrowing controls (the same class of gap this ticket closed) fails a test rather than
waiting for someone to open the page.

---

## M11 — Red-team suite, and the count that turned out not to be the real number

### T-051 · The red-team suite, and which "15-20" turned out to be true

M10 closed clean at T-050; M11 opens here. `PLAN.md` §12 catalogues 33 adversarial
attacks; T-051's own summary line reduces that to "15–20 attacks covering key categories."
`ROADMAP.md` line 288 states the same reduction differently — as an explicit list:
`A-01…A-09, A-10…A-13, A-17…A-18, A-23…A-26, A-28…A-30`, plus `TM-19…TM-22` from
`threat-model.md` §6. Counted literally that is 22 attacks, not 15–20. The two documents
disagree, and per this project's own standing habit — a specific claim checked against the
running system beats a round number next to it — the explicit list won. That decision,
recorded as ADR-048, has one concrete consequence worth naming: **A-06 is in scope**
(bearer-token replay from a different agent, inside the `A-01…A-09` range), which the
prior session's handoff notes had asserted was excluded. That assertion did not survive
reading `ROADMAP.md`'s actual text. A-06 is tested and reported as `PLAN.md` §12 itself
already says to report it — **accepted risk**, not a failure — since bearer semantics are
the deliberate design (TM-01).

`TM-19…TM-22`, checked against `threat-model.md` §6 rather than assumed: TM-21 and TM-22's
reaper half were already closed at T-013/T-014, and TM-24 at T-011. Only TM-19 and TM-20
were genuinely still open, confirmed by grepping `tests/` for both ids before writing
anything (zero hits). TM-19's test is the more interesting of the two: it hand-builds the
*wrong* form of a Datalog check — `check if scope($s), $s == "invoice:read";`, written
against the token's own grant facts rather than the request — appends it to a real,
freshly-minted `biscuit_auth` chain, and shows it genuinely authorizes an operation
(`vendor:read`) the narrowing was never meant to permit. Only after that does it show the
actual shipped encoding (`to_datalog(ScopeSubset(...))`, which binds against the request's
`operation()` fact) refuses the identical request. Same shape as TM-24's `test_datalog_
labels.py` — probe the wrong design against the real library first, so the guard is proven
load-bearing rather than merely asserted correct. TM-20 turned out to need no new
mechanism at all: `RequestContext` already refuses to construct with a missing budget
dimension (`_every_dimension_present`, ADR-007), so the test is a `pytest.mark.
parametrize` over every `BudgetDimension`, confirming each one denies by refusing to even
exist, not by a runtime check that could someday be forgotten.

**Two files, not one**, for a reason that only became visible while writing them:
`tests/security/` carries no `conftest.py`, and pytest scopes a `conftest.py`'s fixtures to
its own directory tree — so `tests/integration/conftest.py`'s `postgres_url`,
`migrated_engine`, `redis_url` are not reachable from `tests/security/` at all. The four
named attacks that genuinely need a real database or Redis — A-17 (20-sibling swarm), A-18
(reserve, then abandon rather than crash, and let the TTL reaper reclaim it), A-29
(revocation converges even with the pub/sub push channel blocked), A-30 (rewriting a
*denied* payment into an *allowed* one is caught by the hash chain) — went into
`tests/integration/test_redteam_suite.py`, marked both `integration` and `security` so
`.\make.ps1 test-integration` runs them and `.\make.ps1 check` does not. Everything else,
plus TM-19/TM-20, is `tests/security/test_redteam_suite.py`.

**The same measurement habit that closed nine design errors from T-002 through T-014 found
three wrong tests in this ticket's own first draft**, all from one mistaken assumption:
that `VerifiedToken.scopes` reflects a chain's *effective*, attenuation-narrowed authority.
It does not — `verify()` reads `scopes` only from the authority block's own `scope(...)`
facts (spec 09 §4), so it is identical for a root token and every descendant it narrows,
no matter how many attenuation blocks sit between them. A byte-truncation probe for
A-02/A-03 surfaced it first: a truncated child token that happened to still verify
reported the *full* root grant, which briefly looked like a real widening bug until the
probe was re-run comparing `depth` (`block_count() - 1`) instead of `scopes` — the
narrowing block was never actually stripped, `.scopes` had just never reflected it in the
first place. The same mistake was already sitting in A-11 and A-12's draft assertions,
caught only because the probe forced a second look at what `.scopes` actually means. The
fix in all three: pass the caveats explicitly to `decide()` (exactly what a real PEP does,
reading them back from block content) rather than relying on the bare `VerifiedToken`.

Full check clean: 32 new unit tests, 4 new integration tests against real Postgres and
Redis containers (36.25 s), `ruff`/`ruff format`/`mypy --strict` clean across both new
files, and the existing 1956-test suite unaffected.

### T-054 · Security scanning + SBOM, and the log leak the scanner found in shipped code

`PLAN.md` line 1216 spells the acceptance criteria out flatly — bandit, pip-audit,
trivy, gitleaks in CI (clean or with documented waivers), an SBOM, and *"secret-
scanning test asserts no token, key, or PII in any log line at any log level."* Four
scanners and a test. The scanners are wiring; the test is the deliverable.

**The scanners went in first, because they cost nothing to run and set the bar for
what the test can rely on.** Bandit against `packages/` and `scripts/` produced 19
findings on its first run, all shape-matches on the codebase's own idioms: five B105
hits on `ReasonCode.TOKEN_*` enum values (the closed enum from `PLAN.md` §6.9 that
ruff already ignores via a per-file `S105` waiver), eight B101 asserts already
annotated `# noqa: S101`, one B106 on the throwaway Postgres password in
`run_load_test.py`, one B310 on a fixed-URL localhost health probe, one B404 subprocess
import, and four B603 `subprocess.Popen` calls with fixed argv. Configured as `skips`
in `[tool.bandit]` for the ruff-twinned ones (B101, B105, B106) and inline `# nosec
Bxxx` for B310/B404/B603 — matching what ruff had already documented per line.
`bandit -c pyproject.toml -r packages scripts` now reports **zero findings**.
`pip-audit --strict` against the resolved `uv.lock` also reports zero. Trivy and
gitleaks run in the CI job (they need binaries CI installs from action steps rather
than uv-installable Python packages), configured against `.trivyignore` (currently
empty) and `.gitleaks.toml` (allowlists the tracked `.env.example`, the demo Keycloak
realm export, the `TOKEN_*` reason codes and the two dev-only placeholder secrets).

**The SBOM was the finicky bit.** `pip-audit --format=cyclonedx-json` gave a first
result but it emitted random `bom-ref` values on every invocation, which broke the
`--check` mode the chaos and performance scripts already establish as the project's
habit — the committed file would go stale visibly with every regen even when nothing
had changed. Switched to `cyclonedx-py environment --output-reproducible`, which is
deterministic by design. `docs/evidence/sbom.json` is now CycloneDX 1.5, 136
components, and `python scripts/generate_sbom.py` in `--check` mode (default) exits
non-zero if the venv drifts from what is committed. Same shape as `generate_chaos_
results.py` and `generate_benchmark_results.py`; CI runs the same `--check`.

**The test is where the ticket found something in shipped code.** Two layers, and each
was worth building for a different reason. The static AST scan (`tests/security/
test_secret_scanning.py`, §A) walks every `logger.<level>(...)` call site in
`packages/**/*.py` — 25 sites, from `emitter.py`'s permanent-rejection error through
`nl_compiler/llm.py`'s Gemini/Groq retry warnings — and refuses to pass a positional
argument whose *variable name* is in a forbidden set (`token`, `private_key`,
`nl_statement`, `body_bytes`, ...). The runtime `caplog` layer (§B) drives the shape-
check-accepted sites and scans the emitted records at every level for forbidden
*content* — PEM headers, biscuit-shaped tokens (`{300,}` base64url chars), 64-hex-char
key material, e-mails, JWT-shaped triple-dot strings, Gemini and Groq API-key patterns.
Detector self-tests on both sides — `test_a_planted_bad_log_call_fails_the_scanner`
plants a `logger.info("token=%s", token)` in a scratch file and confirms it fires;
`test_the_runtime_scanner_would_actually_catch_a_leak` plants a JWT-shaped string and
confirms it fires — mirror the pattern `test_core_purity.py` established for
`agentiam-core`'s I/O-free rule. A guard whose reachability is not proven can be
silently disarmed by a refactor, and this test is a security guard.

**The finding, on the first run:** `agentiam_controlplane.nl_compiler.compiler.
compile_nl_to_policy` was logging the user's full natural-language statement verbatim
at INFO. A policy like *"Only alice@example.com approves >5000 BDT"* — the shape
`docs/DEMO.md` names as beat 5 — put an e-mail address into logs by design. Nothing in
`threat-model.md` had this as a threat; nothing in the red-team suite would have
caught it (the operator's own console typed it, not an adversary). Fixed by hashing
the statement (`sha256[:16]`, matching `arg_digest`'s shape from spec 10 §5.4) and
logging the raw length alongside. `test_compile_nl_to_policy_does_not_log_the_
statement_verbatim` pins the fix behaviourally; the AST scanner refuses any future
reintroduction of the pattern. **The static half saw it; the runtime half was written
because the static half cannot see a library's exception-chain rendering.** Both
layers are load-bearing.

`.env.example` also gets its own deterministic guard (`test_env_example_carries_only_
placeholders`) — the file is tracked deliberately (`.gitignore` has `!.env.example`),
and `CLAUDE.md` records that a real key nearly landed there once before. Gitleaks
allowlists the file wholesale; this test is the semantic half of that allowlist.

Full check clean: 9 new tests (8 security + 1 unit), one product fix in
`nl_compiler/compiler.py`, `pyproject.toml` grows three dev deps (bandit, pip-audit,
cyclonedx-bom) and a `[tool.bandit]` section, one new CI job (`security-scan`), two
new Makefile targets (`security`, `sbom`), the SBOM committed, and the existing
2011-test suite unaffected. `make check` clean, `make security` clean, `bandit -c
pyproject.toml -r packages scripts` reports zero issues with **8 `# nosec` suppressions
each with a rationale in `docs/evidence/security-scan.md`**.

---

## M11 continued — chaos, load, scanning, the evidence pack, deployment

These entries were kept in `STATUS.md`'s ticket ledger rather than written up here at the
time. Moved here verbatim when `STATUS.md` was cut back to current state.

### T-052

**Chaos suite** (M11; ADR-049, ADR-050) — `tests/chaos/` with the harness (invariant sidecar, load driver, JSON result per run), an in-process `asyncio` fault proxy standing in for toxiproxy, and `scripts/generate_chaos_results.py` regenerating `docs/benchmarks/chaos-results.md` with a `--check` mode so the committed table cannot drift from the runs. **All five scenarios pass** — 10 tests, 3m05s against PLAN.md §13's 20-minute budget. Two decisions were forced by probing before anything could be written: `testcontainers` reassigns a container's host port across `stop()`/`start()` (measured `:54423` → `:54429`), which would have made CH-1's recovery assertion vacuous, and the fault proxy's black hole must *freeze* established connections rather than close them, since the first draft's cut made SQLAlchemy's pool fail in 1 ms and `connections_blackholed` was 0 — the mode was inert. **What the suite found is worth more than the suite:** gap 23 (spent budget never reached the ledger — the headline correctness claim was false), gap 22 (a hung Ollama stalls the event loop), gap 21 (a partitioned PEP cannot shut down and a timeout does not bound it), gap 20 (a transient audit-sink outage treated as a poison batch — since fixed, ADR-051), and a fourth fixed in place: **a drained lease could never be refilled**, because top-ups are scheduled only from `reserve()` and `reserve()` runs only after `check()` allows — a closed loop that leaves a PEP dead after any outage outlasting its lease, which is CH-1 exactly. `check()` now schedules a top-up as it refuses. Gaps 20 and 23 and the drained-lease loop are fixed; 21 and 22 are measured, pinned by tests, and open

### Settlement path

**Settlement path** (ADR-049) — `agentiam_pep.settlement` + `agentiam_controlplane.db.settlement_sink` close gap 23. `Pipeline.settle()` now enqueues the `CommitOutcome` its own contract always said it must; a background queue applies each as `LEDGER_COMMIT` with **unbounded** retries, which is safe precisely because G4 dedups on `reservation_id`. `LeasePool` gained a `before_release` hook so a lease is never retired while it still owes the ledger — the second double-spend route, found by re-running CH-10 after the first fix and measuring 6,678 of 6,992 settlements declined. 7 integration tests, 1 new e2e assertion, both CH-10 scenarios

### T-054

**Security scanning + SBOM** (M11; ADR-054) — `bandit`, `pip-audit`, `trivy fs`, `gitleaks` all wired into the new `security-scan` CI job and into `make security`; four scanners clean at initial run, waivers documented in `docs/evidence/security-scan.md` and mirrored in `pyproject.toml [tool.bandit]`, `.gitleaks.toml`, `.trivyignore`. SBOM (CycloneDX 1.5, 136 components) regenerated by `scripts/generate_sbom.py` in `--output-reproducible` mode and committed to `docs/evidence/sbom.json`; CI asserts the committed file matches the resolved venv (same `--check` habit as `chaos-results.md` and `performance.md`). **The secret-scanning test is the deliverable that mattered.** `tests/security/test_secret_scanning.py` combines a static AST scan of every `logger.<level>(...)` site in `packages/**/*.py` (refuses positional arguments in a forbidden name set — `token`, `private_key`, `nl_statement`, `body_bytes`, ...) with a directed `caplog` capture that regex-scans the emitted records at every level for PEM headers, biscuit-shaped tokens, e-mails, hex key material and Gemini/Groq API-key shapes. A third check pins `.env.example` to placeholders only. **One real leak found in shipped code:** `nl_compiler.compiler.compile_nl_to_policy` was logging the user's full natural-language statement verbatim at INFO — a policy like *"Only alice@example.com approves >5000 BDT"* landed in logs by design. Fixed by hashing (`sha256[:16]`) and reporting length; the AST scanner now refuses any future reintroduction, and `test_compile_nl_to_policy_does_not_log_the_statement_verbatim` pins the fix behaviourally. 9 new tests (8 security + 1 unit); zero pre-existing lint/type/test regressions. **Correction, found post-merge, in two rounds**: the SBOM `--check` had actually been failing in CI since this exact commit. Round 1 (DECISIONS.md ADR-056 addendum, Part 5): `json.dumps(sort_keys=True)` sorts object keys, never array order, and `cyclonedx-py environment`'s component order follows filesystem enumeration, not guaranteed stable across two independently-built venvs — fixed by sorting `components`/`dependencies` explicitly. That fix was necessary but insufficient; pushed and CI failed again. Round 2 (ADR-056 addendum, Part 6), found by reproducing in a throwaway container matching CI's OS rather than guessing further: the SBOM also embedded this machine's absolute checkout path (`file://` `externalReferences` on the five local workspace packages — never portable, fixed by stripping any `file://` reference), and a `required-extra` property on five components that a fresh Ubuntu build sets and this Fedora dev machine's build — even freshly rebuilt from scratch — does not; not fully root-caused beyond "tracks the host OS," practically fixed by requiring the file be regenerated only from a Linux x86_64 environment matching CI, verified reproducible across two independent such builds plus a third clean `--check` pass

### T-053

**Load test, published numbers, and two corrections to what we were claiming** (M11; ADR-052, ADR-053) — `tests/perf/test_pipeline_breakdown.py` (PB-2), `scripts/serve_pep.py` (the composition root the project did not have — every prior assembly lived in a test), `scripts/serve_tools.py`, `tests/perf/driver.py` (coordinated-omission-aware), `scripts/run_load_test.py` and `scripts/generate_benchmark_results.py`, all folding into `docs/benchmarks/performance.md` with `--check` for CI. **Correction 1: NFR-1's published ~5 µs measured `decide()` with `FakePolicy`** — a stub returning one fixed verdict — so it excluded the most expensive thing in the path. Against the real Cedar engine it is **145 µs median / 279 µs p99**, still well inside the 1 ms budget; policy evaluation is nearly the whole decision and `verify()` (209 µs) is the most expensive step and sits *outside* `decide()`. **Correction 2: NFR-2 is not demonstrated.** Three tiers at every rate separate the proxy hop from enforcement; running each profile three times (§13.1) showed enforcement p99 ranging **1.75–74.7 ms at 100 RPS**, straddling the 8 ms budget by 10x in each direction, and the 500 RPS profile cannot be offered here at all — the *stub upstream alone* achieved 138 RPS at p50 335 ms. Reported as not established rather than quoting the flattering run. Flame graph committed (py-spy, attached by PID with `--subprocesses`). **Settlement batching** (ADR-053) closes the throughput item ADR-049 parked here: `ledger_commit_batch` settles a lease's reservations in one transaction, `ledger_commit` becomes a batch of one so the eight existing race tests cover it, and refusals under CH-10's aggressive load fell from 7,053 of 21,006 (33.6%) to **22 of 1,083 (2.0%)**

### T-055

**Evidence pack generator** (M11; ADR-055) — `scripts/generate_evidence_pack.py` folds `chaos-results.md` and `performance.md` verbatim (by importing and calling the sibling generators' own `render()` rather than re-deriving), `security-scan.md` and `threat-model.md` verbatim, and hardcodes the formal invariants table (INV-1..INV-10, spec 03 §6), the PB-1..PB-12 coverage table, and the A-01..A-33 red-team table (`PLAN.md` §13.1/§12) — all three checked against independently-known facts, not only against themselves. Output is a single self-contained HTML file (no new dependency; a PDF library needs either a native toolchain this project has no other use for or its own ADR, and a browser already prints HTML to PDF for free), `--check`-verified in a new CI job needing no live infrastructure. **Honest about what is not built**: the drift model card, a `mutmut` run, and OSS traction each get a section stating plainly that the underlying work has not happened, with a citation to what is measured instead, rather than an invented substitute. **Two gaps found and one closed while building it, neither belonging to this ticket**: `make.ps1` was missing the `security`/`sbom` targets `T-054` added to the `Makefile` only — closed here, per `ADR-003`'s own rule that a target added to one and not the other is a drift risk. `docs/benchmarks/performance.md` was stale at HEAD relative to its own committed source JSON, with nothing in CI, the `Makefile`, or `make.ps1` ever calling `generate_benchmark_results.py` to catch it — the stale file is resynced (mechanical, no new benchmark run), but no CI gate is added, because PB-2's raw timings vary run to run and a byte-exact check would fail on ordinary noise; recorded as gap 24 rather than decided unilaterally on `T-053`'s behalf

### T-056 Part 1

**Deployment composition roots** (M11; ADR-056) — `scripts/pep_service.py`, the PEP's first production entry point: env-configured, seeds nothing, refuses to boot without every credential and bundle it enforces with. Found three "never assembled outside a test" gaps by grepping constructor calls rather than assuming: `RedisRevocationSet` (both prior reference assemblies use `InMemoryRevocationSet()`, which never revokes anything — T-038/T-039's push/pull consumer and Bloom filter had never run in an assembled PEP) — **required**, closed here. `RuleBasedDriftOracle` (chaos-test helper only) — closed in Part 2, see below; Part 1's own first draft claimed this was already fixed and was corrected before it could compound (ADR-056 addendum). And `PolicyCache` (had never met a `Pipeline` — wiring it naively would have raised `AttributeError` on the first decision record, since it exposes `.bound()` but no `.bundle`; shipped instead as a signed bundle loaded once from disk, `POLICY_BUNDLE_STALE` therefore unreachable in this configuration, gap 26). `agentiam_controlplane.app` gains `/healthz`, `/readyz` and `create_app_from_env()` — it had no health endpoint at all, which blocked both `compose --wait` and k8s probes; the existing module-level `app` (T-027's console, database-less by design) is left untouched, not removed, after a first grep wrongly suggested nothing imported it. `agentiam_pep.app.create_app` gains a `lifespan` parameter, replacing `@app.on_event` (deprecated in FastAPI 0.141) for new callers only. `LICENSE` added, byte-identical to apache.org's canonical Apache-2.0 text (sha256-verified) — closes gap 3. **Two cross-platform defects found by actually running `docker compose up` on Fedora, both invisible on Docker Desktop**: none of six bind mounts across the two compose files carried an SELinux label, so Keycloak crash-looped (`ERROR: directory not found`) on any enforcing host and `make up` never reached healthy; and Keycloak 26 only imports a realm file named `<realm>-realm.json` — both the compose mount and `testcontainers`' `with_realm_import_file` used the wrong name, silently importing nothing while logging "Import finished successfully". This is why T-043's three OIDC integration tests had been failing (confirmed pre-existing by stashing and reproducing on clean HEAD); both are now fixed and passing. Verified live: `make up` reaches healthy in 23 s against NFR-8's 90 s three-service floor. **Found, not fixed**: the PEP is scoped to exactly one mandate (`LeasePool` binds `mandate_id` at construction, `Pipeline.reserve()` takes none) — a real limitation for a general deployment, recorded as gap 25 rather than fixed unilaterally, since correcting it touches the money hot path and ADR-049's settle-before-release invariant. 44 new tests

### T-056 Part 2

**Container image, one-command demo bring-up, NFR-8** (M11; ADR-056 addendum) — `Dockerfile` (one image, three entrypoints per ADR-056 5.1), `docker-compose.demo.yml` (an overlay on `docker-compose.yml`, the same pattern `docker-compose.observability.yml` established), `scripts/bootstrap_demo_secrets.py` (generates the root keypair and signs `agentiam_core.corpus.CORPUS_SOURCE` — the real 51-case-tested policy, reused rather than invented — idempotently, since nothing in this repository publishes a bundle, ADR-039). **Corrected Part 1's own overclaim first**: `RuleBasedDriftOracle` had been documented as fixed and was not wired at all; now optional and configurable (`AGENTIAM_PEP_OLLAMA_URL`), warmed via `asyncio.to_thread` rather than blocking the loop, `decide()`'s own contract making "unconfigured" a safe default rather than a silent gap (spec 06 §2.1) — the opposite posture from revocation, where the fallback is a security hole. **Two more defects found only by actually running the stack**: `alembic.ini`'s `script_location` resolves against alembic's cwd, not the ini file, so the image uses an editable workspace install (not `--no-editable`) to keep `src/` physically present for the migration container; and a fresh **named** Docker volume mounts `root:root`, so the non-root `bootstrap` container (uid 1000) got `PermissionError` until the image pre-created `/secrets` with the right ownership — a different mechanism from Part 1's SELinux findings (those were host bind mounts; a named volume has no SELinux label to begin with, measured) but the same class of defect: something that only breaks under a non-root container, which nothing in this repository ran as before this ticket. Ollama is an opt-in Compose profile, not part of the default bring-up (several-GB model pull; the hosted LLM backend is ADR-040's default). **Verified live, twice, past "healthy" into actual behaviour**: `make demo-up` reaches every service healthy in **20 s and 19 s** across two cold runs against NFR-8's 90 s budget, with the control plane's `/readyz` reporting `database`/`escalations`/`revocation_publisher` all `true`, the PEP's `/readyz` reporting `enforcing: true`, and an unauthenticated proxy call returning a real `401` through verify → revocation → policy → lease inside the built image — the first time this project's whole pipeline has run assembled, in a container, against real Postgres and Redis. A `demo-stack` CI job repeats the same three checks on every push. 27 new tests. **Correction, found post-merge when that CI job actually ran**: `PEP_PORT` defaulted to 8080, same as `docker-compose.yml`'s own `keycloak` service — a real collision (`Bind for 0.0.0.0:8080 failed: port is already allocated`), invisible in every local verification above because each one used a non-default `PEP_PORT` to work around an unrelated pre-existing port-8000 collision on the dev machine, which incidentally also dodged this one without testing the shipped defaults. Fixed: `PEP_PORT` now defaults to 8082; re-verified live against the actual shipped defaults (DECISIONS.md ADR-056 addendum, Part 5)

### T-056 Part 3

**k3s manifests** (M11; ADR-056 addendum) — `deploy/k3s/` (Namespace, Postgres/Redis Deployments+PVCs, a `migrate` Job, `controlplane`/`pep`/`tools` Deployments, `kustomization.yaml`), showing the same deployable shape as `docker-compose.demo.yml` on a real orchestrator, no Helm. `controlplane`/`pep` each carry a `wait-for-migration` initContainer that polls `information_schema.tables` for the `budgets` table via `asyncpg` directly, re-deriving Part 1's own lesson: `make_engine()` is lazy, so `/readyz` would report `database: true` against an unmigrated schema. `secretKeyRef` replaces Compose's shell-wrapper workaround — a genuine platform capability difference, not a style choice. Keycloak and Ollama are deliberately absent, matching Part 2's posture stated explicitly for k8s. `tests/unit/test_k3s_manifests.py` (25 tests) cross-checks every env var name against the real `ServiceSettings.from_env()`/`ControlPlaneSettings.from_env()` and every `secretKeyRef` key against `bootstrap_demo_secrets.py`'s real output, never retyped. **Measured: `kubectl apply --dry-run=client --validate=strict` does not catch a deliberately broken field on this kubectl/server version (v1.36.1)** — only `--dry-run=server`, needing a reachable API server, does real schema validation; both the finding and the reproduction are in `deploy/k3s/README.md`. **Live-deployed once, for real, against a pre-existing `kind` cluster on this host** — the same standard of proof Parts 1–2 set for Compose. Found and fixed one real bug this way: every `agentiam:latest` container needs `imagePullPolicy: IfNotPresent` explicitly, since Kubernetes defaults `:latest` to `Always` regardless of local presence, and there is no registry to pull from — 7 sites fixed, pinned by a new regression test. Two more findings while getting `postgres`/`redis` into this specific node (an IPv6 egress timeout and a local multi-arch manifest-list gap) were host-environment artifacts, not AgentIAM defects, worked around via a direct `ctr images pull` and documented so a future run on different infrastructure isn't blocked by the same dead end. After the fix, every pod reached `Running`/`Completed`, the migrate Job completed, `controlplane`'s `/readyz` reported `database: true` against the real schema it had created, and an unauthenticated proxy call returned a genuine `401` through the real route table and biscuit-token gate — verify, policy and lease all exercised for real inside the cluster. All test resources deleted afterward; the host's pre-existing `demo` namespace was confirmed untouched. 26 new tests. **Correction, found post-merge (DECISIONS.md ADR-056 Part 7)**: `trivy fs`'s Kubernetes misconfig scanner had never actually reached `deploy/k3s/` in CI — blocked by the unrelated SBOM failure two steps earlier in the same job (Parts 5/6) — and once it did, correctly flagged all 9 containers for not setting `securityContext.readOnlyRootFilesystem: true`. Fixed by setting it everywhere; `postgres`/`redis` needed real `emptyDir` mounts (`/var/run/postgresql` + `/tmp`, and `/tmp` respectively) to actually start under it, confirmed by redeploying to the real `kind` cluster and running real reads/writes, not just a healthy pod status. 27 tests total now

### T-056 Part 4

**Signed images, SBOM attestation, rollback procedure** (M11; ADR-056 addendum — **T-056 and M11 complete**) — `.github/workflows/release.yml`: tag-triggered only (`push: tags: v*`, never on an ordinary push to `main`, per this ticket's own locked planning decision), builds and pushes to GHCR, signs **keylessly** with `cosign` (short-lived GitHub OIDC token → Fulcio certificate → public Rekor log, no private key material anywhere in this repository), attests `docs/evidence/sbom.json` (T-054's own file, not a second SBOM) as a CycloneDX predicate, and verifies both the signature and the attestation in the same job immediately after publishing. Every action version pin was checked against the real GitHub API before being written down — the same discipline that caught the missing `v` on `trivy-action` in T-055 — rather than guessed. `docs/deployment.md` (new) covers image provenance and a rollback procedure grounded in gap 21's measured shutdown-hang finding: don't raise `terminationGracePeriodSeconds` past the shipped 30 s default during a rollback, since that only delays resolving a pod already stuck past a measured five-minute hang rather than protecting anything. **Found while writing that rollback section, not while reading: gap 21's own recovery claim — "the lease expires and `REAP` reclaims it" — is true only inside the chaos harness.** Grepping every non-test caller of `reap()` found none anywhere in the deployed system; spec 04's own pseudocode prescribes running it on a schedule, and nothing does. Confirmed the function itself is sound first (a throwaway Postgres, a real migration, the exact snippet now in the runbook reclaimed a real stranded lease cleanly) before concluding the gap is purely the missing scheduler. Recorded as new gap 27, gap 21's text corrected rather than left overstating a fact that was not true, and deliberately not fixed inside this ticket — a scheduled reaper is a money-hot-path addition needing its own design, the same posture Part 1 took with gap 25. **What was not possible to verify**: the release workflow has not run against a real tag (a visible, hard-to-reverse action needing explicit confirmation, not taken unilaterally while documenting a different ticket), and a local `cosign` CLI dry run could not complete either — this sandbox's network egress could not reach GitHub's release-asset host. Stated plainly in `docs/deployment.md` §1 rather than overclaimed

---

## After M11 — the fix queue (formerly `docs/TODO.md`, items 1–34)

Work queued out of the manual end-to-end passes of 2026-09-07 and 2026-09-08
([`manual-test-report.md`](manual-test-report.md), [`RUNNING.md`](RUNNING.md)), including
T-057's demo seeding (item 7). Commit messages and the test report cite these as
"TODO item N"; the numbering is unchanged. Moved here verbatim once every item was either
closed or carried into `STATUS.md` §5 — items 21, 23 and 30 are the ones still open.

### P0 — the core claim (resolved, with one new defect behind it)

#### ~~1. Wire caveat evaluation into the deployed PEP~~ — **DONE**

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

#### ~~3. Correct STATUS gap 2's impact statement~~ — **no longer needed**

Gap 2's text — a *reporting* limitation — is now accurate again, because item 1 closed the
enforcement half by a different route. Worth a line in `STATUS.md` recording that biscuit's
authorizer is what enforces attenuation, so the next reader does not re-derive it.

#### ~~3b. The mandate's budget check was existential~~ — **DONE**

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

### P1 — visible in the demo

#### ~~2. Build the Datalog→caveat parser (STATUS gap 2)~~ — **DONE**

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

#### ~~4. Give agents real identities instead of `agt-depth-{N}`~~ — **DONE**, minus one field on purpose

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

#### ~~5. Make the identity tree fail loudly, and render regardless~~ — **DONE**

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

#### ~~6. Make lease size configurable~~ — **DONE**, and the second half deliberately not

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

#### ~~7. Build T-057's demo seed script~~ — **DONE**

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

#### ~~8. Test that the deployed PEP primes its lease pool~~ — **DONE**

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

#### ~~9. Schedule `reap()`~~ — **DONE** (STATUS gap 27)

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

#### ~~10. Audit authentication and routing refusals~~ — **DONE**, as the alternative

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

#### ~~11. Fix the status codes~~ — **investigated, no change. I was wrong.**

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

#### ~~12. The SBOM's platform dependence~~ — **DONE**, but not the way I filed it

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

#### ~~13. Give `performance.md` a CI drift check~~ — **DONE** (STATUS gap 24)

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

### P3 — verification gaps in this pass

#### ~~14. Verify the narrowing-only approval invariant against a live request~~ — **DONE**

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

#### ~~15. Exercise the console pages that this pass could only check over HTTP~~ — **DONE**, and it found two bugs

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

### Found since

#### ~~16. A duplicate escalation returns 500, not 409~~ — **DONE**

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

#### ~~17. The authority block's own budget ceiling reports `BUDGET_EXHAUSTED_CAVEAT`~~ — **DONE**

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

#### ~~18. The root node in the identity tree has no name of its own~~ — **DONE**

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

#### ~~19. Two log-assertion tests go vacuous when every suite runs in one process~~ — **DONE**, and it was our bug

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

#### ~~20. `performance.md`'s numbers do not include the block-source parse~~ — **DONE**, as the documented alternative

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

#### 21. Re-measure NFR-2 — **half done**: the harness now matches, the numbers do not yet

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

#### ~~22. Both nightly CI failures~~ — **DONE**

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

#### 23. One unexplained integration-job failure, watch for a second

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

### Found in the second manual pass (2026-09-07)

A hand-driven pass over the running demo stack — real biscuits, real Postgres, real Cedar,
real Chrome. 33 control-plane cases, 18 PEP cases, 7 console pages, plus revocation, audit,
custody, SSE, metrics and the activation gate. Written up in
[`manual-test-report.md`](manual-test-report.md) §8. Five anomalies; the first is critical.

#### ~~24. The PEP stops authorizing every budgeted request 60 seconds after boot~~ — **FIXED**

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

#### ~~25. The SDK's own intent header can never match a demo-minted mandate~~ — **FIXED**

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

#### ~~26. `email:send` is unroutable~~ — **FIXED**, and it uncovered item 29

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

#### ~~27. Chain-of-custody on an unknown task returns 200 with an empty list~~ — **FIXED**

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

#### ~~28. `DEMO.md`'s F-2 drill scripts a failsafe that does not exist~~ — **FIXED**

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

#### ~~29. The deployed PEP has an empty tool catalogue — and cannot vary `principal.role` anyway~~ — **FIXED**, and the investigation is what made it possible

Closed by ADR-069. The investigation stands as written — both defects were real, and the
second is why the first could not simply be wired — but its conclusion that option (b) needed
an issuance service was one step too strong.

**What shipped.**

1. **The catalogue travels inside the signed bundle.** `PolicyBundle` grew a `tools` field,
   `signing_payload` covers it, and `CedarEngine` reads its catalogue from the bundle it was
   handed unless a caller passes one explicitly. `pep_service.load_policy` passes none, so the
   deployed engine cannot be running a policy about `resource.sensitivity` against a catalogue
   that never arrived — there is no second artifact to forget, and one serial names both.

   Signed rather than mounted beside the bundle because `sensitivity` is an authorization
   input: downgrade `payment_api` from `critical` to `low` on disk and the forbid is disarmed
   *silently*, since every request still returns a decision. Same threat `verify_bundle`
   already closes for `cedar_source`.

2. **Roles are asserted per agent, by delegation path.**
   `AGENTIAM_PEP_ROLE_ASSIGNMENTS_PATH` reads a JSON object of path to role. ADR-057 is
   untouched: the role still comes from the organization and never from the token. What
   changed is the *arity* — configuration could previously say one word for the whole
   process, which is what made a role-discriminating policy always-on or always-off.

   **Keyed on the path and not the `agent_id`**, because the id is written by the delegating
   parent: keying on it would let any agent name its child `agt-payer` and collect
   `agt-payer`'s role, which is ADR-057's own attack one field over. A path can only be forged
   by an agent already on it. The one residual — an ancestor assigned less than its own
   descendant could mint that descendant — is refused at boot by
   `pep_service._refuse_widening_roles`, because at request time the forged chain *is* a real
   chain.

3. **`AGENTIAM_PEP_DEFAULT_ROLE` is finally read.** `default_role`'s docstring called itself
   configuration and `serve_pep.py` names the variable in a comment; `from_env` never looked
   it up, so the only role a deployed PEP could assert was the dataclass default.

**Measured against the real bundle**, which is the table the investigation asked for, extended
with what now runs:

| operation / tool | catalogue absent (as deployed) | wired, `role=agent` | wired, per-agent roles |
|---|---|---|---|
| `invoice:read` via `invoice_api` | allow | allow | allow |
| `vendor:read` via `vendor_api` | allow | allow | allow |
| `payment:initiate` via `payment_api`, depth 1 | allow | **DENY** | allow (payer is senior) |
| `payment:initiate` via `payment_api`, depth 3 | DENY | DENY | DENY (on depth, as intended) |
| `email:send` via `email_internal` | allow | allow | allow |
| `email:send` via `email_external` | **allow** | DENY | DENY |

Every demo beat behaves exactly as it did; both resource-attribute rules are now live. The
`email_external` row is the second inert rule and the one item 26 could not have known about:
ADR-067 split `email:send` into two routes so `is_external` would have something to
distinguish, and the attribute was never reaching Cedar.

**Verified live on a rebuilt demo stack**, and the negative half is the part worth having.
With the role map as shipped, all 13 scenario calls land on their intended outcome:

```
allow 200  payer settles a small invoice                OK
allow 200  settlement agent pays within its slice       OK
deny  429  settlement agent exceeds its ceiling         BUDGET_EXHAUSTED_CAVEAT
deny  429  root attempts more than the mandate grants   BUDGET_EXHAUSTED_MANDATE
deny  403  sub-contractor is too deep for the policy    POLICY_DENIED
```

Then the same stack with `/secrets/role_assignments.json` replaced by `{}` and the PEP
restarted — every other input identical:

```
deny  403  payer settles a small invoice                POLICY_DENIED
deny  403  settlement agent pays within its slice       POLICY_DENIED
```

That is the forbid firing in a deployed PEP, which it had never done. It also proves the two
halves are both load-bearing: the catalogue is what makes `payment_api` critical, and the role
map is what lets the payer lineage through it. Restored and re-driven afterwards to confirm
the stack returns to the first table.

**The demo's assignment is the payer lineage and nothing else** — `agt-payer`,
`agt-payer/agt-settlement`, `agt-payer/agt-settlement/agt-subcontractor`, which is exactly who
`seed_demo` grants `payment:initiate`. `agt-doc-reader` and `agt-negotiator` are *not* senior,
which is the difference between this and option (a)'s "set the demo's role to senior".

**What is still open, and it is the honest remainder:**

- **No demo beat exercises the discrimination end to end.** The only agents holding
  `payment:initiate` are the ones the organization made senior, and `agt-doc-reader`'s payment
  attempt dies at its own scope subset before Cedar is consulted. The rule is live and
  corpus-tested; the scenario does not show it. Making it visible means giving some non-senior
  agent the payment scope — demo *content*, T-057's call, filed as item 30.
- **This is not the issuance service.** Roles are static until an operator edits the file and
  restarts; nothing rotates, expires or attests them. `STATUS.md` gap 7 stands, and the
  path-keyed map is what is available without it.

---

### Found while closing item 29

#### 30. No demo beat shows the role discrimination the corpus asserts

**Demo content, not a defect.** `forbid_critical_tool_payment_worker` (beat 3) and
`forbid_critical_tool_payment_senior` (beat 8) are both enforced by the deployed PEP as of
item 29, and neither is visible in the scenario: every agent that can reach `payment_api`
holds a role the organization made senior, and the one non-senior agent that tries to pay
(`agt-doc-reader`) is refused by its own scope subset several steps earlier.

**What to do:** give `seed_demo` a fourth child that holds `payment:initiate` and is left off
`bootstrap_demo_secrets.ROLE_ASSIGNMENTS`, then a `_TRAFFIC` line where it attempts a payment.
The refusal is `POLICY_DENIED` naming the sensitivity forbid — a distinct beat from the depth
refusal `agt-subcontractor` already provides.

**Done when:** the console shows a payment refused because the agent's organization-asserted
role does not permit a critical tool, and `DEMO.md` beat 3 or 8 points at it.

**Weigh first:** it is a fourth sibling in a tree the docs describe as three, so `DEMO.md`
beat 2's "root spawns 3 sub-agents" and the identity-tree screenshots move with it.

#### ~~31. The first payment on an idle stack is refused~~ — **FIXED**

**Item 24's remaining half**, found while verifying item 29 live. Item 24 fixed the case where
an aged-out lease scheduled *nothing* and the PEP refused budgeted requests permanently. What
was left: the scheduling is asynchronous and `check()` has already decided by the time the
replacement lands, so **the request that notices the expired lease is still refused**. Every
top-up was request-triggered, so something has to be refused to trigger the renewal that would
have prevented it.

Invisible on a PEP that spends steadily — the low-water mark fires long before anything
expires. On an idle one it is the whole demo: the deployed PEP primes one lease at boot with a
60 s TTL, and a presenter is idle for far longer than a minute between bring-up and the first
payment. Measured on the demo stack, 80 s after `up --wait`, same tokens both times:

```
payer settles a small invoice           429 LEASE_UNAVAILABLE
settlement agent pays within its slice  429 LEASE_UNAVAILABLE
-- immediately again --
payer settles a small invoice           200 OK
settlement agent pays within its slice  200 OK
```

Both automated paths miss it for the same reason item 24 was missed: `make demo-seed` runs
seconds after `up --wait`, inside the first TTL, and every load run spends promptly.

**Fixed by ADR-070.** `LeasePool.start()` sweeps every `ttl / 4` — spec 04 §4.6's `REAP`
cadence — and replaces any lease past **half** its TTL. Half, not "once stale", because the
margin has to be wider than the skew or the replacement lands after `check()` has already
begun refusing; `PoolSettings` refusing `ttl <= 2 * skew` is what guarantees that. Renewal goes
through `_acquire`, the same call a top-up makes, so ADR-049's settle-before-release ordering
is not a second code path — a test pins that ordering for the renewal specifically.

Wired into the PEP's lifespan next to the emitter, the settlement queue and the revocation
consumer, and into `serve_pep.py` because ADR-062 requires the load harness to compose the PEP
the way the deployment does.

Verified live: after the fix, a stack left idle well past the TTL authorizes the first payment
it is asked for.

---

### Found in the third manual pass (2026-09-08) — a full sweep of the running stack

48 probes across the PEP's proxy, its auth and intent gates, every console page, every
`/v1` API, the SSE stream, the escalation flow, revocation, the activation gate and both CLI
tools. **Everything behaves as specified except item 32.** The other two entries are
operational truths about the demo that no code change fixes and every runbook needs.

Recorded because the sweep is only worth as much as its record: `docs/RUNNING.md` is the guide
it produced, with the expected output for each surface.

#### ~~32. The PEP authenticated a request with no `Bearer` scheme~~ — **FIXED**

`Authorization: <token>`, no scheme, returned **200** against a live PEP. The extraction was:

```python
token_text = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
```

so a header carrying no scheme fell through to the bare-token branch.

**Nothing exploitable followed** and nothing depended on it: the token still had to verify
against a root key, and `SdkClient`, both reference assemblies, the load driver and every test
send `Bearer`. What it was, is an undocumented and untested tolerance in the one step whose job
is deciding whether a caller is who they say — in a codebase where every deviation is written
down and justified. RFC 6750 §2.1 makes the scheme part of the credential, so the strict
reading is also the fail-closed one.

Now refused with `MALFORMED_REQUEST`, which names what to fix. Three tests pin it: no scheme is
refused, a lowercase `bearer` still works (RFC 6750 makes the scheme case-insensitive), and
`Bearer` with no credential is refused.

#### 33. The demo's tokens expire 8 hours after seeding — **operational, no fix intended**

`seed_demo._mandate` sets `expires_at = now + 8 hours`. Past that every call returns
**401 `TOKEN_EXPIRED`**, which is the token layer working exactly as designed — and it is also
the single most likely way to find a dead demo, because a stack brought up the evening before a
morning presentation is outside the window.

Found the blunt way: a sweep against a stack up for 20 hours returned `TOKEN_EXPIRED` for all
six agents on every route.

**Not a defect and deliberately not "fixed" by widening the window** — a mandate that never
expires is a worse product than one a presenter has to re-seed. The fix is procedural and lives
in `docs/RUNNING.md`: re-seed before presenting.

```
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm --no-deps seed python scripts/seed_demo.py --out /secrets
```

Idempotent for the mandate and the budget row; it re-mints the six tokens.

**Worth considering if the demo grows a pre-flight check:** `DEMO.md` §6's checklist has no
step that would catch this, and `/readyz` cannot — the PEP is healthy, it is the tokens that are
dead. A one-line `curl` of an `invoice:read` through the PEP is the cheapest possible canary.

#### 34. Re-seeding a used volume leaves ghost nodes in the identity tree — **known, benign**

Each seed run mints new tokens, so new block ids. The identity tree is derived from the
append-only audit chain, so a task's old generations stay in it: after three seed runs
`agt-depth-0` appears as **three nodes**, each with its own block id and its own last outcome.

Benign, and already survivable because item 5 made it so — the console keys nodes on something
unique per node and no longer throws `d3.stratify: ambiguous` and blanks the canvas. The tree is
*cluttered*, not wrong: every node it draws really did make those calls.

Two things follow, both in `docs/RUNNING.md`:

- For a pristine tree, start from a clean volume (`make demo-down` then remove the
  `demo-secrets` volume) rather than re-seeding in place.
- Revocation targets a **block id**, so it applies to one generation. Revoking a stale
  generation's block appears to do nothing, and revoking the *current* root authority block
  cascades to every agent — measured, `ANCESTOR_REVOKED` for all six, which is
  `subtree`-shaped behaviour arriving through a `token`-scoped record because the block
  revoked *is* the root of every chain.

**Also confirmed while here, and worth stating because it reads like a bug and is not:** a
revocation does **not** stop applying when its `expires_at` passes. Spec 07 §8 defines that
field as *the original token's* expiry, carried so the row can be pruned once the token could
not have authorized anything anyway — it is not a lease on the revocation. Enforcement is
permanent for the life of the token, which is the only safe reading: honouring an expiry there
would silently un-revoke a token someone revoked on purpose. Recovery is re-seeding, which
mints fresh blocks that no revocation names.

---

## What the numbers looked like at T-033

The snapshot the entries above built up to, left as it was written rather than corrected in
place — the second snapshot is the section below.

| | |
|---|---|
| Tickets complete | 23 of 53 in scope (61 defined, 8 deferred) — **M4 complete**, M5 started |
| Milestones | M1, M2, M3 complete; M4 started (specs 05-09 outstanding) |
| Tests | 1552, all passing (1434 plus 103 integration, 12 e2e, plus 3 benchmarks) |
| Coverage on `agentiam-core`, `agentiam-sdk`, `agentiam-pep` | 100% of statements |
| ADRs | 29 |
| Specs written | 8 (01-05, 08, 09, 10); 06-07 outstanding |
| Design errors caught before implementation | 12 |
| Wrong diagnoses written down before being measured | 3, all in gap 13 |
| Threats found by measurement | 9 (TM-19 through TM-27) |

---

## What the numbers looked like at T-051

The second snapshot, also left as written. Current figures are in `STATUS.md` §1.

| | |
|---|---|
| Tickets complete | 42 of 52 in scope (61 defined, 9 deferred) — **M1–M10 all resolved**, M11 started (T-051 done) |
| Tests | 2197, all passing (1958 unit + 223 integration + 12 e2e + 4 benchmarks) |
| Coverage | `agentiam-core` **100%**, kept since T-005 (`STATUS.md` §1). Whole tree ~98% — `-sdk` 89%, `-pep` 95–100% by module, `-controlplane` 86% — not independently re-measured for this entry; see `STATUS.md` §1 and §3 gap 14 for the current figure and the fact that it is reported but not yet CI-gated |
| ADRs | 48 |
| Specs written | 10 — every spec `PLAN.md` names now exists |
| Real gaps found by grepping shipped code rather than by testing it | at least 3 beyond T-033's dozen: the activation gate that existed only in the template (T-030, ADR-039), T-022's `decision_span` never once called from production code in two milestones (T-049, ADR-047), and A-04/TM-19 having no test anywhere in the tree despite `threat-model.md` documenting both (T-051) |
| Dependencies rejected on measurement, not on API completeness | `pyprobables` (~900–1,200x slower than the `set` it was meant to guard, T-039, ADR-044) |
| Threats, total | 27 catalogued (TM-01 through TM-27); 9 of those (TM-19 through TM-27) came from measurement rather than the original brainstormed catalogue — unchanged since T-033, though T-051 closed the last two (TM-19, TM-20) that had no test until now |
| Red-team attacks proven at test level | 22 of `PLAN.md` §12's 33, plus TM-19/TM-20, per T-051's reduced scope (`ROADMAP.md` line 288, ADR-048) — 1 accepted risk (A-06), 1 partially mitigated (A-12, confused deputy), 20 mitigated |

---

## Appendix — closed gaps, as `STATUS.md` §3 recorded them

Gap numbers are cited throughout the specs, ADRs and commit messages, so the closed ones
are kept here with their original findings. Open gaps stay in `STATUS.md` §4.

| # | Gap | Impact if left | Where it lands |
|---|---|---|---|
| ~~24~~ | **CLOSED.** ~~`docs/benchmarks/performance.md` has no CI drift-check.~~ `generate_benchmark_results.py --check` now runs in the `evidence-pack` job — deliberately not in `quality`, whose benchmark step rewrites the JSON the check reads. The tolerance-band problem this gap describes does not arise: `--check` renders from the *committed* JSON, so it is deterministic; only re-running the benchmark is noisy, and that half is separately gated by `test_the_whole_decision`'s `p99 < 1000 µs` assertion. `make benchmarks` / `.\make.ps1 benchmarks` added too. Original finding:  Unlike `chaos-results.md` (wired into `make chaos` and a nightly CI job), `generate_benchmark_results.py` is called by no CI job, `Makefile` target, or `make.ps1` target — confirmed by grepping all three, zero hits. Found because it was stale at HEAD (T-055, ADR-055) and resynced there; the staleness itself is fixed, the missing gate is not | The file can silently drift from `pb2-breakdown.json`/`nfr2-load.json` again with nothing to catch it, the same way it already had | T-053 owns the fix. A byte-exact `--check` in CI is not simply copy-pasteable from `generate_chaos_results.py`'s pattern: PB-2's raw timings vary run to run by design (measured: policy step median 143.1 µs vs 135.3 µs, same machine, a few seconds apart), so a naive check would fail on ordinary noise rather than a real regression — needs a tolerance band or a structural check, a design decision this ticket deliberately left to T-053 rather than deciding unilaterally |
| ~~27~~ | **CLOSED.** ~~`reap()` is never scheduled to run anywhere in the deployed system.~~ The control plane's lifespan now sweeps every `AGENTIAM_CONTROLPLANE_REAPER_INTERVAL_S` seconds (default 15, spec 04 §4.6's TTL/4). It lives there rather than in the PEP because the ledger is the control plane's and a PEP that died cannot reap its own lease. Original finding:  Spec 04's own pseudocode prescribes `REAP() # background, every TTL/4`; grepping every non-test call site found none — not in `scripts/pep_service.py`, `scripts/serve_pep.py`, either compose file, or `deploy/k3s/`. T-013 built and proved the function correct, and CH-3/CH-4's chaos tests exercise it by injecting an advanced clock and calling it directly (`tests/chaos/pepstack.py` says so in its own docstring), which is why the suite's prose reports "REAP reclaims it" as a running fact rather than a capability nobody schedules. Found while writing T-056 Part 4's rollback documentation, whose own safety argument depended on this being true, and confirmed against a real Postgres before writing it down (a manually-run `reap()` did reclaim a real stranded lease; a scheduled caller is what is missing) | Gap 21's own "impact if left" text overstates recovery: it is accurate only inside the chaos harness's manually-advanced clock. In the deployed system, a lease stranded by a crash, a `SIGKILL`, or gap 21's own scenario stays `leased` and unavailable — not lost, since `committed` is unaffected, but not reclaimed either — until *something* calls `reap()` | Needs a scheduled caller at spec 04's own `TTL/4` cadence — a periodic task in a service's lifespan, or a k8s `CronJob`. Deliberately not built inside T-056 Part 4: it touches the money hot path the way gap 25 does, and deserves its own ticket rather than a documentation-ticket side effect. `docs/deployment.md` §2.3 gives the manual invocation as an interim rollback step |
| ~~2~~ | **CLOSED.** ~~No Datalog→caveat parser.~~ `agentiam_core.datalog` reads a token's rendered block source back into caveats and identity facts (spec 02 §11), and `effective_authority()` folds a received token to its true bound. Three consumers were waiting on it and all three are wired: the deployed PEP now names the failing caveat on a decision record (spec 09 §4), reads each agent's real `agent_id`/`role` off its own block instead of `agt-depth-{N}`, and the console can show what a third party's token actually permits. **Enforcement never waited on this** — biscuit's authorizer evaluates the token's checks natively, which is what closed the enforcement half by a different route. TM-24's note was the design constraint, not a footnote: the recognizer trusts nothing, refuses an identity fact that appears twice (the exact signature of a value that broke out of its own string literal), and reports what it could not read rather than dropping it, because a dropped restriction *overstates* authority. Two behaviours it had to be measured against, both confirmed live: `block_source()` renders strings unescaped, and normalizes whitespace and statement order | Was: for a received token the console could not show a true effective bound | Closed — TODO items 2 and 4 |
| ~~3~~ | ~~**No LICENSE file.**~~ — **closed (T-056).** `LICENSE` added, byte-identical to apache.org's own canonical Apache-2.0 text — verified by sha256 against a fresh download rather than retyped | Weakened the §14.4 IP claim for a submission judged on IP ownership | Closed |
| ~~5~~ | ~~**Specs 05–09 unwritten**~~ — closed. `07-revocation` (the last one) written and read back before T-038 started, per the spec-first rule | Its ticket cannot start spec-first, which is the rule that caught seven design errors | Closed |
| ~~9~~ | ~~**CI ran none of the 45 integration tests.**~~ Closed while integrating M3: `make test` and the CI workflow both excluded the `integration` marker, so every ledger race test ran only by hand | Three tickets of ledger correctness — `FOR UPDATE` serialization, the 50-concurrent-acquire bound, the ADR-017 dedup race — were gated by nothing and would have rotted silently | Closed: `integration` CI job + `make test-integration` |
| ~~13~~ | ~~**`test_inv1_attenuation_never_widens` is intermittently flaky**~~ — closed. **Two** unrelated flakes, and the recorded cause was wrong twice. INV-1 is *not* violated: zero violations across ~15,000 contexts in two brute-force campaigns. The real cause is `biscuit-python`'s authorizer defaulting to `max_time = 1 ms` of **wall clock** — measured at 2 of 42,014 authorize calls under suite load, both inside `verify()`. The harness read that as a denial, so the parent "denied" what the child allowed. Fault injection proved it: 10 of 10 timeouts injected on a parent check reproduce the false `child authorized what the parent did not`, 9 of them with `FlakyStrategyDefinition` on top. The second flake was `test_strategies.py::test_zero_ceilings_occur`, a shape audit sampling the nine-kind union — 3 misses in 60 campaigns | The printed counterexample was **spurious**, and INV-1 stands. But this was a *product* bug on the PEP hot path, not only a test bug: a loaded PEP would have denied legitimate requests for want of scheduling, and 1 ms is the whole of NFR-1's decision budget | Closed: ADR-021 (explicit Datalog limits, harness re-raises, INV-1 draws its scenario as one composite value), ADR-022 (`caveats_of_kind`), TM-25 |
| ~~11~~ | ~~**The PEP enforces nothing.**~~ — **closed in T-023.** `/readyz` reports `enforcing: true`, and the flag is derived from the wiring rather than declared, so an app built without a pipeline still reports `false` | Was the single most misleading state in the repo | Closed. The estimate moved four times (T-019 → T-020 → T-021 → T-023); three were guesses written before reading `decide()`'s signature, and the fourth was the decision to build T-024 first (ADR-027) |
| ~~23~~ | ~~**Spent budget never reached the ledger — the same money was spendable twice.**~~ — **closed (ADR-049).** `Pipeline.settle()` settled the reservation in memory and discarded the `CommitOutcome` its own docstring says must be enqueued as `LEDGER_COMMIT`; grepping the tree found **no production caller** of `ledger_commit` from T-014 until T-052. So `budgets.committed` never moved, and since `RELEASE` returns `granted - settled`, a PEP handed the whole grant back on shutdown. Measured in CH-10: 992 requests spent 4,960, ledger recorded `committed = 0`; one instance spending 300 of a 500 lease returned all 500 | The headline correctness claim of the whole project — *budget cannot be overspent* — was false across any PEP restart or lease top-up. **The invariant checker could not see it**: `committed == Σ settled reservations` held as 0 == 0, so the books were consistent about a number that had stopped describing reality | Closed: `agentiam_pep.settlement` + `agentiam_controlplane.db.settlement_sink`, wired in both reference assemblies. A second route — a top-up releasing a lease with settlements still queued, 6,678 of 6,992 declined — closed by `LeasePool._release`'s `before_release` hook. 7 integration tests, 1 e2e assertion, CH-10 |
| ~~20~~ | ~~**A transient audit-sink outage is treated as a poison batch.**~~ — **closed (ADR-051).** `_write_batch_locked` gave a batch `max_retries` attempts and then discarded it — a path written for one record the sink will never accept, but a stopped database is indistinguishable from that at the emitter's layer. CH-1 measured records dropped roughly every `(max_retries + 1) x flush_interval_s` through a 30 s outage; the queue never filled and back-pressure never engaged | The PEP kept authorizing requests it could no longer record — the exact outcome ADR-026 exists to prevent, produced by the mechanism meant to serve it | Closed: transient failures retry without limit and `capacity` bounds them by filling and denying; only `SinkRejectedRecord` (raised by the sink, which is the only layer that can classify) drops a batch. `max_retries` removed rather than left as dead config. CH-1 now asserts *every* record survives the outage; 2 integration tests pin the classification both ways |
| ~~15~~ | ~~**`DecisionRecord.drift_score` is never written.**~~ — **closed.** `decide()` populated `Decision.drift_score` and `pipeline._record()` never read it, so the field was `None` on every record, including on a `DRIFT_ESCALATION` denial where the score is the whole justification. The pipeline now records it, and `DecisionRecord.drift_features` carries T-033's f1/f2/f5 vector alongside | `log_only` was paying two embedding round-trips for nothing observable — spec 06 §3's stated purpose unimplemented. Also unblocked persisting the feature vector, which spec 06 §5 named as the blocker | Closed. Absent features are omitted rather than stored as null, so a deferred dataset can still tell *not measured* from *measured as zero* |
| ~~16~~ | ~~**`POST /policy/activate` gates nothing.**~~ — **closed.** It assigned `store.current_source` and returned 200 with no corpus, no auto-tests and no parse; `can_activate` was computed for the template, so the gate was UI-only and a direct POST installed unparseable Cedar. The endpoint now parses, runs the full 51-case corpus, and refuses with **409** naming the failing cases (ADR-039) | Was a direct contradiction of ADR-034, T-030's acceptance criterion and `PLAN.md` §907. The test that existed asserted the *ungated* behaviour as correct | Closed. Signature and serial gates still await real bundle signing — the console store is a stub |
| ~~17~~ | ~~**`scripts/evaluate_compiler.py` measures nothing.**~~ — **harness closed, and the result is bad.** It passed bare names (`"admin"`) where Cedar needs entity uids (`User::"admin"`), so every request returned `NoDecision`: 0/30 regardless of compiler quality. Fixed and verified against a control policy (0/30 → 1/30, correct, since only case 1 concerns admins) | The harness now discriminates. **See gap 19 for what it then measured** | Closed |
| ~~18~~ | ~~**The NL compiler's model is not installed.**~~ — closed; `qwen2.5:7b-instruct-q4_0` is present and resident on GPU (5.32 GB VRAM). Meeting a real generation immediately exposed the 30 s timeout as below the *warm median* — ADR-038 | Every first call and most warm calls timed out, reported indistinguishably from Ollama being down | Closed: 300 s timeout, `keep_alive`, `warm()` |
| ~~19a~~ | **CLOSED — 27/30 (90%) on a clean run, all 30 attempted, zero throttling.** `passed 27 · wrong 1 · unparseable 2 · asked for clarification 0 · errors 0`; latency median 2.6 s. Backend `gemini-flash-lite-latest`, which resolved to **`gemini-3.5-flash-lite`** — logged per call, because the alias is otherwise unattributable. Journey: **0% → 43% (local, inflated) → 90%**, from rebuilding the dataset, removing three leaked cases from the prompt, and teaching the three Cedar mistakes that were actually measured | Beat 5 is demonstrable. The compiler is now measurable *and* good, and `--validate` keeps dataset iteration free of model quota | Remaining: 1 wrong policy and 2 unparseable, out of 30. Worth one more prompt pass, but no longer blocking |
| ~~19a-partial~~ | ~~**Measured on Gemini 2.5 Flash, leak-free prompt: 17/30 (57%) — or 17 of the 19 that got an answer (89%).**~~ — superseded by the clean run above; that one was throttled at 11 of 30. Both numbers are stated because either alone misleads. Full breakdown: **passed 17 · wrong decision 2 · unparseable Cedar 0 · asked for clarification 0 · errors/timeouts 11**; latency median 7.6 s. The 11 errors are rate-limit exhaustion, not compiler failures — Gemini's free tier throttled harder than the 15 s pacing assumed | **The two failure classes that dominated are gone.** Unparseable Cedar 8 → 0 (teaching `&&`/`||` and that only `context.amount` is a decimal); clarification refusals 23 → 0. What remains is 2 genuinely wrong policies out of 19 answered. Beat 5 is demonstrable | Lower `PACING_S` for Gemini, or use a paid tier, then re-measure for a clean 30/30-attempted number. Prompt work should target the 2 wrong cases, not the throttle |
| ~~19~~ | **Rebuilt, and the number moved 0% → 43% on the same local model.** Dataset v2 evaluates against AgentIAM's own entity model through the shared `evaluate_case`, so a case is won by generalising (`principal.role == "senior"`) rather than by guessing an id, and it is **self-validating**: `evaluate_compiler.py --validate` scores the reference policies with no model at all and must hit 30/30 before any run is worth starting. The prompt now teaches the fixed schema and the exact Cedar mistakes that were measured. Result on `qwen2.5:7b-instruct-q4_0`: **passed 13 (43%) · wrong 7 · unparseable 8 · asked for clarification 0 · errors 2**; latency median 4.4 s. **Refusals went 23 → 0.** This is the documented *local* baseline for the ADR-040 migration | Beat 5 is demonstrable on the local model for the first time, though 43% is not good enough to put in front of a judge unrehearsed. Remaining failures are Cedar-syntax slips, several of them a missing trailing `;` on an otherwise-correct policy | Hosted inference is now the prototype default (ADR-040). Re-measure on Groq; the gap between the two numbers is what the migration back to local will have to close |
| ~~19-original~~ | ~~**The NL→Cedar compiler scores 0/30 — but the dataset cannot be passed.**~~ — kept for the record, because the diagnosis is the useful part. First real measurement, now the harness works: **passed 0 · wrong decision 4 · unparseable Cedar 2 · asked for clarification 23 · timeout 1**; latency median 25.9 s, max 300.6 s. Three independent defects, established by probe: **(a)** the ambiguity instruction over-fires, refusing 77% of the corpus on prompts as plain as *"Managers can approve expenses"*; **(b)** the model's Cedar is wrong in a small fixed way — `Resource::*` for an unconstrained resource, `resource in Expense` where Cedar wants `resource is Expense`, conditions in the scope instead of a `when` clause (a narrowed prompt turned all 6 sampled refusals into compile attempts, but only 1 of 6 parsed); **(c)** — the root cause — **the dataset is not a valid instrument** | **Fixing the compiler cannot fix this.** Of 30 cases the positive principal id appears verbatim in the English in only **10**; **13** need an arbitrary suffix guessed (*"Admins"*→`admin`, *"HR"*→`hr_rep`) and **7** name an id the prompt never mentions (`alice`, `manager1`, `guest`). So ≤10/30 is the ceiling for *any* compiler — and reaching it requires emitting `principal == User::"manager1"`, a policy about one named person, which is the wrong generalisation. The right role-based policy cannot work either: the harness passes `entities=[]`, so `principal.role` and ownership do not exist. The 25.9 s median is also flattered by the refusals, since a refusal is a short generation — real compilations took 53–242 s against beat 5's 90 s budget | **Rebuild the instrument before touching the prompt.** Ids derivable from the prompt, entities supplied so role-based policies are expressible, and expectations that reward generalisation rather than id-guessing. Then re-measure, then decide on T-031. Prompt work against the current dataset optimises toward a target that is both unreachable and wrong |

### Correction to the T-019 record

Gap 13's original entry, `JOURNAL.md`'s T-019 section and the T-019 commit message all state
that the flake came from `attenuate()` drawing fresh entropy per call, breaking hypothesis
replay. **That is false.** The ephemeral-key fact is true (spec 01 §4), but the inference was
not: 200 re-mints of identical inputs produced identical token sizes and identical authorization
results, so the mint is deterministic in every way the test observes.

A second attribution — that the 1 ms timeout was firing constantly — was also wrong in degree,
and was caught the same way. The first instrumented campaign logged 5,618 swallowed exceptions
with **zero** limit errors; only the larger campaign (42,014) found the two that matter. A rate
of 0.005% is exactly the sort of thing a small sample says is absent.

The lesson is not that the guesses were bad. It is that each was written down before it was
measured, and two of the three documents carrying the first claim were user-facing. Measuring
first would have cost one afternoon and saved the correction.
