# Architecture Decision Log

Append-only. Newest entries at the bottom. One entry per non-obvious choice.

Format:

```
## ADR-NNN — <title>
**Date:** YYYY-MM-DD
**Status:** accepted | superseded by ADR-NNN | reversed
**Context:** what forced the decision
**Decision:** what was chosen
**Consequences:** what this costs, and what it rules out
```

---

## ADR-001 — Infrastructure is introduced per-ticket, not front-loaded in M1

**Date:** 2026-08-14
**Status:** accepted

**Context:** The original milestone plan placed the full service stack — Postgres, Redis,
Keycloak, Ollama with a ~5 GB model, OTEL Collector, Prometheus, Tempo, Loki, Grafana — in
Milestone 1, so that an infrastructure owner could work in parallel with protocol development.
This project has a single developer, so there is no parallel track to fill. Front-loading eight
services means one to two weeks of environment debugging before any protocol code exists, and
it de-risks nothing: the real risk in this project lives in the attenuation algebra (§6.3) and
the lease protocol (§6.4), neither of which needs more than a database.

**Decision:** `docker-compose.yml` grows one service at a time, each added by the first ticket
that requires it.

| Milestone | Services present |
|---|---|
| M1 | Postgres, Redis |
| M3 | + Alembic against real Postgres; testcontainers in the test suite |
| M4 | unchanged — the end-to-end slice needs neither Keycloak nor Grafana |
| M5 | + Ollama/Qwen2.5-7B (T-028/T-029), Keycloak (T-043) |
| M6 | + OTEL Collector, Prometheus, Tempo, Loki, Grafana (T-049) |

NFR-8 (cold start of the full stack under 90 s) is measured in M6/M7 once the stack is
complete, rather than chased against a partial stack in M1.

**Consequences:** The correctness core (M1–M4) is reachable in days rather than weeks, and the
first demoable state (T-023) arrives without any dependency on OIDC or observability wiring.
The cost is that infrastructure risk is discovered later — Keycloak realm configuration and the
Grafana datasource wiring are both known multi-hour tasks, and they now land in M5/M6 rather
than M1. This is accepted: those tasks are tedious but low-risk, whereas a lease-protocol
correctness bug found late is project-ending (risk R-3).

---

## ADR-002 — Single-developer execution; role-partitioned planning removed

**Date:** 2026-08-14
**Status:** accepted

**Context:** `docs/ROADMAP.md` was originally written for a three-person team, partitioning
work into Infrastructure, Core Logic, and Demo/Console tracks with a parallelism map across
milestones. This project is executed by one developer.

**Decision:** Scope is unchanged — all seven milestones and all eight demo beats remain in
scope, including the NL→Cedar compiler and drift detection. Only sequencing changes: the three
parallel tracks collapse into one dependency-ordered track, per-ticket ownership columns are
removed, and the "Build / Verify" split is retained because the distinction between writing
code and proving it against real infrastructure remains real for one person.

**Consequences:** Calendar time is longer than the three-person estimate; there is no fixed
submission deadline, so this is acceptable. Risk R-8 in `PLAN.md` §17 ("team member leaves") is
restated as a single-point-of-failure risk. The mitigation is unchanged and already in place:
every contract is specified in writing before implementation, and the property tests on
attenuation plus the invariant checker serve as the standing second pair of eyes on the two
components where a silent bug would be fatal.

---

## ADR-003 — Windows development host; `make.ps1` mirrors the Makefile

**Date:** 2026-08-14
**Status:** accepted

**Context:** Development happens on Windows 11. `make` is not available there, but the Makefile
is a specified deliverable of T-001 and CI runs on `ubuntu-latest`, where it works natively.
Three options were considered: install a Windows `make` port (adds a machine-specific setup
step and a second `make` dialect to reason about); drop the Makefile and drive everything
through `uv run` directly (loses the single documented entry point, and CI diverges from local);
or keep the Makefile authoritative and add a thin Windows shim.

**Decision:** The Makefile stays authoritative and is what CI invokes. `make.ps1` mirrors its
targets one-for-one for local use on Windows, so `.\make.ps1 check` and `make check` do the same
thing.

**Consequences:** The two files must be kept in step by hand — a target added to one and not the
other is a real, if small, drift risk. Accepted because the shim is short and target changes are
rare. The shim is a convenience wrapper only: it must never contain logic that the Makefile
lacks, or CI stops being the source of truth about whether the project builds.

---

## ADR-004 — Ruff does not format Markdown

**Date:** 2026-08-14
**Status:** accepted

**Context:** Ruff 0.16 formats Python code blocks embedded in Markdown files. Applied to
`docs/`, it rewrites the illustrative Python in `PLAN.md` §6 and §7 — collapsing the aligned
trailing comments on the `Budget` and `DecisionRecord` models, among others.

