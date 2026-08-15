# Session context — AgentIAM

Hand-off snapshot for resuming work in a fresh session. Gitignored.

**State:** **M1-M4 complete.** T-024, T-025, T-026 were pulled forward from M5. CI green on five jobs. T-027, T-028, T-029, T-030, T-032, and T-036 are complete.
**Next ticket:** **T-033** — ML dataset groundwork (or T-038 Revocation/T-043 Keycloak). Deps: T-032, done.

Read `CLAUDE.md` first for the rules and the environment. This file is only *where things
currently stand*.

---

## Project in one paragraph

AgentIAM gives every AI agent and sub-agent its own cryptographic identity carrying strictly
narrower permissions and a bounded spend budget than its parent, enforced at the tool boundary,
with a complete chain of custody. Pitch framing: *the credit limit and chain of custody for AI
agents*. It is a Bangladesh ICT & Innovation Awards submission, Python everywhere, 100% open
source with no paid service anywhere. Repo: `https://github.com/Sayjad21/agentIAM`.

---

## Where the work is

```
packages/agentiam-core/          the correctness core — zero I/O, 100% covered
  models.py       Budget, Mandate, 9 caveat types, RequestContext, DecisionRecord,
                  validate_label (ADR-013)
  errors.py       ReasonCode (closed enum) + exception tree
  hashing.py      canonical JSON, sha256, audit chain link
  tokens.py       mint_root(), verify(), RootKeySet
  caveats.py      to_datalog(), evaluate(), request_context_datalog()
  attenuation.py  narrows(), attenuate(), atoms(), effective_bound()
  decision.py     decide() — steps 3-7, the precedence contract, ~5 us

packages/agentiam-sdk/           what an agent developer imports — 100% covered
  identity.py     AgentIdentity: token + grant + the caveats this process minted
  context.py      the ContextVar, use_identity(), bind_identity(), run_in_executor()
  client.py       AgentIAM: verify, attenuate, activate
  decorators.py   @requires_scope

packages/agentiam-controlplane/  the ledger
  db/models.py    BudgetRow (pool + allocation rows), LeaseRow, ReservationRow,
                  ReconciliationAnomalyRow
  db/ledger.py    SPLIT / ACQUIRE / RELEASE / REAP / LEDGER_COMMIT — all under FOR UPDATE
  db/invariants.py  check_invariants() — five invariants, one statement, read-only
  db/base.py      make_engine(), make_session_factory()
  db/migrations/  Alembic: 0001_budgets, 0002_leases, 0003_reservations, 0004_budget_split

packages/agentiam-pep/           the gateway — 100% covered
  app.py          create_app(): ANY /proxy/{path}, /healthz /readyz /metrics
  config.py       PepSettings — timeouts, pool limits, worst_case_connect_s
  headers.py      what may cross the proxy hop, and what must not
  lease.py        LocalLease, Reservation, CommitOutcome, reserve(), commit()
  extractor.py    RouteTable, extract() - step 1; refuses ambiguity (TM-26)
  pool.py         LeasePool - sync reserve(), single-flight top-up, BudgetOracle
  emitter.py      DecisionEmitter - deny-on-full, retry-not-drop, OTEL span
  policy.py       CedarEngine.bound(principal) - step 5; NoDecision fails closed
  policy_cache.py PolicyCache - verify, rollback-by-serial, staleness, hot reload
  pipeline.py     the ten steps; emits BEFORE forwarding (ADR-026 needs that)
  revocation.py   InMemoryRevocationSet - honest empty set until T-038

scripts/
  run_invariant_checker.py   the CLI: --once / --json / --interval / --fail-fast

packages/agentiam-demo/          stub only

tests/unit/        core, sdk, schema shapes, pep lease
tests/property/    strategies.py + attenuation invariants + the strategy audit
tests/security/    test_datalog_labels.py — TM-24
tests/integration/ 84 tests, testcontainers, real Postgres — NOT in `make test`
tests/fixtures/    tokens.py — a_mandate(), a_root_client(), frozen_clock()
```

1676 tests: 1558 in `.\make.ps1 check`, 103 in `.\make.ps1 test-integration`, 12 in `.\make.ps1 test-e2e`, 3 in `.\make.ps1 bench`.

---

## Closed: `STATUS.md` gap 13 — and how three diagnoses went wrong

Fixed. Kept here because the *method* is the reusable part; ADR-021, ADR-022 and the
`JOURNAL.md` gap-13 entry carry the detail.

