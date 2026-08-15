# AgentIAM — Build Roadmap

> **Scope:** the full BIIN submission — all 7 milestones, all 8 demo beats, including the
> NL→Cedar compiler and drift detection. Nothing is cut for being a single-developer build.
>
> **Sequencing:** one dependency-ordered track. `PLAN.md` is authoritative on *what* each
> ticket builds; this file governs *order* and *exit gates*. Standards are in
> `ENGINEERING-RULES.md`.
>
> **Timeline:** no fixed deadline. Milestones advance on their exit gate, not on a date.

---

## Part 1 — Scope decisions carried from the 12-month plan

### 1.1 Deferred, and stated as deferred

These are genuine roadmap items, not cut corners. Each is recorded in `PLAN.md` §21 with a
resumption trigger, and each should be named honestly in the submission rather than hidden.

| Item | Ticket | Why deferred | What is claimed instead |
|---|---|---|---|
| Academic paper draft | T-060 | BIIN scores the demo and technical report, not a publication. The specs and evidence pack carry the academic content. | "Paper in preparation." |
| Full OSS release + traction | T-061 | Community traction is a post-award activity. Apache-2.0 licence and a public repo are enough. | "Post-award release planned." |
| SPIRE workload identity | T-044 | mTLS between services achieves the same demo-visible outcome. SPIRE is infrastructure complexity for zero demo value. | ADR: "mTLS in place; SPIFFE deferred as production hardening." |
| OPA as a second policy backend | T-024 | The `PolicyEngine` protocol interface still exists with `CedarEngine` behind it. A second backend is a talking point, not a demo beat. | Interface abstraction is visible in the code; `OpaEngine` is a stub raising `NotImplementedError`. |
| Adaptive lease sizing | T-015 | Fixed-size leases behave identically for the demo. The adaptive algorithm is specified in `specs/04-lease-protocol.md`. | "Specified; production roadmap." |
| Trained ML drift model + dataset | T-034, T-035 | Dataset curation is weeks of irreducible human labelling. Rule-based v0 produces the same demo experience (escalation on drift). | "v0 rule-based shipped; ML pipeline designed, dataset partially constructed." |
| Full mutation-testing iteration | §10.2 | Run `mutmut` once and keep the output as evidence. Chasing every surviving mutant is optimization, not evidence. | One `mutmut` result in the evidence pack. |
| Schemathesis API contract fuzzing | §4.4, §10.1 | Not demo-visible. Unit, integration, and e2e coverage is the priority. | — |
| k6 as a secondary load tester | §4.4 | Locust alone is sufficient; two load tools is redundant. | — |
| Token reference / large-chain overflow | T-010 | The demo uses depth 3–4; the 8 KB header limit will not trigger. | Size guard still implemented and tested. |
| `RateLimit` caveat type | §6.2 | The other 8 caveat types cover every demo scenario. | 8 caveat types, stated as 8. |
| MCP gateway + scope extractor | T-041, T-042 | The HTTP PEP demonstrates the full 10-step pipeline. MCP is the adoption path, not the correctness story. | "MCP gateway is the adoption surface; HTTP PEP proves the pipeline." |
| APICTA expansion deep-dive | §19 | One slide, not a document. | One slide. |
| Patent/invention log | §18.2 | Not scored at BIIN. | Post-award if filing is pursued. |

**Reduced rather than dropped:** T-051 → 15–20 red-team attacks instead of 33.
T-052 → 5 chaos scenarios (CH-1, CH-3, CH-4, CH-8, CH-10) instead of 12.
T-053 → 2 RPS profiles instead of 3. T-049 → 2 Grafana dashboards instead of 4.
T-008 → 8 caveat types instead of 9.

**Added, not in the original ticket list:** rule-based drift v0 (replaces T-034/T-035);
`seed_demo.py` with realistic BD company names and BDT amounts; the Cedar authoring UI (T-027)
merged with T-030 to form demo Beat 5.

### 1.2 Looks skippable — is not

