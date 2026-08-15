# Status

What is built, what remains, and what is worth improving.

**Last updated:** after T-024.
Keep this current at every milestone boundary — a stale status page is worse than none.

---

## 1. At a glance

| | |
|---|---|
| **Milestone** | M1, M2, M3 complete · **M4 in progress** (T-018…T-022 done, T-024 pulled forward) · specs 06–08 outstanding |
| **Tickets** | 21 done · 32 remaining · 8 deferred · **61 defined, 53 in scope** |
| **Tests** | 1347 passing (1260 in `make test`; 84 in `make test-integration`; 3 in `make bench`) |
| **Coverage** (`agentiam-core`, `-sdk`, `-pep`, `-controlplane`) | 100% statements · 99%+ branches |
| **CI** | green — lint/types/tests, **NFR-1 benchmark**, integration against real Postgres, core purity, compose health |
| **Specs** | 7 written — `05-policy` added by T-024; `06`–`08` outstanding |
| **ADRs** | 28 |

---

## 2. Ticket ledger

### Done

| Ticket | Delivered | Commit |
|---|---|---|
| — | Repository bootstrap, docs reworked for solo execution | `b46f8cc` |
| T-001 | uv workspace, 5 packages, CI, compose, **core purity guard** | `4a0367f` |
| T-002 | `specs/01-token-format.md` — measured, not derived | `872f375` |
| T-003 | `specs/02-caveat-language.md`, `specs/03-attenuation.md` | `f26e10c` |
| T-004 | `specs/04-lease-protocol.md` — model-checked | `7a9e9a3` |
| T-005 | `models.py`, `errors.py`, `hashing.py` | `7122cf0` |
| T-006 | `threat-model.md` — 23 STRIDE threats (26 after T-011, gap 13 and T-020) | `c565367` |
| T-007 | `tokens.py` — biscuit mint and verify | `a073b3e` |
| T-008 | `caveats.py` — DSL to Datalog + conformance suite | `1bfa35a` |
| T-009 | `attenuation.py` — `narrows()` + invariant properties | `ff7bb5e` |
| — | `README.md`, `JOURNAL.md`, `STATUS.md` | `e42b0ac` |
| T-011 | SDK: identity propagation, `attenuate()`, `@requires_scope`, **TM-24** | `66f12cb` |
| T-012 | `budgets` table + Alembic migration, DB `CHECK` for the pool invariant, testcontainers | `0905e7a` |
| T-013 | `leases` table + `ACQUIRE`/`RELEASE`/`REAP`, `FOR UPDATE` and clock-skew guards proven load-bearing, partial P-10/P-20 | `0c145ec` |
| T-014 | `RESERVE`/`COMMIT` (`agentiam_pep.lease`, pure) + `LEDGER_COMMIT` (`reservations`, `reconciliation_anomalies`), G2/G3/G4 guards proven load-bearing, TM-21 closed, P-10/P-12 extended | `ca5b83a` |
| — | CI job for the 45 integration tests nothing was running; `make.ps1 help` fixed | `4bff392` |
| T-016 | `db/invariants.py` + `scripts/run_invariant_checker.py` — four invariants, one statement, 3–5 ms over 500 budgets | `ecbd448` |
| T-017 | Sibling budgets: shared pool + proportional split (`allocated`, migration 0004), INV-5 under 3 PEP instances | `0b4d10b` |
| T-018 | PEP gateway: ASGI app, streaming reverse proxy, header hygiene, timeout budget, `/healthz` `/readyz` `/metrics` | `e6d57ec` |
| T-019 | `specs/09-decision-record.md` + `decision.py` — 10 steps, precedence contract, 54 scenarios, **NFR-1 measured at ~5 µs** | `7cf6448` |
| — | Gap 13: explicit Datalog limits, two flaky property tests fixed at their causes (ADR-021, ADR-022, TM-25) | `d63e641` |
| T-020 | `specs/10-scope-extraction.md` + `extractor.py` — route mapping, JSONPath-lite args, declared numeric types, **ambiguity refusal (TM-26)** | `acf6010` |
| T-021 | `pool.py` — zero-network `reserve()`, single-flight top-up, graceful `RELEASE`, crash bound proven by killing a real process (ADR-025) | `e34b698` |
| T-022 | `emitter.py` — buffered emit, **deny-on-full** back-pressure, retry rather than silent loss, OTEL span (ADR-026) | `cd722c6` |
| T-024 | `specs/05-policy.md` + `policy.py` — Cedar engine, 32-case corpus, `NoDecision` fails closed, decimals unscaled (ADR-027, ADR-028). **Pulled ahead of T-023** so enforcement never turns on around a stub | — |

