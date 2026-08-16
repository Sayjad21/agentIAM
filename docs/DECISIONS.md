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

---

## ADR-019 — Proportional split adds an `allocated` column, not a second meaning for `leased`

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_controlplane.db.models`, `db.ledger.split_budget`, `db.invariants`,
`docs/specs/04-lease-protocol.md` §13, §15, T-017, INV-5

**Context:** spec 04 §13 says a proportional split gives each child "its own budget row." Two
things had to be settled before that could be built, and neither was answerable from the spec
text.

**Measured first.** A second row for the same `(mandate_id, dimension)` is refused —
`uq_budgets_mandate_dimension`, an ordinary `UniqueViolationError`. So the feature is not
additive; it changes a constraint T-012 established.

**Decision 1 — the pool uniqueness becomes partial, not absent.** `uq_budgets_pool` is a unique
index on `(mandate_id, dimension) WHERE parent_budget_id IS NULL`. Allocation rows share their
parent's pair by design, so an unconditional constraint refuses the whole feature; dropping it
outright would also allow two pool rows for one mandate, which is the thing T-012 was protecting
against. A second constraint, `uq_budgets_allocation` on `(parent_budget_id, agent_id,
dimension)`, keeps one allocation per child — splitting twice for the same agent is a bug, not a
top-up.

`ck_budgets_split_shape` requires `parent_budget_id` and `agent_id` to be set together or not at
all. A row with a parent and no agent belongs to nobody; a row with an agent and no parent is a
pool wearing a name tag. Either would be skipped by the checker's per-kind queries rather than
reported, which is the worst outcome available.

**Decision 2 — `allocated` is its own column.** Budget promised to a child is spoken for, so it
has to enter the pool invariant:

    committed + leased + allocated <= total

The tempting alternative — increment `leased` when allocating, since the money is equally
unavailable either way — is wrong, and specifically wrong in a way that only shows up one ticket
later. T-016's checker asserts `leased == Σ outstanding of active leases`. Overloading `leased`
with allocations breaks that check on the first split, and the natural "fix" would be to weaken
the check, which is rule 9 in reverse. Two meanings, two columns.

**Consequences.** The checker gains a fourth invariant, `allocated == Σ child totals`, with the
same shape as the other two books invariants: no `CHECK` can express a sum over other rows, so
only the checker can see it drift.

`acquire()` gains `agent_id: str | None = None`. `None` means the pool row, which is spec 04
§13's default and keeps every T-013 and T-014 call site working unchanged. It is not optional
politeness: before T-017 the lookup was `scalar_one()` on `(mandate_id, dimension)`, and once a
split exists that raises `MultipleResultsFound`.

**The downgrade destroys data, and says so.** Below revision 0004 an allocation row cannot be
represented, so its leases, settled reservations and anomalies go with it. Merging children back
into their parent is not a safer alternative — it would have to invent an answer for a child's
already-`committed` spend. The deletes run in foreign-key order because the obvious single
`DELETE FROM budgets` fails on `leases_budget_id_fkey` as soon as a split has been spent
against; measured, as a teardown error the first time the tests ran.

**A spec correction, not a deviation.** §13 and §15 both recorded the three-sibling outcome as
"granted 100 / 50 / 0". Measured, a probe returned `50 / 100 / 0`: which caller gets the full
amount depends on which transaction takes the row lock first. Stated as a sequence it reads like
a guarantee, and a test written from it would assert the scheduler and flake. The spec now states
what is actually guaranteed — the grants sum to exactly what the pool had — and the tests assert
that.

---

## ADR-020 — The PEP does not handle HTTP trailers, because this stack cannot

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_pep.app`, `PLAN.md` §9 T-018, T-041

**Context:** T-018's acceptance criteria read *"transparent proxying of GET/POST/streaming;
**header and trailer handling**; upstream timeout and retry policy; `httpx` connection pooling;
`/healthz` `/readyz` `/metrics`."* Four of the five were built. The trailer half was measured
first, and it cannot be built on the chosen stack.

**Measured**, against httpx 0.28.1, Starlette 1.6.0 and uvicorn 0.52.3:

| Layer | Trailer support |
|---|---|
| `httpx.Response` | No attribute exposes them — `[a for a in dir(response) if "trail" in a.lower()]` is empty |
| `starlette.responses` | No trailer-related name exists in the module |
| `uvicorn` httptools implementation | The source never mentions trailers |

So the PEP cannot *read* trailers from an upstream response, and could not *emit* them to a
client if it had them. This is not a gap in our code that more work would close; it is absent
from every layer between the socket and the handler.

**Decision:** T-018 ships without trailer handling, and says so rather than quietly satisfying
four fifths of a criterion. Concretely:

* The `Trailer` **header** is treated as hop-by-hop and dropped, which is what RFC 9110 §7.6.1
  requires of a header describing this connection's framing. Announcing trailers the PEP will
  not forward would be worse than saying nothing.
* Actual trailer *fields* are neither read nor emitted.

**Consequences:** an upstream that sends trailers loses them across the hop. In practice this
costs nothing for the demo and very little in general — trailers are rare over HTTP/1.1, and the
usual carrier is gRPC, which is HTTP/2 and out of scope for the HTTP PEP (`PLAN.md` §1.4).

The one place it could matter is T-041's MCP streamable-HTTP gateway, which is **deferred**
(`PLAN.md` §21, ROADMAP Part 1). If MCP is picked up and turns out to need trailers, the change
is not to this module but to the ASGI server underneath it — h11/httptools would have to grow
support first. Recorded here so that ticket starts from the measurement rather than repeating
it.

**Cost:** one acceptance criterion consciously unmet, in a submission judged partly on honesty
about limitations. Stating it in `STATUS.md` §3 alongside the other known gaps is cheaper than a
reviewer finding the silence.

---

## ADR-021 — Datalog execution limits are set explicitly, because the default is 1 ms of wall clock

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_core.tokens`, `tests/property/test_attenuation.py`, TM-25, NFR-1, gap 13

**Context:** `STATUS.md` gap 13 recorded `test_inv1_attenuation_never_widens` as intermittently
flaky — roughly one full-suite run in ten — reporting `FlakyStrategyDefinition` and, once,
`child authorized what the parent did not`. INV-1 is the central security property, so the
question *is INV-1 actually violated?* had to be answered before the flake could be dismissed.

**It is not violated.** Two independent brute-force campaigns found zero violations across
roughly 15,000 request contexts: one with hand-rolled generators and `random`, one driving this
project's own hypothesis strategies while *collecting* violations rather than asserting, so no
shrinking or replay could distort the result.

The cause is a library default, and it is a product bug rather than a test bug. **Measured**
against `biscuit-python` 0.4.0:

```
AuthorizerBuilder("allow if true;").limits()
  max_facts      = 1000
  max_iterations = 100
  max_time       = 0:00:00.001000     ← one millisecond