| Item | Why it looks skippable | Why it stays |
|---|---|---|
| **Property tests P-01, P-02, P-06, P-10** | "There are already unit tests." | They are the *evidence* that the attenuation invariants hold. When a judge asks "how do you prove this?", these are the answer. Cheap to write, impossible to retrofit credibly. |
| **Invariant checker (T-016)** | "It's just a monitoring script." | It runs live on screen during Beat 4 while three agents spend concurrently. Roughly 50 lines, and the single most persuasive thing in the demo. |
| **Demo failure drills (T-058)** | "We'll wing it." | A demo failure in front of judges is fatal. Rehearsed recovery is the difference. This is irreducible human time — budget it. |
| **Spec documents (T-002…T-004)** | "The plan already exists, why write specs?" | Judges may read `docs/specs/`. More importantly, the property tests in T-009 are written *from* the specs — a wrong spec produces a property test that proves nothing. |
| **Threat model (T-006)** | "Security theatre." | BIIN scoring explicitly weighs security. A threat model with test-id mappings is the highest value-per-page document in the repo. |
| **`DECISIONS.md`** | "Nobody reads ADRs." | It demonstrates engineering discipline, and it is where every "deferred X because Y" claim in the submission gets its receipt. |

---

## Part 2 — Work that cannot be rushed

Writing code is the fast part. These are the tasks where the clock is set by something other
than typing speed — first-time infrastructure setup, integration debugging across process
boundaries, and rehearsal. Plan around them; they do not compress.

### Category A — Infrastructure and environment

Introduced per-milestone rather than all at once (see ADR-001). Each is a known multi-hour task
the first time.

| Task | What makes it slow | Lands in |
|---|---|---|
| Docker Compose first bring-up | Image pulls, port conflicts, volume permissions, healthcheck ordering, `depends_on` semantics | M1 (Postgres + Redis only) |
| Alembic migrations against real Postgres | Testcontainers vs. a live DB; verifying `CHECK` constraints actually fire | M3 |
| Ollama + Qwen2.5-7B download | ~5 GB pull, first inference, CPU/GPU detection | M5 |
| Keycloak realm configuration | OIDC realm, client registration, users, redirect URIs. 2–4 hours if it is your first time. Export to JSON immediately for reproducibility. | M5 |
| Grafana datasource wiring | Prometheus, Tempo, Loki datasources; dashboard JSON import | M6 |
| Full-stack cold start under 90 s (NFR-8) | Iterating on compose ordering and healthchecks once all services are present | M6/M7 |

### Category B — Integration debugging

Each side of these works in isolation. Making them talk over real HTTP with real tokens is
where the time goes.

| Task | Why it is hard | Lands in |
|---|---|---|
| Biscuit token round-trip across services | Mint → serialize → HTTP header → PEP extract → verify → authorize. Every boundary is a potential bug. | M4 |
| PEP ↔ control plane | Two correct halves that have never spoken to each other | M4 (T-023) |
| **End-to-end thin slice (T-023)** | The first time everything works together. **The hardest single item in the project.** | M4 |
| Redis pub/sub revocation delivery | Passes in unit tests; needs real debugging across a multi-PEP compose setup | M5 |
| Cedar bundle fetch + cache | Bundle signing, PEP fetching, signature verification, hot reload, staleness guard | M5 |

### Category C — Content and curation

Machine-assisted drafting does not remove the judgement these require.

| Task | The judgement involved | Lands in |
|---|---|---|
| Demo seed data | Must look real: Bengali company names, plausible BDT amounts, coherent agent roles and scopes | M6 |
| Cedar policy corpus (50 cases) | Generating 50 cases is easy; curating them so they reflect the actual demo workflows is not | M5 |
| Drift v0 rule set | Keyword lists, cosine thresholds, scope↔task mapping. The code is trivial; choosing the thresholds is the work. | M5 |
| Evidence pack content | Architecture diagrams, benchmark tables, chaos results, red-team results — assembled, then checked for claims the data does not support | M7 |
| Submission package (T-059) | Architecture doc, TAM, scalability roadmap, IP statement, socio-economic impact, in BIIN's required format | M7 |

### Category D — Demo preparation

Entirely human time. No part of this compresses.

| Task | Notes | Lands in |
|---|---|---|
| Demo scenario scripting (T-057) | All 8 beats individually runnable, deterministic, resettable. `make demo-reset` under 10 s. | M6 |
| Failure drills (T-058) | 7 scenarios, each rehearsed, each with a documented recovery under 30 s | M7 |
| Judge one-pagers | 3 of them (Bank CTO, Professor, Trade judge) | M7 |
| Screencast fallback | 90-second video of the full demo, on a phone | M7 |
| Second machine | Same compose stack, pre-warmed. **Solo, it must be already connected and switchable — there is nobody to swap cables mid-demo.** | M7 |
| Projector resolution test | Console readable at 1024×768 | M7 |