**Decision:** `extend-exclude = ["*.md"]` in the Ruff configuration.

**Consequences:** Code blocks in documentation are not format-checked, so they can drift from
the style of the real code. That is the right trade: the blocks in `PLAN.md` are specification
prose, chosen for readability, and they are never compiled. A formatter rewriting a
specification to satisfy a line-length rule is the tool overruling the spec. The
milestone spec-drift check (`ENGINEERING-RULES.md` §5) is what keeps documented code honest,
not the formatter.

---

## ADR-005 — Token checks constrain verifier-supplied request facts

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `docs/specs/01-token-format.md`, T-005, T-007, T-009

**Context:** `PLAN.md` §6.1 sketches the token format: attenuation blocks carry `depth(n)`
facts and checks described as "scope subset, budget ceilings, time window". Implementing that
sketch literally produces a token that appears to enforce restrictions and does not. Three
problems, all confirmed by measurement against `biscuit-python` before any code was written:

1. **Biscuit checks are existential.** `check if scope($s), ["invoice:read"].contains($s)` asks
   whether *some* granted scope is in the list, not whether the scope *being requested* is. A
   token granting `invoice:read` and `vendor:read` authorizes `vendor:read` under a caveat
   naming only `invoice:read`. Measured: the narrowing had no effect.
2. **A `depth` fact in a block is attacker-controlled.** Combined with existential semantics,
   `check if depth($d), $d <= 8` is satisfied by the authority block's own `depth(0)` no matter
   how deep the chain runs. Measured: a depth-9 chain authorized successfully.
3. **A budget ceiling cannot be a string.** §6.1 shows `budget("spend_bdt", "500000")`. Datalog
   cannot compare strings numerically, so the token could not enforce its own budget caveat
   offline — which is the reason for carrying it at all.

**Decision:**

1. Token blocks carry only the **grant**. The verifier supplies the **request context** —
   `operation`, `requested(dimension, value)` for every dimension, `current_depth`,
   `request_intent`, `time` — and every check is written against those facts.
2. Authorization depth is `block_count - 1`, computed by the verifier. A `declared_depth` fact
   may appear in a block for audit and console rendering, and MUST NOT be used for
   authorization.
3. Budget values are integers scaled by 10⁴ (`BUDGET_SCALE`), matching `NUMERIC(20,4)`. Every
   dimension, money or count, uses the same scale so one comparison rule covers all of them.

A corollary that is easy to miss: a check whose fact is absent **fails**. So the verifier must
supply a `requested` fact for every dimension on every call, defaulting to zero. Omitting a
dimension denies the request rather than leaving it unconstrained.

**Consequences:** The verifier becomes responsible for assembling a complete, trustworthy
request context on every call — a real obligation, and the natural place for a bug. T-019 must
test that each context fact is populated, and T-051 should include a red-team case for a
partially-populated context. In exchange, INV-1, INV-2, INV-6, and INV-7 hold against a direct
fact-injection attack, verified: a block appending `operation(...)`, `scope(...)`,
`requested(...)`, and `current_depth(0)` could not re-grant anything it had been narrowed out
of.

This changes the wire format only, not the caveat language, the attenuation semantics, or the
lease protocol. Those remain as specified in `PLAN.md` §6.2–§6.4.

---

## ADR-006 — Token reference mode stays deferred, now with a measured reason

**Date:** 2026-08-14
**Status:** accepted
**Affects:** T-010, `PLAN.md` §21 item 6

**Context:** T-010 (opaque token references for chains that overflow HTTP headers) was deferred
on the assumption that the demo runs at depth 3–4 and would not reach the 8 KB limit. That was
a guess. The token format spec required real byte counts, so the growth curve was measured.

**Decision:** Keep T-010 deferred. The measurement supports it more strongly than the original
reasoning did.

**Consequences:** Measured growth is ~410 base64 bytes per attenuation block. At `max_depth = 8`
— the *maximum permitted chain*, not the demo's depth — a token is 4,940 base64 characters:
over the 4 KB warning threshold, but 60% of the 8 KB hard limit. Reference mode is therefore
unreachable within the permitted depth range, and T-010 is dead code until `max_depth` rises
above roughly 16.

Two live consequences remain. The 4 KB warning fires at depth 6, so that path is reachable and
EC-T11 must test it. And the full `Authorization` header at depth 5 approaches nginx's 4 KB
default `large_client_header_buffers` line size — T-018 must raise it deliberately rather than
discovering it at depth 6.

---

## ADR-007 — `check if` for mandatory facts, `reject if` for optional ones

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `docs/specs/02-caveat-language.md`, T-008, T-019