```

`max_time` is **wall clock, not work.** A query that normally takes microseconds raises
`AuthorizationError: Reached Datalog execution limits` whenever the process loses the CPU for
long enough — which is what a full test suite arranges. `_safe_authorizes` caught that and read
it as *denied*, so parent and child disagreed for a reason having nothing to do with authority.

**How rare, and how it was caught.** Instrumenting the HEAD test to log every exception it
swallowed as a denial: **2 of 42,014** (~0.005%) were execution-limit errors, both raised inside
`verify()`. That rate is why three earlier `make check` failures were each dismissed as
unreproducible, and why 30 runs of `test_attenuation.py` *alone* produce 0 failures — the file on
its own does not generate enough CPU contention.

**The causal chain, proven by fault injection rather than inferred.** Injecting one
`AuthorizationError("Reached Datalog execution limits")` at a chosen authorize call, sweeping the
injection point across twenty calls:

| Injection lands on | Result |
|---|---|
| a child check (even indices) | passes — a spurious denial of the child asserts nothing |
| **a parent check (odd indices)** | **10 of 10 produce the false `child authorized what the parent did not`**; 9 of those 10 additionally report `FlakyStrategyDefinition` |

So: timeout on the parent check → `False` → the assertion fires → hypothesis shrinks and replays
→ the timeout does not recur → the loop no longer exits early → the interactive draw count
differs → `FlakyStrategyDefinition`. Both observed symptoms, from one cause.

Note what that means for the printed counterexample: it was spurious. `child authorized what the
parent did not` named a real disagreement between two authorizations, but the disagreement was
scheduling, not authority.

**Decision:** every authorizer this project builds sets its limits explicitly.

| Limit | Default | Ours | Why |
|---|---|---|---|
| `max_time` | 1 ms | **250 ms** | Generous against work measured in microseconds; still bounded, so TM-14 keeps a ceiling |
| `max_facts` | 1,000 | **10,000** | A depth-8 chain with several caveats per block sits well inside; the default leaves little headroom for chains spec 01 §9 permits |
| `max_iterations` | 100 | **1,000** | Raised proportionally, same reasoning |

`AuthorizerLimits` has no constructor in `biscuit-python`; the builder's own object is fetched,
mutated and set back. That is the supported route, not a workaround. Neither `limits()` nor
`set_limits()` appears in the type stubs, so both carry an ignore — and `test_tokens.py` pins the
defaults and proves the constant is load-bearing by driving `max_time` to zero, so a stub or API
change fails a test rather than silently leaving the limits unset.

Two changes to the property harness go with it, and the second matters more than the first:

* It applies the same limits, and **re-raises** an execution-limit error instead of swallowing it.
  A timeout can never again be read as a denial.
* INV-1's mandate, caveats and probes are drawn as **one composite value** rather than
  interactively through `st.data()` inside a loop that the assertion can exit early. This is what
  makes the failure *legible*: injecting the same fault into the fixed harness now reports
  `biscuit_auth.AuthorizationError: Reached Datalog execution limits` with a traceback to the
  line, instead of `FlakyStrategyDefinition: is your data generation depending on external
  state?`. The root cause is the limit; this is why the bug survived three tickets undiagnosed.

**Consequences:** the ceiling is 250× looser, so a pathological token can occupy a worker for a
quarter second instead of a millisecond. That is the trade and it is the right way round — a
bounded resource cost against denying legitimate requests for want of scheduling. Token size is
already capped at 8,192 base64 characters and depth at 8, which bounds what the engine can be
handed.

**Why this is a product bug and not only a test bug.** `_authorizer()` runs on every `verify()`,
which is the PEP's hot path, and both observed timeouts were inside `verify()`. One millisecond
is also the same order as NFR-1's *entire* decision budget (`PLAN.md` §17): a hot-path library
whose internal timeout equals the system's latency target will fire under exactly the load that
target exists to describe. Measured on a depth-8 chain, a full `authorize()` costs 290 µs quiet
and 478 µs under 24-way CPU contention — against a 1 ms cap, that is under 2× headroom before
any adversarial input.

**Residual, recorded rather than fixed here** — ~~an exceeded limit still surfaces as a raw
`biscuit_auth.AuthorizationError` out of `verify()`, not as a typed error with a reason code~~.
**Closed in T-020**: `verify()` now raises `VerificationLimitError`, a `TokenError` carrying
`VERIFICATION_LIMIT_EXCEEDED`, so a caller catching `TokenError` still gets a reason code
instead of an unexplained 500. Spec 09 §2 step 2 and §7 list it.

**Cost:** this supersedes a written claim. T-019's commit message and journal entry attributed
the flake to `attenuate()` drawing fresh entropy per call and so breaking hypothesis replay. The
ephemeral-key fact is true (spec 01 §4) but the inference was **false**, and measuring it said so:
200 re-mints of identical inputs produced identical token sizes and identical authorization
results. The mint is deterministic in every way the test observes. Superseded in `JOURNAL.md`
T-019 and `STATUS.md` §3; the commit message stands as written, because history is not rewritten
to look better than it was.

---

## ADR-022 — Shape-coverage audits draw from the kind they audit, not from the union

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `tests/property/test_strategies.py`, gap 13

**Context:** `test_strategies.py` audits the generators rather than the code — the roadmap's note
on T-009 is *check the property-test strategies, not just that the tests pass; a weak strategy
passes vacuously.* Each test asserts that some shape spec 03 §6 requires is actually reachable.

Chasing gap 13 turned up a **second, unrelated** intermittent failure in the same directory, and
it was the one that reproduced most readily. `test_zero_ceilings_occur` drew 400 caveats from the
nine-kind union and asserted at least one was a `BudgetCeiling` with value 0. About a ninth of
the union's draws are ceilings and `money()` is `decimals(0, 1000, places=4)`, so whether a zero
appeared was luck the test had no control over.

**Measured**, 60 campaigns of exactly the sampling the test performs:

| Shape | Missed, union of 400 | Missed, kind sampled directly (300) |
|---|---|---|
| **zero ceiling** | **3 / 60** | 0 / 60 |
| empty scope set | 0 / 60 | 0 / 60 |
| depth limit 0 | 0 / 60 | 0 / 60 |
| depth limit ≥ 6 | 0 / 60 | 0 / 60 |
| two-sided time window | 0 / 60 | 0 / 60 |

A 400-draw union sample holds 23–49 ceilings (mean 36); one shape in five is thin enough for that
to matter. Confirmed end to end: at HEAD, `tests/property/test_strategies.py` failed **2 of 30**
runs, both on `test_zero_ceilings_occur` and nothing else.

That ~5% is also, in hindsight, the "residual" rate previously attributed to INV-1 while chasing
gap 13. Two intermittent failures in one directory read as one intermittent failure.

The file's own docstring already names this hazard — `test_every_arg_operator_is_generated` says
*"sampling the union instead would leave only about a ninth of the draws here, and an operator
could be missed by chance rather than by a real gap — which would make this audit flaky, and a
flaky audit gets deleted."* That test was guarded. The shape tests were not.

**Decision:** every coverage assertion about a *specific kind* samples that kind directly, via
`strategies.caveats_of_kind()` — which already existed for exactly this, documented as *"drawn
directly rather than filtered out of the union."* The union is still sampled, but only by
`test_every_caveat_kind_is_generated`, whose subject genuinely is the union.

Rejected alternatives:

* **`derandomize=True`.** Freezes the dice, so the test becomes deterministic — but deterministic
  on one lucky seed. A strategy producing the shape 1% of the time would then pass forever, which
  is the opposite of what an audit is for.
* **Raise the draw count.** Pushes the rate down without bounding it, and buys a probability
  rather than a guarantee at a cost paid on every run.

**Consequences:** the audit now fails when the strategy actually narrows, and not otherwise. The
file runs in 3.8 s.

**Cost:** a coverage audit is still a sampling argument, not a proof; `caveats_of_kind` makes the
sample dense, not exhaustive. Worth stating, because the value of these tests is precisely that
they are believed.

---

## ADR-023 — Extraction buffers the body, so T-018's constant-memory claim becomes conditional

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_pep.extractor`, `agentiam_pep.app`, spec 10 §6, T-018, T-053

**Context:** T-018's module docstring states: *"bodies stream in both directions. Nothing is
buffered, so a large upload or a slow event stream costs the PEP a constant amount of memory."*
That was true and is the reason the proxy reads with `aiter_raw()`.

T-020 must read JSON arguments out of the request body to evaluate `ArgPredicate` caveats. Those
two things cannot both be unconditionally true.

**Measured**, against Starlette 1.6.0:

| Order | Result |
|---|---|
| `json()` then `stream()` | Works. The body is replayed from Starlette's cache, byte-identical |
| `stream()` then `json()` | `RuntimeError: Stream consumed` |

So extraction is possible, it must happen **before** forwarding, and it holds the whole body in
memory while it does.

**Decision:** the guarantee becomes conditional, and is stated that way rather than quietly
weakened.

* A route with **no** `body.` source streams exactly as before. Constant memory, unchanged.
* A route **with** a `body.` source reads at most `max_extract_body_bytes` (default 1 MiB). A
  larger body on such a route is denied with `MALFORMED_REQUEST` rather than buffered.