### Next

| Ticket | Delivers | Milestone |
|---|---|---|
| **T-023** | **End-to-end thin slice.** Where enforcement turns on — the first ticket with all five of `decide()`'s inputs. Needs a deliberate answer for `PolicyEngine` (an allow-all stub would report that policy was evaluated when none exists) | M4 |
| T-024…T-043 | Cedar, revocation, escalation, drift, NL compiler, Keycloak | M5 |
| T-045…T-057 | Console, D3 identity tree, Grafana, demo scenarios | M6 |
| T-051…T-059 | Load, chaos, red-team, evidence pack, submission, drills | M7 |

**Also outstanding from M2:** specs `05`–`09` (policy, drift, revocation, audit, decision
record). They gate the tickets that implement them, so spec `06-revocation` and
`09-decision-record` are wanted before M4 rather than before M3.

### Deferred — 8 tickets

Each is a genuine roadmap item with a resumption trigger, recorded in `PLAN.md` §21. Stating
them explicitly is part of the submission's honesty story, not an omission to hide.

| Ticket | Why | Resumption trigger |
|---|---|---|
| T-010 | Token reference for oversized chains. **Measured unreachable**: at `max_depth = 8` a token is 4,940 base64 chars — 60% of the 8 KB limit (ADR-006) | `max_depth` above ~16 |
| T-015 | Adaptive lease sizing. Fixed leases behave identically for the demo; the algorithm is specified in spec 04 §12 | Production traffic with variable rate |
| T-034 | Drift dataset — 2,000+ labelled pairs, weeks of irreducible human labelling | Research phase |
| T-035 | Calibrated ML drift classifier. Rule-based v0 gives the same demo experience | After T-034 |
| T-041 | MCP streamable-HTTP gateway. The HTTP PEP proves the full pipeline; MCP is the adoption path | Adoption push |
| T-042 | MCP scope extractor | With T-041 |
| T-044 | SPIRE workload identity. mTLS achieves the same demo-visible outcome | Enterprise SPIFFE requirement |
| T-060 | Academic paper draft. BIIN scores the demo and report, not a publication | Post-BIIN, USENIX/CCS workshop |

**Reduced but in scope:** T-008 (8 caveat clauses, `RateLimit` dropped), T-024 (`CedarEngine`
only), T-049 (2 dashboards), T-051 (15–20 attacks), T-052 (5 chaos scenarios), T-053 (2 RPS
profiles), T-061 (public repo, no traction push).

---

## 3. Known gaps in what is already built

Real debt, not speculation. Each has a home.