**Context:** Biscuit offers two clause forms, and they behave oppositely when the fact they
constrain is absent. `check if BODY` passes only if BODY has a solution, so it **denies** when
the fact is missing. `reject if BODY` fails only if BODY has a solution, so it **passes** when
the fact is missing. Both are supported by `biscuit-python`; this was verified before choosing.

Picking one form for all caveats breaks half of them in opposite directions:

- As `check if`, an `ArgPredicate` on `payment.amount` denies an `invoice:read` call, which
  carries no such argument. Measured: exactly that happened.
- As `reject if`, a `TimeWindow` becomes vacuous whenever the verifier omits `time()` — the
  token would simply never expire. This is the worse failure, because it fails *open*.

**Decision:** The form follows the fact, not the caveat.

- Caveats constraining facts the verifier supplies on **every** request — `operation`,
  `requested(dimension, …)` for every dimension, `current_depth`, `request_intent`, `time` —
  compile to `check if`.
- Caveats constraining facts that may legitimately be **absent** — `tool`, `arg` — compile to
  `reject if`.

`ToolAllow` is the deliberate exception: it constrains the optional `tool` fact but uses
`check if`, because a call with no tool identity must not satisfy an allow-list. Fail closed.

**Consequences:** The two forms must not be mixed up per caveat type, so T-008's table-driven
tests must cover the absent-fact case for every type — not just the present-and-passing and
present-and-failing cases, which is the natural thing to write and would miss the entire class
of bug. Verified as sound: `reject if` is vacuous when its fact is absent, binding when present,
and a later block adding a benign `tool` fact cannot escape an earlier block's `reject`.

---

## ADR-008 — `RequiresApproval` is a block fact evaluated in Python, not a Datalog clause

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `docs/specs/02-caveat-language.md` §4.9, T-008, T-019, T-037