The cap is not optional. An unbounded read on a path an attacker chooses is TM-14 — control-plane
denial of service — reintroduced at the gateway, and the gateway is the more exposed of the two.

**Alternatives rejected:**

* **Stream and extract incrementally.** A JSON field can appear at the end of the document, so a
  streaming parser still cannot answer "what is `body.amount`" without reading to the end. It
  buys nothing but complexity.
* **Extract from a copy while forwarding concurrently.** The decision must precede the forward —
  that is what a policy enforcement point *is*. Forwarding while deciding would mean the upstream
  has already seen a request that turns out to be denied.
* **Refuse `body.` sources entirely.** `ArgPredicate` over a payment amount is the demo's central
  beat and the reason the caveat language has arguments at all.

**Consequences:** worst-case extraction memory is `max_connections × max_extract_body_bytes` —
100 × 1 MiB with the defaults. Recorded in spec 10 §9 and flagged for T-053's load profile,
because a concurrency bound is not the same thing as a per-request bound and the two are easy to
conflate.

T-018's docstring is amended rather than left standing. A claim that was true when written and
is now conditional is worse than one that was never made: a reader who checks the docstring and
not this ADR gets the wrong answer.

---

## ADR-024 — An argument's type is declared in the mapping, never inferred from its text

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_pep.extractor`, spec 10 §4, spec 02 §4.6

**Context:** `02-caveat-language.md` §4.6 requires numeric `arg` facts to be scaled by 10⁴ so that
one Datalog comparison rule covers every numeric term. The extractor therefore has to decide,
for each extracted value, whether it is a quantity.

The obvious rule is to try parsing it as a number and fall back to string. It is wrong, and the
counterexample is ordinary rather than adversarial: an `account_id` of `"0012"` parses as a
number, extracts as `12`, and a caveat comparing it as a string stops matching the value the
upstream will act on. The failure is silent and it fails **open**.

The same ambiguity exists in the other direction. A JSON body can legitimately carry an amount as
`25.5` or as `"25.5"` depending on the client's language, so reading the JSON type is not a
reliable signal either. And path, query and header values are *always* text on the wire — there
is nothing there to infer from at all.

**Decision:** a source expression may carry a `:number` suffix — `query.limit:number`,
`body.amount:number` — which declares the value is a quantity and must be scaled. Without it the
value is a string.

A `:number` source whose value will not parse as a finite decimal is a **denial**, not a fallback
to string. A caveat that expects to compare a quantity must not silently start comparing text;
that is the same failure as the inference rule, arriving later.

Three further refusals follow from the same reasoning, all measured:

| Input | Why refused |
|---|---|
| More than 4 decimal places | `Decimal("0.00005") * 10**4` is `0.5`. Rounding would enforce a number the caller did not request, in whichever direction the rounding mode chose. Four places is the domain everywhere else (money is `NUMERIC(20,4)`) |
| `NaN`, `±Infinity` | Every comparison against `NaN` is false, so a `reject if` predicate over it never fires and the caveat silently passes |
| A JSON boolean through `:number` | A boolean is not a quantity, and Python's `bool` is an `int` subclass — so without an explicit guard `true` would scale to `10000` |

**Consequences:** the mapping is more verbose, and a policy author who forgets `:number` gets a
caveat that compares text.

That is safe, but only because of a property of biscuit that had to be checked rather than
assumed. `ArgPredicate` compiles to `reject if arg(p, $x), <negated>` precisely so it is vacuous
when the argument is absent — so if a numeric comparison against a *string* term merely failed to
match, the reject would never fire and a mistyped argument would fail **open**. **Measured**,
against a `reject if arg("payment.amount", $x), $x > 50000000` token:

| Term supplied | Result |
|---|---|
| `100` (numeric, under) | allow |
| `50000001` (numeric, over) | **deny** |
| `"100"` (string, under) | **deny** |
| `"50000001"` (string, over) | **deny** |
| argument absent | allow |

Both string rows deny, so the type mismatch is not silently ignored. Forgetting the annotation
costs a false *denial*; the inference rule this ADR rejects would have cost a false
*authorization*. That asymmetry is the whole argument, and it rests on a library behaviour now
pinned by `test_caveats.py::TestArgTermTyping` — which also pins the vacuity of an absent
argument, since spec 10 §2 declines to deny on that basis.

It also means the mapping carries semantics, not just plumbing, which is a second reason
`mapping_version` belongs in the decision record (spec 10 §8).

---

## ADR-025 — The lease pool tops up by replacement, off the hot path, one flight at a time

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_pep.pool`, spec 04 §4.1/§4.2/§12, T-021, T-023

**Context:** spec 04 says a top-up *is* an `ACQUIRE` against the same `(mandate, dimension)` —
"there is no separate operation" (§4.1) — and that `RESERVE` triggers one asynchronously when it
runs short (§4.2). It does not say what the PEP does with the lease it already holds, how many
top-ups may be in flight, or what happens when there is nothing to schedule onto. Three
decisions, and each has a wrong answer that looks reasonable.

**Decision 1 — a top-up replaces the held lease; it does not accumulate.**

The old lease is `RELEASE`d as soon as the new grant lands, returning its unspent remainder to
the pool in the same breath.

The alternative — hold both and sum the remainders — wastes nothing in principle and is worse in
practice. Two leases mean two `expires_at` values to expire early against, two `RELEASE`s to keep
straight at shutdown, and a `remaining_local` that no longer corresponds to any single ledger
row. Spec 04 §2.3 is explicit that the PEP's view is one number per lease; making it a sum across
leases is where `leased` starts to drift, which §2's note already identifies as this protocol's
characteristic failure.

Replacement costs the unspent remainder of the old lease for the moment between the new grant and
the old release — bounded by `lease_size` and by `max_fraction`, and returned rather than lost.

**Decision 2 — top-ups are single-flight per dimension.**

A burst of requests below the low-water mark must produce **one** `ACQUIRE`, not one per request.

Ten concurrent acquires would each take a slice of the pool, and a PEP that cannot spend them
before they expire has stranded most of the budget for the full TTL — spec 04 §14 limitation 1,
which `max_fraction` exists to bound and which this would drive straight to that bound for no
reason. Measured with the guard removed: six requests crossing the mark produced six extra
`ACQUIRE` calls.

**Decision 3 — `reserve()` schedules a top-up but never requires an event loop.**

**Measured:** `asyncio.get_running_loop()` raises `RuntimeError` when called from synchronous
code outside a coroutine — including from a worker thread. `reserve()` is deliberately
synchronous (spec 04 §4.2: "no network, no ledger mutation, no lock"), so it can be called from
either place.

Where there is no loop, the top-up is simply not scheduled and the reserve still succeeds. The
alternative — raising, or blocking to make one — would put a scheduling dependency in the one
code path whose entire purpose is to have no dependencies. A pool that refuses to spend budget it
demonstrably holds, because it could not arrange to fetch more, has the failure exactly backwards.

The cost: a PEP driven purely from worker threads never tops up, and runs its lease down until a
call arrives from the event loop. Acceptable because the PEP is an ASGI application — the hot
path *is* the loop — and stated so that a future thread-pool caller does not discover it.

**Consequences and what was verified.** Each guard was removed in turn and the suite re-run, per
the standing rule that a guard never seen to fire is not a guard. The first pass found **four of
five tests passing vacuously** — the single-flight test never had two concurrent crossings, the
shutdown-drains-top-ups test released its gate before closing, and nothing constructed an unsafe
configuration at all. All five now go red when their guard is removed.

One guard was deleted rather than tested: `_acquire` skipped releasing an old lease that was not
`active`, which is unreachable today because nothing in the pool moves a lease out of `active`
except `aclose()`, and `aclose()` stops top-ups first. A comment marks where T-038's revocation
gossip will make it reachable again.