| # | Gap | Impact if left | Where it lands |
|---|---|---|---|
| 1 | **TM-19…TM-20 have no tests.** TM-21 closed in T-014; TM-22's reaper side closed in T-013 — the PEP-side skew-refusal half of TM-22 is still open | Two of four sharpest failure modes are defended only by prose | T-008, T-019, T-051 |
| 2 | **No Datalog→caveat parser.** The SDK now carries the caveats *it* minted, so `attenuate()` catches re-widening along a chain it built. A token received from elsewhere still folds to an upper bound | For a chain this process built, closed. For a received token, the console cannot show a true effective bound. Never understates a restriction — biscuit's append-only structure sees to that | Needed by T-019 (naming the failing caveat) and T-045 (identity tree). **Note:** whatever parses block source must not trust it — see TM-24 |
| 3 | **No LICENSE file.** README and every package declare Apache-2.0; the text is absent | Weakens the §14.4 IP claim for a submission judged on IP ownership | Add via GitHub's license picker — verbatim text matters |
| 4 | **`mutmut` not yet run.** T-009 asks for ≤10% surviving mutants on `attenuation.py` and `caveats.py` | Coverage says the lines run; mutation says the assertions bite. Untested claim | M7, one run (ROADMAP Part 1) |
| 5 | **Specs 05–09 unwritten** | Their tickets cannot start spec-first, which is the rule that caught seven design errors | M2 tail / M5 |
| 6 | **A1 re-verification is manual.** The security rests on biscuit scoping block facts; nothing fails if a library upgrade changes it | Silent collapse of INV-1 on a dependency bump | Should become a test — see §4 |
| 7 | **`budgets.mandate_id` carries no foreign key.** No `mandates` SQL table exists yet — T-005 built `Mandate` as a pure Pydantic model, no persistence (ADR-014) | A budget row can reference a mandate id that was never issued; nothing in the schema catches it | Whichever ticket first persists mandates (issuance service, `PLAN.md` §8 — not yet its own ticket) |
| 8 | **`ACQUIRE` does not clamp by `max_fraction`.** Spec 04 §4.1's clamp is mathematically incompatible with T-013's own acceptance test, applied to a fixed caller-`requested` amount (ADR-015, measured) | No single-PEP-crash blast-radius bound beyond `ttl` — a PEP can be granted more than a quarter of the pool in one `ACQUIRE` | T-015 (adaptive lease sizing, deferred) — that's where the formula actually applies |
| ~~9~~ | ~~**CI ran none of the 45 integration tests.**~~ Closed while integrating M3: `make test` and the CI workflow both excluded the `integration` marker, so every ledger race test ran only by hand | Three tickets of ledger correctness — `FOR UPDATE` serialization, the 50-concurrent-acquire bound, the ADR-017 dedup race — were gated by nothing and would have rotted silently | Closed: `integration` CI job + `make test-integration` |
| ~~13~~ | ~~**`test_inv1_attenuation_never_widens` is intermittently flaky**~~ — closed. **Two** unrelated flakes, and the recorded cause was wrong twice. INV-1 is *not* violated: zero violations across ~15,000 contexts in two brute-force campaigns. The real cause is `biscuit-python`'s authorizer defaulting to `max_time = 1 ms` of **wall clock** — measured at 2 of 42,014 authorize calls under suite load, both inside `verify()`. The harness read that as a denial, so the parent "denied" what the child allowed. Fault injection proved it: 10 of 10 timeouts injected on a parent check reproduce the false `child authorized what the parent did not`, 9 of them with `FlakyStrategyDefinition` on top. The second flake was `test_strategies.py::test_zero_ceilings_occur`, a shape audit sampling the nine-kind union — 3 misses in 60 campaigns | The printed counterexample was **spurious**, and INV-1 stands. But this was a *product* bug on the PEP hot path, not only a test bug: a loaded PEP would have denied legitimate requests for want of scheduling, and 1 ms is the whole of NFR-1's decision budget | Closed: ADR-021 (explicit Datalog limits, harness re-raises, INV-1 draws its scenario as one composite value), ADR-022 (`caveats_of_kind`), TM-25 |
| 11 | **The PEP enforces nothing.** T-018 built the gateway; it forwards every request, token or not. `/readyz` reports `enforcing: false` and `TestEnforcementIsNotWiredYet` pins it | A component named *policy enforcement point* that looks like protection and is not. Deliberate and visible, but it is the single most misleading state in the repo | **T-023.** Steps 1–4 and 7 are built (T-020, T-007, T-019, T-021); what is missing is a `RevocationOracle` and a `PolicyEngine`. An empty revocation set is honest — nothing can revoke yet — but an allow-all policy engine would report that policy was evaluated when none exists. **This estimate has moved twice** (T-019 → T-020 → T-021 → T-023); each earlier guess predated reading `decide()`'s signature, and the journal says so |
| 12 | **No HTTP trailer support, and none available.** Measured across httpx, Starlette and uvicorn: no layer exposes trailers (ADR-020) | One T-018 acceptance criterion consciously unmet. Costs nothing for the demo; trailers are rare on HTTP/1.1 | Only reopens if T-041 (MCP, deferred) needs them — and the fix would be in the ASGI server, not here |
| 10 | **`tests/integration/test_budget_schema.py` duplicates `conftest.py`'s fixtures.** T-012 predates the shared conftest; noted in its header and left alone | Two copies of the container and migration fixtures drift apart | Same cleanup as §4.3 |

---

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

---

## 4. Improvements worth making

Not in any ticket. Ordered by value per hour.

### 4.1 Pin assumption A1 with a test — **high value, ~30 minutes**