**INV-1 was never violated** — zero violations across ~15,000 contexts in two independent
brute-force campaigns. Ask that question first, before *why is the test flaky?*.

**Two unrelated flakes in `tests/property`, counted as one:**

1. `biscuit-python` 0.4.0's authorizer defaults to `max_time = 1 ms` of **wall clock**. The
   harness caught the resulting `AuthorizationError` and returned `False`, so a busy CPU looked
   like a denial. On a *parent* check that produces `child authorized what the parent did not`,
   and the shrink-replay then produces `FlakyStrategyDefinition`. Measured at 2 in 42,014 calls.
2. `test_zero_ceilings_occur` sampled the nine-kind union and needed a zero-valued ceiling to
   turn up by luck — 3 misses in 60 campaigns.

**Three wrong diagnoses, all written down before being measured:**

* *Entropy breaks hypothesis replay.* False — 200 re-mints gave identical sizes and identical
  results. It had already reached four documents including a pushed commit message.
* *The 1 ms timeout fires constantly.* Wrong in degree — a 5,618-exception sample showed zero.
* *The 1 ms timeout never fires.* Also wrong — the 42,014 sample found two. A 0.005% event is
  exactly what a small sample calls absent.

**What actually settled it, and is worth copying:**

* Instrument the swallowed exception rather than reasoning about it. `except Exception: return
  False` is where causes go to hide — every one of those handlers is a place a bug can look like
  a legitimate result.
* Then **inject the fault** to close the loop. Waiting for a 1-in-21,000 event to recur is not a
  method; forcing it at a chosen call is. Sweeping the injection point gave 10 of 10 failures
  on parent checks and 0 of 10 on child checks — which is a proof, not a correlation.
* When two flake rates don't add up, suspect two bugs.

---

## M4 is done. What M5 needs

`ROADMAP.md` M5 has four sub-tracks. Cheapest-infrastructure-first, and T-024 is already done:

1. **Policy** — T-025 (signed bundles + PEP cache + staleness), T-026 (50-case corpus +
   activation gate), T-027 (authoring UI). **T-027 is the natural next ticket**: its
   dependencies are T-025 and T-026, which are both done.
2. **Revocation + escalation** — T-038/T-039/T-040, then T-037. Replaces
   `InMemoryRevocationSet`'s *source*, not its shape.
3. **Intent + drift** — T-032 makes the intent header meaningful; right now an absent header
   falls back to the token's own intent, which is documented in `pipeline.py` and means
   `INTENT_MISMATCH` fires only when a caller actually asserts one.
4. **Compiler** — T-028…T-031, and the only track needing new infrastructure (Ollama).

T-043 (Keycloak) last; it gates only the console login.

### What the slice left for later, deliberately

* **Step 9 settles the reserved amount, not the upstream's reported charge.** Reading what the
  tool actually charged means buffering its response body. The ledger clamps an over-report
  anyway (spec 04 §4.4 G2), so this is a fidelity gap rather than a safety one.
* **`caveats_for` returns nothing by default.** `decide()` takes the caveat list as an input
  because a token exposes its grant, not what later blocks added (`STATUS.md` gap 2). The
  pipeline accepts a callable and the tests exercise it; wiring a real one needs the
  Datalog→caveat parser that gap 2 tracks.
* **One mandate per pool.** `LeasePool` is per-mandate by construction, so a PEP serving many
  mandates needs a pool per mandate and something to own them. T-021 was scoped to the pool.

---

## Open threads

| # | Thread | Where it is recorded |
|---|---|---|
| 1 | **TM-19, TM-20 have no tests.** TM-21 closed in T-014; TM-22's reaper half closed in T-013, PEP-side skew refusal still open | `threat-model.md` §6 → T-008, T-019, T-051 |
| 2 | **No Datalog→caveat parser.** A chain this process built is checked exactly; a *received* token folds to an upper bound. Whatever writes the parser must not trust block source (TM-24) | `STATUS.md` §3 gap 2 |
| 3 | **Specs 05–09 unwritten.** `06-revocation` and `09-decision-record` are wanted before M4 | `ROADMAP.md` M2 |
| 4 | **No LICENSE file** though Apache-2.0 is declared everywhere | `STATUS.md` §3 gap 3 |
| 5 | **Assumption A1 is verified by hand, not CI.** Still the highest-value missing test | `STATUS.md` §4.1 |
| 6 | **`budgets.mandate_id` has no FK** — no `mandates` table exists, no ticket owns one | `STATUS.md` §3 gap 7, ADR-014 |
| 7 | **`max_fraction` clamp unenforced** until T-015 (deferred). No blast-radius bound beyond `ttl` | `STATUS.md` §3 gap 8, ADR-015 |
| 8 | **Fixture duplication**, both `tests/fixtures/` (4 older unit modules) and `tests/integration/` (T-012 vs conftest) | `STATUS.md` §4.3 |