**Also settled here:** spec 04 §17 Q2, whether heartbeat-based early reclaim is worth it. It is
not — a heartbeat replaces a bound derived from the ledger's own `expires_at` with one derived
from message arrival, which has no bounded lateness, so a live-but-delayed PEP gets its lease
reclaimed underneath it. That is TM-22 through a new channel. Reasoning and resumption trigger
are in spec 04 §17.1.

---

## ADR-026 — A full audit buffer denies the request; `BLOCK` is not offered

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_pep.emitter`, `PLAN.md` §9 T-022, spec 09 step 10, NFR-1, NFR-5

**Context:** `PLAN.md` T-022 requires the back-pressure policy to be *"defined and tested (when
the buffer is full: block, drop, or deny — default is deny, because losing audit records is a
compliance failure, and this choice must be recorded in `DECISIONS.md`)"*. The plan names three
candidates and the default; this records what was actually built and why one candidate is gone.

**Decision: `DENY` (default) and `DROP` (opt-in). `BLOCK` is not implemented.**

**Why deny is the default.** A decision record is the answer to *who authorized this payment?*
The system's entire pitch is chain of custody, and NFR-6 makes the audit ledger tamper-evident —
which is worth nothing if records can go missing under load. So when the buffer is full the
request is refused with `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`: a system that cannot record what
it authorized should not authorize.

This is the one place where step 10, which runs *after* the decision, can change the outcome. It
is deliberate and it is the direction that fails closed.

**Why `BLOCK` is not offered.** Blocking means the hot path waits for the audit sink to drain.
`emit()` is synchronous and runs inside the ASGI event loop, so "blocking" there does not stall
one request — it stalls the loop, and with it every other in-flight request, `/healthz`, and the
lease pool's top-up tasks. A slow audit sink would become a total outage.

`DENY` fails the same requests, per-request, with a reason code, while the process keeps serving
health checks and draining the buffer that caused the problem. It is strictly better on every
axis that matters, so `BLOCK` is absent rather than present-and-discouraged: an option that is
never the right choice is a trap in a config file.

If a future deployment genuinely wants to wait, the honest shape is an *asynchronous* emit on a
path that is already awaiting — not a synchronous block on the loop. That is a different API and
would need its own entry here.

**Why `DROP` exists at all.** Some deployments would rather serve than record — a read-only
reporting PEP, say, where the decisions are low-value and availability is the whole point. It is
opt-in, never the default, and **counted**: `emitter.dropped` increments on every discard,
because a dropped audit record that nothing counts is indistinguishable from one that was never
made.

**Consequences.** A saturated audit sink takes the PEP's availability with it. That is the
intended reading and it deserves stating plainly: the buffer's capacity (1,024 records by
default) and the drain cadence (§ spec 04 17.2's 64-record / 500 ms window) are what stand
between a slow ledger and a refusing gateway. Both are configurable, and T-053's load profile
should measure how much headroom the defaults actually give.

**Writing this entry is what found the bug it now describes.** The first implementation counted
a failed batch and *discarded* it, so a broken sink degraded to silently losing records — losing
exactly what the deny policy refuses requests to protect. The argument above would have been one
the code did not honour.

A failed batch is now **retried**, paced by the drain interval, and stays at the head of the
queue while it is. So a persistently broken sink fills the buffer and `DENY` starts refusing
requests: a broken audit path stops authorization the same way a saturated one does. `max_retries`
(3) then bounds a *poison* batch — one the sink will never accept — after which the records are
dropped and counted in `lost_records`, because one bad record must not wedge the pipeline forever.

A second bug fell out of the same test: `flush()` retried in a tight loop, spending the entire
`max_retries` budget in microseconds against a sink that had had no time to recover. That is not
a retry. `flush()` now gives each batch one attempt and leaves the pacing to the drain loop.

**Measured, and recorded because it is a hot-path cost.** With `opentelemetry-api` installed and
no SDK, `start_as_current_span` plus one attribute costs **5.58 µs** — the tracer is a
`ProxyTracer` and the span a `NonRecordingSpan`, but context attach/detach is real work. Against
`decide()`'s measured ~5.2 µs that roughly doubles the decision; against NFR-1's 1 ms budget it
is 0.56%. The budget is the framing that matters, so tracing is on by default, and
`EmitterSettings.tracing=False` exists so T-053 can measure both. `emit()` plus a span
benchmarks at 6.3 µs mean.

**Also measured:** with no SDK a span's `trace_id` is all zeroes and its context reports
`is_valid=False`. `current_trace_id()` returns `None` there rather than handing back a
correlation handle that correlates every decision to every other one — `DecisionRecord.trace_id`
must come from the request (a `traceparent` header, or one the PEP generates), not from the span.

---

## ADR-027 — T-024 runs before T-023, so enforcement never turns on around a stub

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `PLAN.md` §9 M4/M5 ordering, `STATUS.md` gap 11, T-023, T-024

**Context:** T-023 is the end-to-end thin slice and the point where `/readyz` stops reporting
`enforcing: false`. `decide()` takes five inputs. After T-022, four were real — extraction,
verification, caveats, budget — and the fifth, `PolicyEngine`, had no implementation until
T-024 brought Cedar.

Three ways to reach a working slice were on the table:

1. Ship an allow-all `PolicyEngine` and have `/readyz` report *which* steps are live rather
   than a boolean.
2. Build T-024 first, so all five inputs are real when the slice lands.
3. A `PolicyEngine` that denies everything until configured.

An empty revocation set is honest — nothing can revoke until T-038, so nothing is revoked. An
allow-all policy engine is a different thing: it reports that policy was evaluated when no
policy exists, and it is the exact shape of complaint `STATUS.md` gap 11 was opened to record
(*"a component named policy enforcement point that looks like protection and is not"*).

**Decision:** T-024 moves ahead of T-023. Its only dependency is T-019, which was already
done, so nothing blocked it — the ordering in `PLAN.md` §9 reflects milestone grouping (policy
is M5) rather than a dependency.

This costs nothing but a ticket's delay to the first demoable state, and the project has no
deadline pressure. It buys a slice with no stub in it, no `/readyz` contract change made under
pressure, and no ADR excusing a fail-open default in a security component.

ADR-002 already established that sequencing may change while scope does not; this is that.

**Consequences.** M4's exit gate lands one ticket later. `STATUS.md` gap 11 has now moved four
times (T-019 → T-020 → T-021 → T-023), which is three times too many — the difference is that
the earlier moves were estimates written before reading `decide()`'s signature, and this one is
a decision with the code in front of it.

---

## ADR-028 — Cedar in the hot path: parsed once, decimals not scaled, and anything but Allow is a denial

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_pep.policy`, spec 05, NFR-1, T-025, T-029, T-053

**Context:** T-024 puts a general-purpose policy engine in a path with a 1 ms budget. Four
things about `cedarpy` had to be measured before spec 05 could commit to anything, and three of
them changed the design.

**1. The policy set is parsed once, at construction.** Measured, per authorize:

| Arrangement | Cost |
|---|---|
| Source string re-parsed every call | 167.7 µs |
| `PolicySet.from_str` once | **80.1 µs** |
| Policy set *and* entities pre-parsed | 61.7 µs |

The naive spelling costs 17% of NFR-1's entire budget for nothing. The third row is not
reachable: `PLAN.md` §6.5 puts `depth`, `task_id` and `role` on the principal entity and those
change per request. Moving them into `context` to win it back measured **78.5 µs against 83.0**
— under 5% — so the plan's entity model stands. Cedar treats entities as the durable graph and
context as the request; the idiomatic placement is also the specified one, and 5% is not a
reason to make `principal.depth` unavailable to a policy author who expects it.

**2. `Decision` has three members, and the third is a trap.** `Allow`, `Deny`, and
`NoDecision` — the last returned when the policy set fails to parse, with the errors in
`diagnostics.errors`. So:

```python
if response.decision == Decision.Deny:   # WRONG: NoDecision falls through as "not denied"
```