---

## Part 3 — Milestone track

Ticket IDs, deliverables, and full acceptance criteria are in `PLAN.md` §9. The **Build** column
is the implementation work; the **Verify** column is what proves it — usually against real
infrastructure, which is why it is listed separately.

### Milestone 1 — Foundation

**Infrastructure: Postgres + Redis only.**

| Ticket | Build | Verify |
|---|---|---|
| T-001 | uv workspace (5 packages, stubs), `pyproject.toml`, Makefile (`up/test/bench/lint`), ruff + mypy --strict + pytest config, pre-commit, GitHub Actions, minimal `docker-compose.yml` | `docker compose up` healthy; `make test` green on empty suite; CI green; the `agentiam-core` purity check actually fails when an I/O import is added |
| T-002 | `docs/specs/01-token-format.md` | Read back against §6.1: authority-block facts, attenuation-block facts, all checks, size limits, 3-level worked example with byte counts |
| T-003 | `docs/specs/02-caveat-language.md`, `03-attenuation.md` | 8 caveat types with Datalog mapping; `narrows()` semantics per type; INV-1…INV-10 stated formally; ≥3 counterexamples each for INV-1, INV-5, INV-9 |
| T-004 | `docs/specs/04-lease-protocol.md` | All 7 operations in pseudocode; lease state machine; safety and liveness arguments written out; partition behaviour; clock skew; idempotency; explicit known-limitations section |
| T-005 | `models.py`, `errors.py`, `hashing.py` — frozen Pydantic models, canonical JSON | 100% branch coverage on `models.py`; property test: canonical serialization stable across dict ordering and Unicode normalization; float rejected for every money field |
| T-006 | `docs/threat-model.md` | ≥12 STRIDE threats, each mapped to the test id that covers it. Delivered 23: 15 mitigated, 5 partial, 3 accepted risks, plus 4 coverage gaps routed to T-008/T-013/T-014/T-019/T-051 |

**Exit gate:** compose healthy on Postgres + Redis · `make test` and CI green · four specs
written and read back against §6 · canonical-serialization property test passing.

> The specs are not paperwork. T-009's property tests are written *from* them. Do not start M2
> with a spec you have not read back critically.

---

### Milestone 2 — Token layer

| Ticket | Build | Verify |
|---|---|---|
| T-007 | `tokens.py` — biscuit mint and verify | `biscuit-python` behaves as the spec assumes in this environment — confirm before building on it |
| T-008 | `caveats.py` — 8 caveat types → Datalog, table-driven tests | Generated Datalog is correct for each type, by inspection against `specs/02` |
| **T-009** | `attenuation.py` — narrowing algebra, `narrows()`, hypothesis property tests for INV-1, 2, 6, 7, 9 | **The most important ticket in the project.** Check the property-test *strategies*, not just that the tests pass — a weak strategy passes vacuously |
| T-011 | SDK `client.py`, `context.py`, `identity.py`, `decorators.py` — identity propagation, `attenuate()` | Token isolation holds under concurrent asyncio tasks. **Done** — 100 tasks, distinct tokens, zero cross-task visibility; thread boundary handled per ADR-012 |
| — | Remaining specs `05-policy` … `09-decision-record` | Quick read-back |

**Exit gate:** tokens mint, attenuate, verify · all 8 caveat types compile to Datalog ·
P-01, P-02, P-05…P-09 pass · SDK propagates tokens across asyncio tasks without leakage.

**Status: code complete.** The five specs `05`–`09` remain; they gate the tickets that implement
them, not M3, so M3 starts now and the specs land against the milestone that needs them.

---

### Milestone 3 — Ledger, leases, budget enforcement

**Infrastructure added: Alembic against real Postgres, testcontainers.**