---

## Decisions already settled

Do not reopen these without reading the ADR.

| ADR | Settled |
|---|---|
| 001 | Infrastructure per-ticket, not front-loaded |
| 003 | Makefile authoritative, `make.ps1` mirrors it |
| 004 | Ruff does not format Markdown |
| **005** | **Blocks carry the grant; the verifier supplies request context.** Depth from `block_count`. Budgets scaled by 10⁴ |
| 006 | T-010 deferred — measured unreachable within `max_depth` |
| **007** | **`check if` for always-present facts, `reject if` for optional ones.** Wrong choice fails open |
| **008** | **`RequiresApproval` is a block fact evaluated in Python** |
| 009 | Commits against a reclaimed lease are rejected and flagged |
| 010 | Idempotency protects the books, not the pool |
| **011** | **A `TimeWindow`'s two sides are separate comparability slots** |
| 012 | Thread propagation carries the identity value, not a copied `Context` |
| **013** | **`role`/`agent_id`/`principal_id` are constrained free text**, not enums |
| 014 | `budgets` ships alone; `mandate_id` carries no FK yet |
| **015** | **`ACQUIRE` uses `min(requested, available)`** — spec 04's `max_fraction` clamp belongs to T-015 |
| 016 | `RESERVE`/`COMMIT` live in `agentiam-pep`; they touch no database |
| **017** | **`LEDGER_COMMIT` locks the lease *before* the dedup check.** The literal spec order is a TOCTOU race |
| **018** | **The checker asserts five invariants; only the pool one has a `CHECK` behind it.** One SQL statement, so the sums share a snapshot |
| **019** | **Proportional split adds an `allocated` column**, not a second meaning for `leased`. Pool uniqueness becomes a partial index |
| 020 | No HTTP trailer support — measured absent from httpx, Starlette and uvicorn alike |

---

## Facts that are easy to get wrong

1. **Biscuit checks are existential.** Written against the token's own grant facts they ask
   *"does some granted value match?"*, not *"is the requested one allowed?"*.
2. **A check whose fact is absent fails.** The request context must carry every budget dimension
   on every call, defaulting to zero.
3. **Minting is not byte-deterministic.** Biscuit embeds a fresh next-block key.
4. **`ToolDeny` and `RequiresApproval` narrow by superset.** Dropping a denial is widening. Three
   test drafts have got this backwards.
5. **`block_source()` is not a faithful round trip** (TM-24). Mint-time validation stops the
   input; consumers must still treat the output as untrusted.
6. **State:**
   - **Current Milestone:** M6 (Verification) / M5 (Drift).
   - **Next Up:** T-033 (Dataset) or T-038 (Revocation).
   - **Core Reliability:** The critical `test_one_authorize_stays_inside_the_budget` NFR-1 benchmark executes in ~100us per request.
   - **Tests:** 1680 total tests passing (100% statement coverage on core).
   - **Control Plane:** Features an Admin Console UI with natural language Authoring, ambiguity handling, and dual test gating (auto-tests + 51-case corpus).
   - **Model Client:** Uses `httpx`-based `ollama_client.py` bound strictly to localhost:11434. Drift oracle implemented with strict fail-open semantics.
7. **A task copies its context at creation, not at first await** — but an awaited bare coroutine
   shares the caller's.
9. **Alembic's `command.upgrade`/`downgrade` call `asyncio.run()` internally**, so they raise
   inside a running loop. The integration fixtures are deliberately *sync* for this reason.
10. **`make test` excludes the integration tests.** Run `test-integration` after touching the
    controlplane or the PEP.

---

## Verify the state on resume

```powershell
$env:Path = "$env:Path;C:\Users\Legion\AppData\Roaming\Python\Python312\Scripts"
cd c:\Users\Legion\OneDrive\Desktop\agentIAM
git log --oneline -5
.\make.ps1 check              # expect: all green, 1558 passed, 83 deselected
.\make.ps1 up                 # Docker Desktop must be running
.\make.ps1 test-integration   # expect: 103 passed
```

If the counts differ from 1080 / 103, this file is stale — check `git log` and update it.