The engine writes `allowed = decision is Decision.Allow`, so an unrecognised outcome — including
a fourth member added by a future Cedar release — fails closed. A bundle that does not parse is
additionally rejected at **load**, which makes `NoDecision` unreachable in production rather
than merely handled. Verified by mutation: flipping the check to `!= Deny` turns two tests red.

**3. Money crosses into policy as a Cedar decimal, unscaled.** The token layer scales money by
10⁴ because biscuit's Datalog compares integers. Doing the same in Cedar would make the NL
compiler (T-029) emit `context.amount <= 1000000000` for *"no payments over ৳100,000"*, and make
a human reviewing a bundle do the arithmetic.

Measured: Cedar's `decimal` extension holds **exactly four decimal places** — `0.0001` is
accepted, `0.00001` is rejected — which is the same precision as `NUMERIC(20,4)` and
`BUDGET_SCALE`. Money therefore crosses into policy with no scale conversion at all, and a
policy reads:

```
context.amount.lessThanOrEqual(decimal("500000.0"))
```

The cost is method syntax rather than `<=`, because Cedar's comparison operators accept only
`long`, `datetime` and `duration` — measured, `<=` against a decimal is a type error. T-029's
templates must emit the method form.

A bare float in the request context is rejected outright as `NoDecision`, which is the correct
outcome for a system whose rule 6 says money never touches a float, and which the engine already
maps to a denial.

**4. Deny by default, confirmed rather than assumed.** An empty policy set returns `Deny` with
no reasons, and Cedar reports the deciding policy id in `diagnostics.reasons` — so
`PLAN.md` §3.2 principle 4 (*every deny names its cause*) is satisfiable. An engine that could
only answer allow/deny would have failed spec 05 §3.

**Consequences.** The decision costs about **85 µs** instead of ~5 µs, so NFR-1's headroom is
about 12×, not the 200× T-019 recorded. That entry has been corrected: it benchmarked four real
steps and one stub, which is not a benchmark of the pipeline. R-2 (*p99 over 2 ms by M8 triggers
a Rust port*) is **comfortable, not closed**, and T-053 should re-measure with a realistic
bundle — the one variable this ADR cannot bound is how large a policy set an operator writes.

`cedarpy` is a new direct dependency, deliberated per `ENGINEERING-RULES`: it is the official
Cedar engine via PyO3, `PLAN.md` §4 already names Cedar as the policy language, and cp312 wheels
exist for `win_amd64` and manylinux x86_64 so neither the dev host nor CI builds from source.

---

## ADR-029 — Bundle signing: PyCA `cryptography`, signed over canonical JSON, rollback caught by a serial

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_core.bundles`, `agentiam_pep.policy_cache`, spec 05 §5.1–§5.4, T-025

**Context:** T-025 requires a bundle's signature to be verified before use, an unsigned or
badly-signed bundle rejected, and — the interesting one — *an older signed bundle rejected
because bundle version must increase monotonically*. Three decisions, plus a new dependency.

**1. The dependency.** `ENGINEERING-RULES` rule 1 says never write your own crypto, so a
library is not optional; the only question is which. Nothing in the lockfile provided Ed25519:
`biscuit-python`'s `PrivateKey`/`PublicKey` expose only `to_bytes`/`from_pem`, with no detached
sign or verify.

**PyCA `cryptography`** — conventional, audited, Rust-backed, cp312 wheels for `win_amd64` and
manylinux x86_64 so neither the dev host nor CI builds from source. PyNaCl would have done as
well; `cryptography` has the broader install base and also gives PEM/DER handling consistent
with how biscuit keys are already serialized.

It goes in **`agentiam-core`**, which needs justifying because that package is I/O-free by
contract. Signing is pure computation — no clock, no network, no filesystem — and
`test_core_purity.py`'s forbidden list does not and should not include it. The alternative was
duplicating the bundle format in the control plane (which signs) and the PEP (which verifies),
and two definitions of *what is signed* is exactly how a signature ends up covering something
other than what is enforced.

**Rejected:** signing the bundle *as a biscuit*, which would have added no dependency at all —
a token carrying a `bundle_hash` fact, verified with the existing root-key machinery. It works,
and it conflates two mechanisms for one reader's convenience. A reviewer asking *why is your
policy bundle a token?* would be asking a fair question.

**2. What is signed: the canonical JSON, not the wire bytes.** `hashing.canonical_json` — the
same canonicalization the audit chain uses. Two encodings of one bundle must produce one
signature, or re-serializing in transit invalidates a perfectly good bundle and the first person
to hit it concludes the signing is broken.

Every field is covered except the signature itself, and there is a parametrized test per field.
`serial` in particular: leaving it out would let a rollback be presented under a valid signature,
which is the whole attack §5.2 defends against.

**Measured**, and it shaped the API: `Ed25519PublicKey.verify` **raises `InvalidSignature`**
rather than returning `False`. `verify_bundle` keeps that. A boolean invites `if verify(...)`
being written where `if not verify(...)` was meant, and the failure of that typo is *accepting
every bundle* — a silent, total loss of the property. Four tamper shapes were checked and all
four raise: flipped signature bit, altered payload, empty signature, wrong key.

**3. Rollback: a serial, not the version label.** `version` is a human label
(`"2026-08-15.3"`) and is what lands in `DecisionRecord.policy_version`. `serial` is a
monotonically increasing integer, and the cache refuses any bundle whose serial does not advance
— *even when the signature is perfectly valid*, which is the point. An attacker replaying an old
legitimately-signed bundle to restore a removed permission presents nothing a signature check
can object to.

Two fields for what looks like one concept, because string labels do not order: `"v10" < "v9"`
lexicographically, and a rollback defence that depends on how an operator names things is not a
defence. There is a test asserting that inequality, so the reason survives the next reader.

**Consequences, and the one place this system does not fail closed.** A rejected bundle leaves
the *previous* one serving (`PLAN.md` §11 EC-P01) rather than emptying the cache. That is
inconsistent with everything else here and deliberate: a forged bundle is evidence of an attack,
and discarding the last known good policy in response would let an attacker disable the policy
layer by sending garbage — a far cheaper attack than forging a signature. The attacker achieves
nothing, and the staleness clock keeps running underneath, so if the real bundle never arrives
the PEP fails closed on age anyway. Rejections are counted and exposed on `/readyz`, which is
what EC-P01's alert reads.

**Also settled:** spec 05 §9 Q2 — whether `stale` denies immediately or serves a grace window.
It denies immediately. A grace window is a second staleness limit with a friendlier name, and it
turns the failure mode into *policy silently out of date* rather than *policy refused*. The
operator's fix is identical either way, and `max_staleness` is already the knob; setting it to
600 s **is** the grace window, stated once instead of twice.

---

## ADR-030 — The operator activation gate blocks deployment of bundles that fail the 51-case corpus

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_pep.activation`, `agentiam_core.policy_testing`, T-026

**Context:** We need to ensure that a newly pushed policy bundle is not hot-reloaded into the cache if it breaks existing critical workflows (T-026). The decision is to enforce this constraint actively within the activation pipeline, executing a defined test corpus (51 cases derived from the demo workflows) against the new bundle *before* allowing it to replace the active cache. 

**Decision:** The activation gate fully executes the policy corpus in-memory using the `cedarpy` engine on the new bundle. If *any* test fails, the bundle is rejected with an HTTP 409 and is never hot-reloaded. 

**Rationale:** Failsafe deployments. An operator error in Cedar authoring shouldn't cause an outage. Catching errors in the activation gate avoids a scenario where a faulty bundle drops critical traffic.

---