| Ticket | Build | Verify |
|---|---|---|
| T-012 | Budget schema — SQLAlchemy models + Alembic migration, `NUMERIC(20,4)` | **Done.** Migration runs against real Postgres; `CHECK` constraints reject bad rows |
| T-013 | ACQUIRE / RELEASE / REAP + 50-concurrent-acquire test | **Done.** `FOR UPDATE` and the skew margin each removed to prove they are load-bearing; spec 04's `max_fraction` clamp found incompatible with this test (ADR-015) |
| T-014 | RESERVE / COMMIT / refund, idempotent | **Done.** Decimal exact to 4 places; replayed commits are no-ops; spec 04 §4.4's statement order found to be a TOCTOU race (ADR-017) |
| T-016 | `scripts/run_invariant_checker.py` | **Done.** Detects an injected violation of each of its four invariants; measured at 3–5 ms over 500 budgets against a 1 s bar; swept under concurrent load to prove it does not cry wolf |
| T-017 | Sibling budgets: proportional split + shared pool (INV-5) | **Done.** Three PEP instances, three separate engines; grants sum to exactly the pool. Spec 04's stated `100 / 50 / 0` corrected — that is lock order, not a guarantee (ADR-019) |

**Exit gate — met.** All lease operations working · P-10 green · invariant checker proven
against a real injected violation · sibling budget test passing under both mitigations.

> The `integration` marker is excluded from `make test` because it needs Docker, so these tests
> run under `make test-integration` and in their own CI job. That job was added while picking
> M3 back up — until then nothing ran them automatically, which made every race test above
> decoration in exactly the sense T-016's Verify column warns about.

---

### Milestone 4 — PEP and the first end-to-end slice ⭐

**Infrastructure unchanged — this slice needs neither Keycloak nor Grafana.**

| Ticket | Build | Verify |
|---|---|---|
| T-018 | PEP ASGI app, httpx reverse proxy, `healthz`/`readyz`/`metrics` | **Done.** GET/POST/PUT/PATCH/DELETE, streaming verified over a real socket, header hygiene measured against a live upstream. Trailers unsupported on this stack (ADR-020). Enforces nothing yet — `/readyz` says so |
| T-019 | `decision.py` — the pure 10-step pipeline, 40+ scenario tests, `pytest-benchmark` | **Done.** 54 scenarios; every reason code reachable or declared unreachable; **NFR-1 measured at ~5 µs mean, ~200× inside the 1 ms budget** — R-2's Rust-port trigger can be considered closed. Spec 09 written first, and its precedence contract is the substance |
| T-020 | Config-driven scope mapping + JSONPath argument extraction | Correct against 15 route patterns |
| T-021 | Local lease pool — zero-network reserve, async top-up | Verify with a socket monkeypatch that the reserve path makes **no** network call |
| T-022 | Buffered async decision-record emitter + OTEL span | Back-pressure policy is deny-on-full-buffer, and that path is tested |
| — | Audit hash chain, `/audit/custody` endpoint, `scripts/verify_audit_chain.py` | Chain verification detects single-record tampering (NFR-6) |
| — | Stub tool servers: invoice, vendor, payment, email | PEP routes to each correctly |
| — | Stub tool servers: invoice, vendor, payment, email | Next |
| T-024 | Cedar engine + `PolicyEngine` protocol | **Done, pulled forward from M5** (ADR-027) so T-023 never wires enforcement around an allow-all policy stub. 32-case corpus; `NoDecision` fails closed; money crosses into policy as a Cedar decimal at the same 4 places as `NUMERIC(20,4)` |
| **T-023** | **End-to-end thin slice** | **The critical checkpoint.** Root token → PEP → stub tool → budget spent → denied on exhaustion → decision in the audit ledger |

**Exit gate:** the T-023 chain works end to end, denials included.
**Record a screencast here** — this is the first demoable state, and having it on video de-risks
everything downstream.

> Do not start M5 until T-023 is genuinely green. If it does not work, nothing after it matters.

---

### Milestone 5 — Policy, revocation, drift, compiler

Four sub-tracks, ordered cheapest-infrastructure-first so that heavy setup is not blocking
correctness work.

**5a — Policy** (no new infrastructure)

| Ticket | Build | Verify |
|---|---|---|
| T-024 | `CedarEngine` + `PolicyEngine` protocol + `OpaEngine` stub, 30-case conformance suite | `cedarpy` works in this environment |
| T-025 | Signed policy bundles, PEP cache, signature verification, staleness guard | Rollback attack rejected: an older correctly-signed bundle must not be accepted |
| T-026 | 50-case policy test corpus + activation gate | Cases curated for realism against the actual demo workflows |