**Context:** `RequiresApproval` must produce the outcome `escalate` — raise the decision to a
human rather than allow or deny it. A biscuit authorizer has no third answer; it returns
authorized or not. Encoding `RequiresApproval` as a check would turn every escalation into a
silent denial, which contradicts both `PLAN.md` §6.6 ("escalation raised to a human, not a
silent block") and demo Beat 6.

**Decision:** `RequiresApproval` compiles to a **fact** (`requires_approval("scope")`) rather
than a clause. The PEP reads block sources via `Biscuit.block_source(i)`, reconstructs the
caveat set in `agentiam-core`, and evaluates it there. Verified: block sources are readable for
every block in a chain.

**Consequences:** This is not a weaker guarantee, and the reason is worth being precise about.
The fact cannot be removed, because blocks are append-only and signed. It also cannot influence
a Datalog decision in either direction: `authorizer.query` returns authority-block facts but
**nothing** for block facts — verified — so block facts are inert to the Datalog engine. That
same scoping is what makes fact injection unable to widen authority (ADR-005), and here it is
what forces evaluation into Python.

The cost is that `agentiam-core` now needs a caveat evaluator that agrees with the Datalog
compilation on allow/deny for every input. That agreement is a **security property**, not a
nicety: the PEP trusts Datalog for the decision and the evaluator for the explanation, so a
divergence means a decision record naming a caveat that did not actually fire. T-008 must
enforce it with a conformance test over a generated corpus.

A second, welcome consequence: the same evaluator is what lets a decision record name the exact
failing caveat, since biscuit reports only *that* authorization failed, not which clause caused
it.

---

## ADR-009 — Commits against a non-active lease are rejected and flagged, not applied

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `docs/specs/04-lease-protocol.md` §11, T-013, T-014, T-047

**Context:** `RELEASE`, `REAP`, and `REVOKE` each return a lease's full `outstanding` amount to
the pool. The pseudocode in `PLAN.md` §6.4 does not say what happens when a `LEDGER_COMMIT` for
that lease arrives *afterwards* — a buffered batch from a crashed PEP, or a partitioned PEP
reconnecting. Applied normally, it decrements `leased` a second time for budget that was already
returned.

This was found by model-checking the protocol before writing the spec, not by reasoning about
it. Random interleavings drove `leased` negative in 55 of 400 runs. It is not a corner case: any
`REAP` racing an in-flight commit reaches it, which is precisely the crash scenario the protocol
exists to survive.

**Decision:** `LEDGER_COMMIT` against a lease whose state is not `active` MUST be rejected. It
MUST NOT modify `committed` or `leased`. It MUST be recorded as a reconciliation anomaly with
the lease id, amount, and terminal state. The anomaly count appears on the budget dashboard
(T-047) and must be zero in a clean chaos run.

**Consequences:** A spend that really happened is not recorded against the budget. That is the
cost, and it is the right one: the pool invariant is preserved by construction and the
divergence is surfaced loudly rather than silently corrupting the ledger. AgentIAM emits
settlement instructions rather than moving money (`PLAN.md` §1.4), so an anomaly is a
reconciliation item, not a lost payment.

The stranded-lease window is what makes this reachable at all, so the two limitations are
linked: shortening `ttl` reduces stranded budget but increases the rate of late commits. Both
are stated in spec 04 §14 with their bounds.

---

## ADR-010 — Idempotency protects the books, not the pool

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `docs/specs/04-lease-protocol.md` §5.1, T-014 (P-12)

**Context:** `PLAN.md` §6.4 lists idempotency-by-`reservation_id` among the correctness
properties of the lease protocol, alongside the safety argument for `Σ spend ≤ total`. The
model check contradicted the implied grouping: with replay enabled and idempotency disabled,
400 interleavings produced **zero** violations of `committed + leased ≤ total`.

The reason is that a replayed commit does `committed += a; leased -= a`, which conserves
`committed + leased` exactly. The pool cannot notice.

**Decision:** Treat idempotency as an **accounting** guarantee and test it as one. P-12 asserts
that `committed`, `lease.settled`, and `outstanding` equal their single-delivery values after
duplicate delivery — not that the safety invariant survives.

**Consequences:** A P-12 written against the safety invariant, which is the natural thing to
write given how §6.4 groups these properties, would pass while the books were threefold wrong.
Measured on one real spend of 30 delivered three times: `committed` reaches 90, `outstanding`
falls to 10. The mandate looks exhausted early, the PEP loses budget it never used, and the
audit ledger records spend that did not happen. For a system whose pitch is chain of custody
that is not a lesser failure than overspend, only a different one.

Related: the clamp to `lease.outstanding` (spec 04 G2) is what prevents a *missing* idempotency
guard from becoming a safety bug — once `outstanding` reaches zero, an unclamped replay drives
`leased` negative on the next delivery. Measured. The two guards are not redundant; they cover
adjacent failures.

---

## ADR-011 — A `TimeWindow`'s two sides are separate comparability slots

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `docs/specs/03-attenuation.md` §3, T-009, T-011

**Context:** Spec 03 §3 defines `TimeWindow` narrowing as interval containment:
`[a₁,b₁] ⊑ [a₂,b₂] ⟺ a₁ ≥ a₂ ∧ b₁ ≤ b₂`. Implemented literally, with an absent bound read as
unbounded, the INV-9 property test failed on a case that is not only safe but is *the most
common thing a holder wants to do*: a child adding only `not_after` to shorten its life. With
`not_before` read as −∞, the child looks wider on the lower side and the mint check refuses it.

The reading is wrong because a `TimeWindow` compiles to one Datalog clause per bound. A child
supplying only `not_after` emits only that clause and claims nothing about the lower bound — and
the ancestor's lower-bound clause still applies, because every clause in every block must pass.

The obvious repair, treating an absent bound as "inherited" inside `narrows()`, breaks the
algebra: an upper-only and a lower-only window would then be mutually narrowing without being
equal, so `narrows()` would no longer be antisymmetric and T-009 explicitly requires that it is.

**Decision:** Introduce `atoms()`. A two-sided `TimeWindow` decomposes into a lower-bound atom
and an upper-bound atom, and `comparability_slot()` distinguishes `lower`, `upper`, and `both`.
`narrows()` keeps strict set containment within a slot and stays a partial order;
`check_narrowing()` compares atom by atom, so a one-sided narrowing is compared only against
ancestors constraining that same side.

**Consequences:** `TimeWindow` is the only kind that decomposes, which is a wrinkle worth
remembering when a tenth caveat kind is added — any caveat compiling to more than one clause
needs the same treatment, or it will produce the same false refusal. The property-test
generators had to change too: a same-slot group must fix the *side* as well as the kind, or
transitivity draws are mostly incomparable and pass vacuously.

Found by a property test rather than by review, which is the argument for writing them against
real tokens: nothing in the spec text looked wrong.

---

## ADR-012 — Thread propagation carries the identity, not a copied `Context`

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `agentiam_sdk.context`, T-011

**Context:** `contextvars` do not cross the thread-pool boundary, and T-011's acceptance
criteria require that boundary be handled explicitly rather than discovered. The idiomatic fix
is `contextvars.copy_context()`: snapshot the caller's context, bind it to the callable, and run
the callable with `Context.run` inside the worker.

Measured before adopting it: **entering one `Context` object from two threads at once raises
`RuntimeError: cannot enter context ... is already entered`.** Sequential reuse is fine;
concurrent reuse is not. A bound callable submitted to a pool twice — which is the ordinary way
to use a pool — therefore crashes under exactly the concurrency the helper exists to serve. The
failure is timing-dependent, so it would have passed a casual test and failed under load.

**Decision:** `bind_identity()` captures the `AgentIdentity` value at bind time and re-installs
it inside a fresh scope on each invocation. No `Context` object is shared, so there is nothing to
enter twice. `run_in_executor()` is a thin wrapper over it.

**Consequences:** Only the AgentIAM identity propagates, not arbitrary context variables. That is
the correct scope for this package — an SDK that silently transplanted a caller's whole context
into worker threads would be doing something surprising — but it is a real difference from
`asyncio.to_thread`, which does copy everything and needs nothing from this module.

Three interpreter behaviours are now pinned by tests rather than trusted: that
`loop.run_in_executor` does not propagate, that `asyncio.to_thread` does, and that the rejected
`copy_context()` design really does raise. The last one exists so the guard is demonstrated to be
load-bearing; a guard never seen to fire is not a guard.

---

## ADR-013 — `role` stays free text, with a character class rather than an enum

**Date:** 2026-08-14
**Status:** accepted
**Affects:** `agentiam_core.models`, `agentiam_core.attenuation`, `docs/specs/01-token-format.md`
§10 Q4, `docs/specs/02-caveat-language.md` §10 Q3, `docs/threat-model.md` TM-24, T-011

**Context:** Both specs left the same question open for T-011: should `role` be a closed enum so
the console can render it safely? The motivation was rendering, and probing the rendering path
turned up the actual hazard, which an enum would only have hidden.

`quote_string()` escapes correctly — a crafted `role` cannot forge a fact inside a signed token,
and biscuit's block scoping (assumption A1) means an injected block fact could not widen
authority even if one existed. But `block_source()` renders the string back **unescaped**.
Measured: a role of `x"); admin(true); //` renders as block text that re-parses into a genuine
second `admin(true)` fact. The same path exists in the authority block via `principal_id`, which
the issuance service will populate from Keycloak claims.

Every planned consumer of block source is a display or re-parsing path: the console's caveat
chain (T-045), the audit explorer (T-048), and the Datalog-to-caveat parser both need
(`STATUS.md` §3, gap 2). Escaping correctly in each of them is three chances to get it wrong.

**Decision:** `role` is not an enum. A closed set would make adding a role a protocol change,
and roles are domain vocabulary rather than protocol. Instead `models.validate_label` bans, in
the three fields that become Datalog string facts, the characters that actually cause harm:
quote and backslash (they break the round trip), C0/C1 controls (they break the line structure),
and bidi marks, embeddings, overrides and isolates (they reorder rendered text without changing
a byte, which spoofs a role name in the console). Length is capped at 128. Everything else is
permitted.

**Consequences:** A Bengali role name renders as itself, which an ASCII allow-list would have
prevented — worth stating for a Bangladesh submission. The console still must escape for HTML;
this closes the Datalog layer, not the browser's.

The check lives in `agentiam-core` rather than the SDK because core is what mints. The PEP and
the issuance service inherit it without knowing it exists, and `tests/security/` gains its first
occupant — the directory T-051 will grow into.

**Cost:** two extra validations on the mint path, both regex searches over strings under 128
characters. Immaterial against NFR-1.

---

## ADR-014 — `budgets` ships alone; `mandate_id` carries no foreign key yet

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `docs/PLAN.md` §7, `docs/specs/04-lease-protocol.md` §2.1, T-012, T-013, T-014

**Context:** T-012's ticket text is "Budget schema + migrations," but `PLAN.md` §7 lists
`budgets`, `leases`, and `reservations` together in one data-model block, and the acceptance
criteria for T-012 name only the `budgets` invariant `CHECK`. Two questions needed a decision
before writing the migration, neither answerable from the schema block alone.

**Decision 1 — scope of this migration.** Only `budgets` lands in T-012. `docs/ROADMAP.md`'s
Milestone 3 table describes T-012's Build column as "Budget schema — SQLAlchemy models +
Alembic migration" (singular), and spec 04 §16's test-mapping table assigns every `leases`- and
`reservations`-shaped test (P-10, P-11, P-12, P-20, the concurrent-acquire test, the
late-commit-rejection test) to T-013 and T-014 — the tickets that implement the operations that
give those tables meaning. Shipping empty tables now would mean guessing at columns a later
ticket might need to change, for no test this ticket can write.

**Decision 2 — `mandate_id` has no `FOREIGN KEY`.** T-005 built `Mandate` as a pure Pydantic
model (`agentiam-core` has zero I/O, `ENGINEERING-RULES.md` rule 3); no `mandates`,
`tasks`, or `principals` SQL table exists anywhere in the repository, and no ticket in `PLAN.md`
§9 yet owns creating them — mandate persistence is narrated in `PLAN.md` line 284 ("the
issuance service ... creates the mandate in the ledger") but is not itself a numbered ticket.
Building a minimal `mandates` table to satisfy the FK would mean inventing a schema for a table
this ticket does not otherwise need, ahead of the ticket that actually owns it, and risking a
second migration to reshape it later. `budgets.mandate_id` is `UUID NOT NULL`, indexed for
lookup by the `UNIQUE(mandate_id, dimension)` constraint (Postgres can use a composite index's
leading column), but unconstrained by a `REFERENCES` clause.

**Consequences:** A budget row can be created against a `mandate_id` that was never issued;
nothing in the schema catches it (`docs/STATUS.md` §3, gap 7). This is a real gap, not a
theoretical one, and it is closed by whichever ticket first persists mandates — at that point
the FK should be added in a follow-up migration, not deferred again. Until then, the pool
invariant itself (`committed + leased <= total`, this ticket's actual acceptance bar) does not
depend on the FK existing: it is a property of one row, not a join.

`leases` and `reservations` will each need their own `CHECK` constraints when T-013 and T-014
add them (spec 04 §5, guards G2/G3), and by the same reasoning, each should be added by the
ticket that implements the operations that would otherwise leave the guard untested.

---

## ADR-015 — `ACQUIRE` does not apply the `max_fraction` clamp

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `docs/specs/04-lease-protocol.md` §4.1, §12, T-013, T-015

**Context:** Spec 04 §4.1's pseudocode computes `grant := min(requested, available,
max_fraction * available)`. `PLAN.md` §6.4's version of `ACQUIRE` has no such term:
`grant = min(requested, available)`. Implementing the clamp literally was tried first, since
spec 04 supersedes the plan on protocol detail (`ENGINEERING-RULES.md` rule 2) — but it does
not survive contact with this ticket's own acceptance test.

**Measured.** With `max_fraction = 0.25` (spec 04 §12's default) and 50 concurrent callers each
requesting `1` against a `total` of `10`: the clamp only binds once `available < 4`, and past
that point each grant is `0.25 * available` — so `available` shrinks by a factor of `0.75` per
acquire and *never reaches zero* in any bounded number of calls; it only hits exactly `0.0000`
once `NUMERIC(20,4)` rounding forces it there, around the 40th call in this scenario. Every one
of the 50 callers gets a nonzero grant. `PLAN.md` §9's stated acceptance bar for T-013 —
"exactly 10 succeed, 40 get `Insufficient`" — is unreachable with the clamp applied to an
explicit caller-supplied `requested` amount, for any `total`/`requested` large enough to make
the concurrency test meaningful. This is not a rounding-corner case; it is the clamp's ordinary
behavior on the exact scenario the ticket specifies.

**Decision:** `ACQUIRE` computes `grant = min(requested, available)` — `PLAN.md` §6.4's
form, not spec 04 §4.1's. The `max_fraction` term belongs to **adaptive lease sizing** (spec 04
§12, "Lease sizing policy"): `lease = clamp(EWMA(spend_rate) × target_horizon, min_lease,
max_fraction × available)` is the formula that decides how large a lease *the ledger itself
should compute and request*, not a second cap layered on top of whatever amount a caller
explicitly asked for. Spec 04 §12 itself states "Current: fixed size. Adaptive sizing is
deferred (T-015...)" — so no caller in the system today derives its `requested` value from that
formula, and `max_fraction` has nothing to apply to yet. Spec 04 §4.1 folding the clamp directly
into `ACQUIRE`'s pseudocode conflates the two concerns; §12's own text is the correction.

**Consequences:** `max_fraction`'s actual job — bounding how much a single PEP can strand on
crash — is unenforced until T-015 computes lease sizes adaptively and applies the clamp there,
where it behaves as intended (bounding a *computed* size, not repeatedly shrinking a *fixed*
one). This is consistent with T-015 already being deferred (ROADMAP.md, `PLAN.md` §21) and
stated as a known gap rather than silently absent: the stranded-budget limitation in spec 04 §14
is currently bounded only by `ttl`, not additionally by `max_fraction`, until T-015 lands. Per
rule 9 ("never weaken a test to make it pass — fix the code or fix the spec, and if the spec,
write the ADR"): the code was fixed to match `PLAN.md`'s simpler formula rather than the test
being loosened, because the simpler formula is what T-013's acceptance criteria were written
against and the math above shows the two are incompatible as literally specified.

---

## ADR-016 — `RESERVE`/`COMMIT` live in `agentiam-pep`, not `agentiam-controlplane`

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `docs/specs/04-lease-protocol.md` §4.2, §4.3, T-014, T-018, T-021

**Context:** T-013's `db/ledger.py` holds `ACQUIRE`/`RELEASE`/`REAP` — all three are ledger-side,
take an `AsyncSession`, and run inside `SELECT ... FOR UPDATE`. Spec 04 §4.2/§4.3 describe
`RESERVE` and `COMMIT` as PEP-side: "no network, no ledger mutation, no lock," touching only a
`LocalLease.remaining_local` the PEP holds in memory (spec 04 §2.3). `CONTEXT.md`'s hand-off from
T-013 left where they belong as this ticket's call.

**Decision:** They land in a new `agentiam_pep.lease` module — `LocalLease`, `Reservation`,
`CommitOutcome` dataclasses plus pure `reserve()`/`commit()` functions, with `agentiam_pep.errors`
mirroring the `ReservationInsufficientError` pattern already used by `LeaseUnavailableError` and
`LeaseNotActiveError`. Not `agentiam_controlplane.db.ledger` alongside the three DB operations
(wrong: they touch no database, and putting them in `db/` beside `AsyncSession`-taking functions
invites a future edit to reach for a session that shouldn't exist), and not `agentiam_core` either
(that package is the *correctness core* — token format, caveats, attenuation — not runtime
protocol state; `LocalLease` is mutable, per-PEP, per-process state, which is a different kind of
thing than the immutable value types `agentiam_core` holds). `agentiam-pep` already depends only
on `agentiam-core`, so this adds no new dependency, and it gives T-018/T-021 (which build the real
PEP gateway and wire a lease pool into it) a working starting point rather than a second
from-scratch design.

**Consequences:** `agentiam_pep` gains real source ahead of T-018, which its own module docstring
says is "Implemented from T-018" — narrowly true of the decision *pipeline*, not of every module
the package will ever hold. The `reservations` table (`PLAN.md` §7) is written only by
`LEDGER_COMMIT`, never by `RESERVE`: `reservations.id` is the PEP-generated idempotency key (spec
04 §10), but no row exists for it until the ledger actually applies a commit, so a `Reservation`
that is never committed leaves no trace in Postgres — consistent with "no ledger mutation" at
`RESERVE` time, and cheap, since an uncommitted reservation was never spend that happened.

`reconciliation_anomalies` is a new table with no entry in `PLAN.md` §7's data-model block —
spec 04 §11's late-commit rule (found by model-checking `PLAN.md` §6.4's original pseudocode, not
present in it) requires the rejection to be recorded, and nothing else in the schema does that.
It carries `lease_id`, `reservation_id`, `reported_amount`, and `lease_state` (the three fields
spec 04 §4.4's pseudocode names plus the reservation id for audit trace), with no uniqueness on
`reservation_id` — a late commit retried by a reconnecting PEP produces one anomaly row per
attempt, since spec 04 §11 states no dedup requirement for anomalies (unlike `reservations`,
where dedup is the entire point).

**Escalation, in `commit()`.** Spec 04 §4.3's pseudocode reads "`RESERVE(lease, delta) or
escalate — must be covered before committing`," which could be read as blocking the commit
outright. It does not: the tool call the reservation was covering has already executed by the
time `COMMIT` runs, so the spend is real regardless of local headroom. `commit()` always returns
a `CommitOutcome` carrying the full, unclamped `actual` for the caller to enqueue as
`LEDGER_COMMIT` — clamping to what the ledger will actually accept is `ledger_commit()`'s G2, not
this function's job (spec 04 §6: "a PEP cannot break this"). `escalated=True` on the outcome is
the signal a caller acts on; it never suppresses the enqueue.

---

## ADR-017 — `LEDGER_COMMIT` checks idempotency after locking the lease, not before

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `docs/specs/04-lease-protocol.md` §4.4, §10, T-014 (G4/P-12)

**Context:** Spec 04 §4.4's pseudocode writes `LEDGER_COMMIT` in this order: check whether
`reservation_id` is already settled, *then* `SELECT lease FOR UPDATE`. Implemented literally,
this is a TOCTOU race — the dedup check runs against an unlocked read, so two concurrent commits
carrying the same `reservation_id` (a retried batch send racing itself, the exact scenario G4
exists for) can both observe "not yet settled" before either takes the lease's row lock, and both
then proceed to apply.

**Measured.** Ten concurrent `ledger_commit()` calls, same `lease_id` and `reservation_id`, run
against the literal spec order: the race reproduced on all three runs attempted (`asyncio.gather`
over 10 tasks against a `NullPool` engine, same shape as T-013's 50-concurrent-acquire test). The
failure is not a silent double-apply — the `reservations.id` primary key catches the second
`INSERT` — but it surfaces as an **unhandled
`asyncpg.exceptions.UniqueViolationError: duplicate key value violates unique constraint
"reservations_pkey"`** propagating out of `ledger_commit()`, rather than the clean `False`
(idempotent no-op) the caller should be able to rely on. A batched-commit worker calling this in
a retry loop would crash instead of degrading gracefully.

**Decision:** `ledger_commit()` acquires `SELECT lease FOR UPDATE` first, and checks
`reservations` for the `reservation_id` second, both inside the lock's scope. The postcondition —
a duplicate `reservation_id` applies nothing and returns `False` — is unchanged; only the order of
two independent reads changed, so this is not a deviation from the protocol spec 04 describes,
only from the literal statement order of its pseudocode. Reordering closes the race because every
concurrent `LEDGER_COMMIT` against the same lease now serializes on the row lock before either one
re-reads `reservations`, so the second caller's dedup check runs *after* the first caller's
`INSERT` has committed and become visible.

**Consequences:** None to the external contract — `ledger_commit()`'s return type and raised
exception are exactly as documented regardless of this ordering. The risk this ADR guards against
is a future edit "fixing" the code to match spec 04 §4.4's literal statement order, silently
reintroducing the crash. Verified guard-proof style (`docs/JOURNAL.md`'s recurring lesson): the
literal order was restored, `test_concurrent_duplicate_ledger_commits_apply_exactly_once` was
rerun and failed with the exact error above on repeated attempts, then the lock-first order was
restored and the full `test_ledger_commit.py` suite rerun green.

---

## ADR-018 — The invariant checker asserts three invariants, and one of them is already a `CHECK`

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_controlplane.db.invariants`, `scripts/run_invariant_checker.py`, T-016,
T-047, T-052

**Context:** `PLAN.md` §9 names two invariants for T-016: `committed + leased <= total` and
`Σ committed = Σ settled reservations`. The first is already a database `CHECK` constraint
(T-012, ADR-014). Re-asserting in application code something the schema enforces is normally
waste, so the question was whether the checker is doing anything the database is not.

**Measured**, before writing it:

| Injection | Result |
|---|---|
| `UPDATE budgets SET committed = total + 1` | **Refused** — `IntegrityError` from `ck_budgets_invariant` |
| `UPDATE budgets SET committed = committed + 10` | **Accepted.** `Σ reservations` still 40, `committed` now 50 |

The `CHECK` compares three columns of one row. It cannot see a sum over `reservations` and
`leases`, so the books invariants have no schema backing at all — and the second `UPDATE` is
what a double-applied commit, a partially-rolled-back transaction, or a hand-repaired row
actually looks like. That is precisely the drift ADR-010 predicts when it says idempotency
protects the books rather than the pool: nothing was protecting the books.

**Decision:** the checker asserts **four** things, not two.

1. `committed + leased <= total` — the pool invariant. Redundant with the `CHECK` in normal
   operation, and kept anyway: a `CHECK` can be dropped by a half-applied migration, and a
   replica or a restored dump may not carry it. Cheap, and the one that matters most if it ever
   does fire.
2. `committed == Σ reservations.amount` over that budget's leases — the plan's second invariant.
3. `leased == Σ (granted - settled)` over that budget's **active** leases. Not in `PLAN.md`,
   derived from the write paths in `db/ledger.py`: `ACQUIRE` adds, `LEDGER_COMMIT` moves, and
   `_retire` returns exactly `outstanding` when a lease leaves `active`. It is the invariant a
   missed decrement in `_retire` breaks — the ADR-009 / TM-21 failure shape — and the schema
   cannot see it either.
4. `committed >= 0 AND leased >= 0`, reported separately from the pool violation. A negative
   `leased` is the specific TM-21 signature that model-checking produced in T-004, and calling
   it by its own name saves whoever reads the alert a step.

**Everything is read in one SQL statement.** Not a style preference. The quantities are compared
against each other, so they must share a snapshot; read in three statements, an `ACQUIRE`
landing between two of them reports a violation that never existed. A checker that cries wolf
gets muted, and a muted checker and a broken one fail identically. The integration suite sweeps
25 times against four concurrent workers to keep that honest.

**Consequences:** the checker never writes and never locks, so it is safe against a live ledger
— which is what T-052 runs it as, and what Beat 4 puts on screen. Measured at 3–5 ms for a
sweep over 500 budgets, against an acceptance bar of *detects within 1 s*, so the default 1 s
interval has roughly 200× headroom and could be tightened for the demo without concern.

`PLAN.md` §8's `GET /v1/budgets/{mandate_id}/invariant` is **not** built here — no FastAPI app
exists yet. It should reuse `check_invariants` when the control-plane API lands rather than
growing a second implementation of the same three sums.

**One bug found by running it, not by testing it.** The loop catches errors so a chaos run can
continue through CH-1 (Postgres down) — and caught only `SQLAlchemyError`, which is wrong: a
refused connection surfaces as a bare `ConnectionRefusedError` because the failure happens below
the dialect and SQLAlchemy never wraps it. The script crashed on the exact condition its
docstring claimed it survived. The unit suite now points it at a dead port; removing `OSError`
from the except clause fails three tests.