## ADR-031 — The Control Plane hosts the Cedar Authoring UI and shares the corpus from `agentiam-core`

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_controlplane.console`, `agentiam_core.corpus`, T-027

**Context:** The operator needs an interface to edit Cedar policies, run tests against them, and see the exact diff of what will change before they activate the bundle (T-027). The corpus was initially located in `agentiam-pep` for the activation gate.

**Decision:** We moved the 51-case corpus from `agentiam-pep` to `agentiam_core.corpus` so that both the Control Plane (for authoring diffs/tests) and the PEP (for the activation gate) share a single source of truth for the tests. We built the Admin Console in `agentiam-controlplane` using FastAPI, Jinja2, HTMX, and Tailwind for a rich, dynamic authoring experience.

**Rationale:** Moving the corpus to `agentiam-core` prevents duplicating the test suite and risking divergence between what the author sees as "passing" and what the PEP enforces at activation. The stack choices (Jinja+HTMX) align with PLAN.md §4.3 to keep the deployment simple (no frontend build step) while enabling live updates.

---

## ADR-032 — The NL->Policy LLM client uses `httpx` to strictly guarantee no external API egress

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_controlplane.nl_compiler.ollama_client`, pyproject.toml, T-028

**Context:** T-028 requires a deterministic, local LLM client for the natural language compiler with a strict requirement: **never an external API**. 

**Decision:** We implemented `OllamaClient` wrapping `httpx.AsyncClient` with the base URL hardcoded to `http://127.0.0.1:11434`. We explicitly rejected using the official `ollama` Python package because raw `httpx` gives us absolute control to guarantee the client cannot be configured to hit a non-localhost egress. The client hardcodes `temperature=0` and `seed=42` for determinism, and utilizes the Ollama `format` parameter to constrain output generation to a JSON schema.

**Rationale:** Security and predictability. By hardcoding the URL and relying on an internal `httpx` client, we physically prevent external egress and satisfy the demo determinism requirements perfectly. It maps all HTTP errors to a generic `OllamaError` to easily trigger the T-031 fallback template logic without leaking connection details.

---

## ADR-033 — The NL->Policy Compiler auto-generates test cases via JSON Schema and evaluates off-path

**Date:** 2026-08-15
**Status:** accepted
**Affects:** `agentiam_controlplane.nl_compiler.compiler`, T-029

**Context:** The LLM needs to output both the Cedar source code and candidate test requests (T-029).

**Decision:** We use Pydantic models (`CompilerTestCase` and `CompilerOutput`) to define the structure and extract a JSON schema from it. We pass this schema to Ollama's `format` parameter. We also established a separate evaluation script (`evaluate_compiler.py`) with a curated 30-case dataset (`dataset.json`) to benchmark the success rate using the `cedarpy` engine.

**Rationale:** Pydantic is already in the dependency tree and handles JSON schema generation flawlessly. Using a structured output ensures the Control Plane can directly parse the candidate test cases and surface them to the user. The standalone evaluation script keeps the heavy GPU requirement out of the automated CI pipeline.

---

## ADR-034 — Dual-Gating for Generated Policies

**Date:** 2026-08-16
**Status:** accepted
**Affects:** `agentiam_controlplane.app`, T-030

**Context:** The NL->Cedar compiler generates Cedar policy and test cases based on an operator's plain English input. However, verifying the intent of natural language is inherently fuzzy.

**Decision:** We require **both** the auto-generated test suite AND the 51-case master corpus to pass before a generated policy can be activated in the Control Plane.

**Rationale:** The master corpus prevents regression of the global invariants (like NFR-1 limits and immutable rules). The auto-generated tests prove that the LLM's own internal logic about the specific prompt holds true. By gating deployment on *both*, we combine global invariant safety with localized intent safety.

---

## ADR-035 — Stateless Intent Binding for Drift Detection

**Date:** 2026-08-16
**Status:** accepted
**Affects:** `agentiam_pep`, `agentiam_sdk`

**Context:** Drift v0 requires scoring the semantic divergence between the English task intent and the requested action intent. However, the PEP operates without network lookups, so it only sees the cryptographic `intent_hash` embedded in the token's authority block.

**Decision:** The SDK will transmit the plain English task intent via the `AgentIAM-Task-Intent` HTTP header, alongside the `AgentIAM-Action-Intent`. The PEP will hash the task intent using `agentiam_core.hashing.hash_object` and assert it matches the token's `intent_hash`.

**Rationale:** This solves the stateless verification problem for drift. The PEP avoids querying the control plane for the original task string. If the hashes match, the PEP knows the provided English text is authentic and can use it in the Drift Oracle to query semantic embeddings.

---

## ADR-036 — Drift features f3, f4 and f6 are deferred; T-033 ships f1, f2 and f5

**Date:** 2026-08-16
**Status:** accepted
**Affects:** `agentiam_core.drift_features`, `agentiam_pep.drift`, T-033, spec 06 §5

**Context:** `PLAN.md` §6.6 defines six drift features feeding a calibrated logistic
regression. T-033's acceptance criterion asks for all six.

Two facts change what that criterion is worth. First, **T-034 (the labelled dataset) and
T-035 (the classifier) are both deferred**, so nothing consumes a feature vector this cycle
— the six features feed a model that will not exist for the submission. Second, three of
the six need state the PEP deliberately does not hold:

* **f3** scope-in-task-plan prior — needs per-task-type priors.
* **f4** step position / sequence anomaly — needs the request's position in a trace.
* **f6** historical frequency of `(task_type, scope)` — needs history.

Spec 06 §1.1 makes statelessness the load-bearing design choice, and ADR-035's whole
stateless intent binding rests on it. `PLAN.md` sketches a `drift_observations` table to
hold this history; it exists nowhere — no model, no migration, no writer.

**Decision:** T-033 delivers **f1, f2 and f5** — the three computable from the request
alone. f3, f4 and f6 are deferred, with T-034 as the resumption trigger, since the dataset
work is where per-task history first has to exist anyway.

**Cost, stated plainly:** the drift feature vector is half a vector. If T-034 is ever taken
up, the classifier starts from three features rather than six, and the three missing ones
are exactly the *temporal* signals — which is where the slow-drift evasion named in
`PLAN.md` §20 (A-27) would show up. Rule-based v0 already cannot see slow drift; this does
not make that worse, but it does not help either.

**Rejected alternative:** computing all six with f3/f4/f6 returning a neutral constant. That
satisfies "six features computed" literally while shipping three features that measure
nothing, and a recorded constant is indistinguishable in the dataset from a real
observation of zero. A missing feature is honest; a fabricated one is not.

---

## ADR-037 — The embedding client is per-process and warms itself, because cold start is 14 s

**Date:** 2026-08-16
**Status:** accepted
**Affects:** `agentiam_pep.drift.EmbeddingClient`, spec 06 §4.1-§4.2, T-033

**Context:** T-033 asks for the "embedding model cached at startup". That reads like an
optimisation. Measuring it showed it is not.

Against `nomic-embed-text` (768 dimensions) on the development machine:

| | measured |
|---|---|
| cold embedding call (model not resident) | **14,244 ms** |
| warm call, median / p95 (n=20) | 17.8 ms / 83.4 ms |
| `httpx.Client` construction alone, median / p95 (n=20) | **724.7 ms** / 1,603.5 ms |
| cache miss, client-per-call — T-032 as shipped | 747.9 ms |
| cache miss, shared client — T-033 | **83.3 ms** |

Two separate defects in T-032's shipped oracle fall out of this:

1. A cold call takes 7x the oracle's own 2 s timeout, so the **first** scored request after
   the model is evicted always times out and fails open. Drift was not slow on a cold PEP;
   it was absent. Ollama evicts an idle model after roughly five minutes, so this recurs in
   normal operation, not only at boot. `lru_cache` does not memoize exceptions, so every
   request in the cold window re-paid the full timeout.
2. Constructing an `httpx.Client` per cache miss cost a median 724 ms of **blocking work on
   the asyncio event loop**, inside `decide()` — 37% of the 2 s budget spent before a byte
   was sent, and at p95 approaching the timeout on its own.

**Decision:** `EmbeddingClient` holds one `httpx.Client` for the process, caches vectors by
text, and exposes `warm()` — which uses a separate 60 s timeout (the hot-path 2 s cannot
load a cold model) and sets Ollama's `keep_alive` so the model stays resident rather than
being re-warmed one request at a time.

**Rationale:** `warm()` returns a bool and never raises. Startup must not fail because an
advisory heuristic is unavailable (spec 06 §2.1), but the operator should be told, so the
outcome is logged and returned rather than swallowed.