**5b — Revocation and escalation** (Redis, already present)

| Ticket | Build | Verify |
|---|---|---|
| T-038 | Revocation service, Redis pub/sub push, periodic pull reconciliation | Converges even when pub/sub messages are dropped |
| T-039 | PEP revocation cache — Bloom filter + exact set | Property test: 10k random non-revoked ids produce zero false denials |
| T-040 | Subtree revocation, depth-4 tree | Propagation to 3 PEP instances < 2 s p99 (NFR-4); record the number for the evidence pack |
| T-037 | Escalation workflow — approval queue, new-token issuance, TTL expiry | Test asserts the original token is **unchanged**; elevated token is ≤ what was requested |

**5c — Intent and drift** (no new infrastructure)

| Ticket | Build | Verify |
|---|---|---|
| T-032 | `intent.py` — canonicalization + hashing | Unicode edge cases, including Bengali text |
| — | Drift v0, rule-based: keyword + cosine threshold | The rule set and thresholds are a judgement call — choose them deliberately and write down why |
| T-036 | Three drift modes per scope: off / log_only / strict | Score > 0.7 escalates and **never** denies |

**5d — NL→Cedar compiler** (adds Ollama + Qwen2.5-7B)

| Ticket | Build | Verify |
|---|---|---|
| T-028 | Ollama client, constrained generation, timeout and fallback | Works against a local Ollama instance |
| T-029 | Compiler pipeline + 30 curated English→Cedar pairs + auto-generated tests | Measure the success rate and **report it honestly** |
| T-030 | Verify-before-deploy loop: generated Cedar → tests → decision diff → activate | End-to-end flow, which is demo Beat 5 |
| T-031 | 10 pattern-matched fallback templates | Compiler failure or timeout actually triggers the fallback (this is failure drill F-2) |

**5e — T-043 Keycloak OIDC**, last: it gates only the console login flow.
Export the realm to JSON as soon as it works.

**Exit gate:** Cedar evaluates in the hot path · NL→Cedar works with template fallback ·
revocation reaches 3 PEPs in < 2 s · escalation completes end to end without mutating a token ·
drift v0 produces escalations.

---

### Milestone 6 — Console, observability, demo scenarios

**Infrastructure added: OTEL Collector, Prometheus, Tempo, Loki, Grafana.**

| Ticket | Build | Verify |
|---|---|---|
| **T-045** | **D3 identity tree** — animates on mint and revoke, shows role, depth, scopes, budget | **The most important screen in the demo. Over-invest here.** Visual polish is the deliverable, not a bonus |
| T-046 | Live decision stream over SSE, filters, inline display of the failing caveat | Real-time updates hold under load |
| T-047 | Budget and lease dashboard — per-mandate spend gauge, lease utilization, live invariant-checker status | Readable at a glance from across a room |
| T-048 | Audit explorer — search, custody chain narrative, "verify chain" button | Tested against real audit data from e2e runs, not fixtures |
| T-050 | Escalation queue UI — approve/deny with narrowing, approver identity audited | Approval flow works through the console |
| T-027 | Cedar authoring UI: edit → test → diff → activate | Pairs with T-030 to form Beat 5 |
| T-049 | 2 Grafana dashboards: Decisions (rate, outcome, reason codes) and Budgets | Imported and showing live data |
| — | `scripts/seed_demo.py` | Realistic and narratively coherent — BD company names, BDT amounts, sensible roles |
| **T-057** | All 8 beats scripted as individually runnable scenarios; `make demo-reset` | **Each beat tested individually.** `make demo-reset` under 10 s |

**Exit gate:** every screen in `DEMO.md` works with live data · all 8 beats individually
runnable · `make demo-reset` under 10 s · Grafana showing real decisions and budgets.

---

### Milestone 7 — Evidence, submission, rehearsal

