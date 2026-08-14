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
