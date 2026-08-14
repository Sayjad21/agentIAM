# Development Journal

What each ticket did, what turned up while doing it, and what changed as a result.

`DECISIONS.md` holds the formal ADRs. This is the narrative around them — including the
findings that did *not* need an ADR, and the mistakes worth remembering.

---

## The one habit that shaped everything

Before writing any spec, I checked its claims against a running system.

That sounds like overhead for a documentation ticket. It was the opposite. Across T-002,
T-003, T-004, T-009 and T-011 it found **seven design errors**, every one of which would
otherwise have surfaced later as a failing property test — or worse, as a *passing* one written
from the same wrong premise. It also turned up a security finding no amount of brainstorming was
going to produce (TM-24).

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

---

## What the numbers look like

| | |
|---|---|
| Tickets complete | 10 of 53 in scope (61 defined, 8 deferred) |
| Milestones | M1 complete, M2 code complete (specs 05-09 outstanding) |
| Tests | 705, all passing |
| Coverage on `agentiam-core` and `agentiam-sdk` | 100% of statements, 99% of branches |
| ADRs | 13 |
| Specs written | 4 of 9 |
| Design errors caught before implementation | 7 |
| Threats found by measurement | 6 (TM-19 through TM-24) |