| Ticket | Build | Verify |
|---|---|---|
| T-053 | Locust profiles, 100 and 500 RPS | **Record NFR-1 (in-process decision) and NFR-2 (proxy overhead) separately and label them.** Conflating them is the single fastest way to lose a technical judge. Commit to `docs/benchmarks/` |
| T-052 | 5 chaos scenarios: CH-1, CH-3, CH-4, CH-8, CH-10 | Invariant checker running throughout every run; commit the results |
| T-051 | 15–20 red-team tests (A-01…A-09, A-10…A-13, A-17…A-18, A-23…A-26, A-28…A-30) **plus TM-19…TM-22** from `threat-model.md` §6 | Each recorded as mitigated / partially mitigated / accepted risk. Write the rationale for the 3 accepted risks carefully — that honesty is what makes the other 20 believable. TM-19…TM-22 were found by measurement rather than brainstorming, so treat them as additions, not substitutions |
| T-054 | bandit, pip-audit, trivy, gitleaks | Clean, or waived with a documented reason |
| T-019 re-run | Final NFR-1 number | Recorded |
| — | One `mutmut` run on `attenuation.py`, `caveats.py` | Keep the output as evidence; do not iterate |
| T-055 | `scripts/generate_evidence_pack.py` | Generated pack is complete and every claim in it is backed by committed data |
| T-059 | Submission package: architecture doc, TAM, scalability roadmap, IP statement, socio-economic impact | Conforms to BIIN's required format |
| — | 3 judge one-pagers | Narrative checked against the archetypes in `DEMO.md` §3 |
| **T-058** | **7 failure drills** (`DEMO.md` §2, including the solo-specific F-7) | **Human-only. Rehearse every one. Recovery under 30 s each.** |
| — | Screencast fallback | 90-second video, on a phone |
| — | Second machine | Cloned stack, pre-warmed, **already connected and switchable** |

**Exit gate:** every item in `PLAN.md` §19 checked.

---

## Part 4 — Dependency graph

```
M1: T-001 ─┬─ T-002 → T-003 → T-005 ─┐
           ├─ T-004                   │
           └─ T-006                   │
                                      ▼
M2: ──────── T-007 → T-008 → T-009 → T-011 ───────────────────────
                                      │
                                      ▼
M3: ──────── T-012 → T-013 → T-014 → T-016 → T-017 ───────────────
                                      │
                                      ▼
M4: ─── T-018 → T-020 ─┐   T-019 ─┐  T-022 ─┐
                       ├──────────┼┤────────┤
                       ▼          ▼▼        ▼
                     T-021 ──→ T-023  ⭐ E2E CHECKPOINT ─────────
                                      │
                                      ▼
M5: ─ 5a T-024 → T-025 → T-026
      5b T-038 → T-039 → T-040 ; T-037
      5c T-032 → drift v0 → T-036
      5d T-028 → T-029 → T-030 / T-031        (adds Ollama)
      5e T-043                                 (adds Keycloak)
                                      │
                                      ▼
M6: ─ T-045, T-046, T-047, T-048, T-050, T-027, T-049, seed, T-057
                                      │        (adds observability stack)
                                      ▼
M7: ─ T-053, T-052, T-051, T-054, T-055, T-059, T-058
```

---

## Part 5 — What "completeness" means to a BIIN judge

| Criterion | What earns full marks |
|---|---|
| Working demo | 8 beats, all functional, judges interact directly (Beats 4 and 5) |
| Architecture depth | A real lease protocol, real PEP/control-plane separation, real async revocation — not a toy |
| Technical rigor | Evidence, not claims: property tests, benchmark tables, chaos results, the live invariant checker |
| Security | Threat model with mitigations, 15–20 red-team tests, 3 honestly-stated accepted risks |
| IP / compliance | 100% BD development, sole-authored original code, all OSS, self-hosted open-weight models, no black-box API |
| Business viability | TAM, pricing, deployment model, APICTA export story |
| Completeness | Every claimed feature actually works and is backed by a test |
| Honesty | Known limitations stated voluntarily: bearer replay, stranded-lease window, slow-drift evasion, agent-reported-amount trust boundary |

> **Completeness is not feature count.** It means every feature claimed actually works and can
> be proven. Twelve real features with evidence beat twenty half-built ones with slides. The
> evidence pack (§14) *is* the proof of completeness.

---

## Part 6 — Never cut

No matter how the schedule moves:

1. **The hypothesis property tests on attenuation (T-009)** — the answer to "how do you prove it?"
2. **The invariant checker (T-016)** — it runs on screen during Beat 4.
3. **The demo failure drills (T-058)** — rehearsed recovery is what separates this from a
   hackathon build.

All three are cheap. All three are what make the rest credible.