Failures are deliberately **not** cached. The cold window is exactly when calls fail, and
remembering a failure would keep drift dead until the process restarted — converting a
transient outage into a permanent one.

**Note:** this does not fix the blocking-call-on-the-event-loop problem, only its magnitude
(748 ms → 83 ms per miss, and 0 ms on a hit). The sync client still blocks the loop inside an
`async` pipeline. Making the oracle async is its own change, on the ticket that wires
features into the decision record.

---

## ADR-038 — The compiler's timeout was below its own warm median

**Date:** 2026-08-16
**Status:** accepted
**Affects:** `agentiam_controlplane.nl_compiler.ollama_client`, T-028, STATUS gap 18

**Context:** `OllamaClient` shipped with a hardcoded 30 s timeout (ADR-032). The model it
names, `qwen2.5:7b-instruct-q4_0`, was not installed on the development machine, so until
now the number had never met a real generation.

Measured once it was, with the model resident in 5.32 GB of VRAM (`ollama ps` confirms GPU,
so this is not a CPU-fallback artefact):

| | measured |
|---|---|
| cold generation (model load + inference) | **216.3 s** |
| warm generation, n=5 — median | **45.2 s** |
| warm — min / max | 24.5 s / 233.7 s |

The 30 s budget was below the **warm median**. It failed every first call and most
subsequent ones, and reported each failure as `OllamaError("timed out")` — indistinguishable
from Ollama actually being down.

**Decision:** default timeout **300 s**, and `keep_alive: "30m"` on every request so the
model stays resident. `warm()` is added, mirroring ADR-037, and returns a bool rather than
raising — the console has plenty to do that does not involve the compiler.

**Rationale:** this is the operator-initiated authoring path, not a request path. NFR-1
governs `decide()`, not this. A timeout that actually covers the work is worth more than a
number that looks responsive and produces false failures. 300 s clears the worst warm case
observed (233.7 s) with margin, and the cold path (216.3 s) too.

**What this does not fix:** 45 s median is still poor for a live demo beat, and it is the
*floor* — not something a longer timeout improves. `DEMO.md` beat 5 is now honest about the
latency rather than silently failing at 30 s, but the underlying cost is the model's. T-031
(the template fallback, deferred) is the mitigation the plan already identified, and this
measurement strengthens the case for un-deferring it.

---

## ADR-039 — The activation gate is enforced server-side, and refusal is 409 not 422

**Date:** 2026-08-16
**Status:** accepted
**Affects:** `agentiam_controlplane.app`, T-030, ADR-030, ADR-034, STATUS gap 16

**Context:** ADR-034 decided that both the auto-generated tests and the 51-case master
corpus must pass before a generated policy is activated. ADR-030 fixed the refusal status
at 409. Neither was enforced: `POST /policy/activate` assigned `store.current_source` and
returned 200 unconditionally — no corpus, no auto-tests, not even a Cedar parse.
`can_activate` was computed and passed to the template, so the gate existed **only in the
UI**, and a direct POST installed anything, unparseable Cedar included.

The test that existed asserted the endpoint *accepted* a new policy, which documented the
missing gate as correct behaviour.

**Decision:** the endpoint parses the candidate and runs the full corpus before assigning.
On failure it returns **409** with the failing case names, and the previous policy keeps
serving (spec 05 §5.5).

It does **not** call `agentiam_pep.activation.activate_bundle`, though it mirrors it. That
function also verifies an Ed25519 signature and a monotonic serial, and the console has no
bundle signing — `DummyBundleStore` is still a stub. Those two gates arrive with real bundle
publication. The corpus gate is the one with something to check today, and it is the one
that was missing.

**Sub-decision — `source` defaults to `""` rather than being required.** An empty policy
denies everything and would take the demo down, so it must be *refused* (409), not rejected
as a malformed request (422). Measured: httpx drops an empty form value, so a required field
yields 422, while a browser sends `source=` and would reach the gate. Defaulting to `""`
makes both paths agree on 409, and the browser is the path that matters.

---

## ADR-040 — The compiler runs on hosted inference for now; local is the production target

**Date:** 2026-08-16
**Status:** accepted — **supersedes ADR-032's no-egress guarantee**
**Affects:** `agentiam_controlplane.nl_compiler.llm`, T-028, T-029, `DEMO.md` F-1, NFR-10

**Context:** ADR-032 hardcoded the compiler's base URL to `127.0.0.1` specifically so that
external egress was impossible. That was the right call for a system whose selling point is
control over what agents may do — and it is not what the measurements support today.

Against `qwen2.5:7b-instruct-q4_0`, resident in 5.32 GB of VRAM on a development GPU
(ADR-038): cold generation **216.3 s**, warm median **45.2 s**, worst warm **233.7 s**.
Demo beat 5 is budgeted **90 s end to end**. And that is the *favourable* case: the
deployment target is a cloud VM without a GPU, where a 7B model is markedly slower still.
Renting GPU inference is not affordable at prototype stage.

**Decision:** the compiler talks to a pluggable `LLMClient`. `GroqClient` (hosted) is the
default while AgentIAM is a prototype; `OllamaClient` (strictly local, unchanged) remains a
first-class backend. `AGENTIAM_LLM_BACKEND` selects, and with nothing set a present
`GROQ_API_KEY` means Groq while its absence falls back to local — so a machine with no key
still runs rather than failing at import.

**This is a real trade, and it should be stated rather than glossed:**

* **Egress.** The operator's policy text now leaves the machine for the compiler path only.
  Tokens, decisions, budgets, the ledger and the audit chain remain entirely local — the
  enforcement core never had, and still has no, network dependency. What is exported is one
  English sentence and the Cedar it compiles to.
* **`DEMO.md` F-1** claimed "everything is local, zero external dependencies". That claim is
  now false for beat 5 and F-1 is rewritten around the local fallback instead — which is a
  *better* drill, since it exercises a real capability rather than asserting one.
* **Determinism.** ADR-032 claimed reproducibility from `temperature=0` and `seed=42`.
  Hosted inference makes that best-effort at most. The compiler does not depend on it: what
  makes a generated policy safe to activate is the corpus gate (ADR-039), not
  reproducibility. This is worth being precise about, because ADR-032 overstated it even
  locally — a fixed seed constrains sampling, not kernel-level nondeterminism.
* **NFR-10 / §14.4.** Unaffected. The IP claim is about authorship of this repository's
  code, and calling a third-party API no more transfers ownership than calling Postgres
  does. But the *narrative* is weaker — "runs on your hardware" is a stronger story than
  "our prompt against someone's API", which is part of why local remains the target.

**Rationale for the shape rather than a straight swap:** selection is configuration, so the
migration back is a config flip, not a rewrite. `tests/unit/test_llm_backend.py` pins that
property — the promise is checked rather than merely asserted. `OllamaError` now subclasses
`LLMError` so callers, including T-031's deferred template fallback, catch one type
regardless of which backend is live.

**The key never appears in code.** It is read from `GROQ_API_KEY`; `.gitignore` already
covers `.env` and `.env.*`; `.env.example` documents the variable with an empty value. A
missing key raises at construction rather than at first request, because failing when the
console starts is far easier to diagnose than failing the first time an operator writes a
policy on stage. HTTP errors deliberately do not echo the response body, since a provider
error can repeat the request content — the operator's policy text — into the console UI.

**Resumption trigger for the migration:** funded inference hardware, or a customer whose
data residency terms forbid the hop. Either flips `AGENTIAM_LLM_BACKEND` to `ollama`.

### Addendum — the free tier's real limits, measured

Read from the API rather than assumed, after two guessed pacing values both proved wrong:

| | |
|---|---|
| tokens per minute (`x-ratelimit-limit-tokens`) | 12,000 |
| **tokens per day** (from the 429 body) | **100,000** |
| requests per day | 1,000 |
| measured cost of one compile | **~1,811 tokens** |