The entire design rests on biscuit scoping block facts so a later block cannot widen authority.
It was verified by hand in T-002 and is re-stated in `threat-model.md` §4 as the load-bearing
assumption. Nothing enforces it.

A dozen-line test that appends a block injecting `operation`, `scope`, `requested` and
`current_depth` facts, and asserts it authorizes nothing it was narrowed out of, converts *"we
checked once"* into *"CI checks every push"*. Given the project is a security submission, this
is the highest-value missing test in the repository.

### 4.2 Property-test the `to_datalog`/`evaluate` agreement — **high value, ~1 hour**

The conformance suite covers 60 hand-written cases. The agreement is a security property
(ADR-008), and the generators from T-009 already exist. Turning it into a hypothesis property
over random caveats and contexts would search the space rather than the cases I thought of —
exactly the move that found ADR-011.

### 4.3 Finish the test-fixture module — **medium value, ~40 minutes**

Started in T-011: `tests/fixtures/tokens.py` now holds `a_mandate()`, `a_root_client()` and a
frozen clock, and the four SDK and security test modules use it. The four *older* modules still
carry their own near-duplicate copies. Migrating them, and adding the deterministic keypair and
golden depth-4 chain `PLAN.md` §10.4 calls for, would make the M3–M4 integration tests cheaper
to write and comparable to each other.

The same duplication now exists on the integration side: `tests/integration/conftest.py` (T-013)
and `tests/integration/test_budget_schema.py` (T-012) hold two copies of the container and
migration fixtures, which the latter's header states plainly. One cleanup covers both.

### 4.4 Make the milestone review a script — **medium value, ~1 hour**

`ENGINEERING-RULES.md` §4 and §5 define a self-review checklist and a spec-drift check. Both are
currently discipline. Several items are mechanical — TODOs without ticket ids, deny paths not
mapped to a reason code, floats near money, I/O in core. A `scripts/review.py` emitting the
findings table would make the checks reliably run rather than reliably intended.

### 4.5 Benchmark the decision path early — **medium value, ~2 hours**

NFR-1 (p99 < 1 ms in-process) is a headline number and T-019 is where it gets measured. But
`caveats.evaluate()` and `attenuation.narrows()` already exist. Benchmarking them now would find
a structural latency problem while it is cheap to fix, rather than at M4 when the pipeline is
built around them. Risk R-2 names exactly this: *if p99 > 2 ms by M8, port `decision.py` to
Rust* — a decision far better informed early.

### 4.6 CI matrix on Linux and Windows — **low value, ~30 minutes**

Development is on Windows, CI runs on Linux. Two of the problems so far were platform-specific:
the mojibake corruption and the missing `make`. The test suite is fast enough that adding a
Windows job costs little and would catch path, encoding and line-ending issues at push time.

### 4.7 Record the biscuit version in the evidence pack — **low value, ~15 minutes**

Every measured claim in specs 01–03 is *"against `biscuit-python` 1.x"*. Pinning the exact
version in `uv.lock` is already done; surfacing it next to the measurements makes them
reproducible by a judge or a reviewer, which is the point of measuring rather than asserting.

---

## 5. Risks currently live

From `PLAN.md` §17, with the current reading.

| Risk | State |
|---|---|
| **R-2** Python latency undermines the story | Unmeasured. §4.5 above would de-risk it early |
| **R-3** Lease protocol bug found late | **Reduced.** Model-checking in T-004 found one before implementation (ADR-009); guard-proof testing in T-014 found a second, narrower one — a TOCTOU race in `LEDGER_COMMIT`'s literal spec order — before it shipped (ADR-017) |
| **R-8** Single developer, no second reviewer | Live and structural. Mitigated by specs-first and property tests, which caught six errors review would not have |
| **R-6** Scope creep | Holding. Every deviation so far is an ADR with a stated cost |
| **R-1** Hyperscalers ship equivalent agent IAM | Untracked. §17 says review monthly |

---

## 6. What "done" looks like from here

The gate that matters most is **T-023, the end-to-end thin slice** in M4: an agent with a root
token calls a stub tool through the PEP, spends budget, is denied when exhausted, and the
decision lands in the audit ledger.

Everything before it is foundation, everything after it is surface. It is also the first point
where the project is demonstrable — the roadmap says record a screencast there, and that advice
is worth taking.