So the free tier affords roughly **55 policy compiles per day**, and a single 30-case
evaluation run costs ~54,000 tokens — **more than half the daily budget**. Two runs
exhaust it.

Three consequences, all of which bear on the demo rather than just on tooling:

1. **The demo is comfortably inside the limit.** Beat 5 is one compile, and a judge
   experimenting might do five. That is a tenth of a day's budget.
2. **The evaluation is a rationed resource.** `--validate` runs the whole dataset against
   its reference policies with no model at all, so dataset iteration is free; only
   measuring the *compiler* costs quota.
3. **A rate-limited demo fails in a visible place.** `GroqClient` retries 429 with
   `Retry-After`, which covers a burst, but nothing recovers an exhausted daily quota.
   That is a concrete argument for un-deferring T-031's template fallback, or for the Dev
   Tier, before the submission demo.

**The quota is charged per organization, not per key — measured.** Three additional API
keys were tested against the exhausted budget. A trivial `max_tokens: 1` request on each
returned HTTP 200, which looks like fresh quota; a *real* 1,814-token compile on the same
key returned 429 naming `org_01jtxk8hy9exf9p5kkghgbgfna` — the same organization as the
original key. The 200s were headroom, not a new budget.

So issuing more keys from one account buys nothing, and issuing them from several accounts
would be multi-account circumvention of the free tier: against Groq's terms, detectable,
and worst if the detection lands the week of a submission demo. Iterating on the dataset
stays free either way, because `--validate` uses no model.

### Addendum 2 — a third backend, because the daily token cap is the wrong shape

`GeminiClient` is added and becomes the preferred hosted option. The reason is the *shape*
of the limit rather than its size:

| | Groq free | Gemini free (`gemini-2.0-flash`) |
|---|---|---|
| requests / day | 1,000 | 1,500 |
| **tokens / day** | **100,000** | no daily token cap |
| tokens / minute | 12,000 | 1,000,000 |

A 30-case evaluation costs ~54,000 tokens. Against Groq's free tier that is over half a
day's budget; against Gemini's it is 30 of 1,500 requests. The measurement stops being
rationed, which matters because an unmeasurable compiler is how STATUS gap 19 survived
undetected for four tickets.

**The privacy trade is different and must not be glossed.** Google's *unpaid* tier grants
them the right to use submitted content to improve their products, and the submitted
content here is the operator's policy text. That is acceptable for a prototype on exactly
the reasoning already accepted above, unacceptable for a customer deployment, and avoided
entirely by `AGENTIAM_LLM_BACKEND=ollama`. Both providers' paid tiers drop the clause.

Selection order with nothing configured: Gemini, then Groq, then local. The key travels in
the `x-goog-api-key` header rather than the query string, because a URL lands in proxy and
server logs in a way a header does not.

---

## ADR-041 — T-037's persistence half: a config-list root key and approver set, extended
## request bodies, and `max_depth = 1` on every elevation

**Date:** 2026-08-16
**Status:** accepted — both custody mechanisms are named stopgaps, not decisions to build on
**Affects:** `agentiam_core.escalation`, `agentiam_controlplane.{settings,escalations_api,
db.escalations,db.escalation_sink}`, `agentiam_pep.{pipeline,escalation_sink}`, `PLAN.md` §8

**Context:** the T-037 commit that shipped the pure workflow (`agentiam_core/escalation.py`)
deliberately stopped there: "the escalations table, the four HTTP endpoints in `PLAN.md`
932-936, and the console queue" were left for a second pass, because they need Postgres and
FastAPI rather than the rules the ticket's acceptance criteria are actually about. This ADR
covers that second pass, and the four places it had to choose something `PLAN.md` doesn't
fix.

**1. The root signing key.** Approving an escalation mints a fresh root-signed `Mandate`
(the T-037 commit's own reasoning: `attenuate()` cannot widen, so elevation cannot be an
attenuation of the agent's token). No issuance service exists yet to custody that key —
every `mint_root()` call anywhere in the repo before this ticket was test-only. Threat model
A3 names the target ("vault in dev"); until an issuance service exists, `agentiam_controlplane
.settings.ControlPlaneSettings.from_env()` reads a hex-encoded Ed25519 private key from
`AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY`, mirroring `agentiam_pep.config`'s
`AGENTIAM_PEP_*` pattern rather than inventing a second one. Stated as a stopgap in the
module docstring; resumption trigger is the issuance service `PLAN.md` §8 assumes but never
schedules as its own ticket.

**2. Who may approve.** `approve()`/`deny()` need an authorized-approver set, and T-043
(Keycloak OIDC) is not built — there is no session identity to check against. Until it
lands, `AGENTIAM_CONTROLPLANE_APPROVERS` is a fixed comma-separated config list, and the
caller names which approver is acting in the request body (`ApproveRequest.approver`,
`DenyRequest.approver`) rather than it coming from a session. Same shape the already-shipped
`tests/unit/test_escalation.py` uses (`APPROVERS = frozenset({...})`), just sourced from the
environment instead of a test literal.

**3. `POST /v1/escalations`'s body is bigger than `PLAN.md` §8 shows.** The plan's sketch —
`{decision_id, requested_scope, requested_amount, reason}` — is what a UI form would submit,
not what `Mandate` needs to exist (spec 01 §4: `task_id`, `principal_id`, `intent_hash`, none
of which the plan's four fields carry, and which the escalating decision's own context is the
only source of). The endpoint accepts all of them; `agentiam_pep.pipeline.Pipeline.authorize()`
supplies them automatically from the `RequestContext` when an `ESCALATE` outcome fires and an
`EscalationSink` is configured, so a human caller hitting the endpoint directly is the only
path that has to supply them by hand. `POST .../approve` similarly gained `narrowed_scopes`
(plural — `PLAN.md` writes `narrowed_scope`, singular, but a request can name more than one
scope) and both resolution endpoints gained `approver` per point 2.

**4. Every elevated token gets `max_depth = 1`.** `PLAN.md` never fixes this number.
`ElevationGrant` carries what was approved (scopes, amount, validity window) but not a
delegation depth, and the pure `agentiam_core.escalation` module has no opinion on one — the
choice belongs to whoever mints, so it lives in
`ControlPlaneSettings.elevation_max_depth` (default `1`) rather than in core. The grant is
for the escalating agent's direct, one-time use on the task that triggered it, not a new root
of a delegation chain; `1` forbids that token from minting any children at all, which is the
narrowest reading consistent with "elevation is not an attenuation" and costs nothing the
approval didn't already grant.

**Locking, not the pure check alone, proves EC-A10 under real concurrency.** The already-
shipped `agentiam_core.escalation` module's docstring says a persisted implementation needs
an `UPDATE ... WHERE state = 'pending'`; `db/escalations.py` uses `SELECT ... FOR UPDATE`
before calling `approve()`/`deny()` instead — same effect, same pattern `db/ledger.py`
already established for the identical problem. `tests/integration/test_escalations.py` races
ten concurrent approvers against one escalation and asserts exactly one wins, the same shape
`test_ledger_commit.py` uses for `LEDGER_COMMIT`'s G4.

**A failed escalation write fails the request closed.** `Pipeline.authorize()` treats a sink
exception the way `ADR-026` treats a full audit buffer: a system that cannot record *that a
human was asked* must not tell the agent one was asked, so it denies with
`CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` rather than returning an `ESCALATE` response with no
id to follow up on.

**Not done here, and left to the tickets that already own them:** the rich approve/deny
screen with inline narrowing is T-050's acceptance criterion, not this one's — the console
page added here (`GET /escalations`) is a read-only queue with two prompt-and-fetch buttons,
enough to satisfy T-037's own "pending queue in the console" line without pre-building T-050.
A sweeper that persists `EXPIRED` onto a stale `pending` row is not built either;
`state_at()` already makes "TTL expiry auto-denies" true for every reader without one
(`db/escalations.list_by_state` excludes an unswept-expired row from a `pending` query for
the same reason), and the schema's `CHECK` already allows the `expired` state so adding a
sweeper later needs no migration.
