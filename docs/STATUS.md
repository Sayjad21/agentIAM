# Status

What is built, what remains, and what is worth improving.

**Last updated:** after T-043.
Keep this current at every milestone boundary — a stale status page is worse than none.

---

## 1. At a glance

| | |
|---|---|
| **Milestone** | **M1–M4 complete** · M5 started (T-024, T-025, T-026, T-027 done) · M6 (T-028, T-029, T-030 done) · M7 started (T-032, T-033, T-036 done) · **M8 complete (T-037…T-040 done)** · **M9 resolved (T-043 done; T-041/T-042/T-044 deferred — nothing left undeferred)** · **M10 complete (T-045…T-050 done)** · **M11 started (T-051 done)** |
| **Tickets** | 42 done · 10 remaining · 9 deferred · **61 defined, 52 in scope** |
| **Tests** | 2197 passing (1958 in `make test`; 223 in `make test-integration`; 12 in `make test-e2e`; 4 in `make bench`) |
| **Coverage** | **`agentiam-core` 100% statements** (the rule that is kept). Whole tree 98% — `-sdk` 89%, `-pep` 95–100% by module, `-controlplane` 86%. See §3 gap 14 |
| **CI** | green — five jobs: lint/types/tests + **NFR-1 benchmark**, integration against real Postgres, **the end-to-end slice**, core purity, compose health |
| **Specs** | **10 written — every spec named in `PLAN.md` now exists.** `07-revocation` closes the last gap |
| **ADRs** | 48 |

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
| T-024 | `specs/05-policy.md` + `policy.py` — Cedar engine, 32-case corpus, `NoDecision` fails closed, decimals unscaled (ADR-027, ADR-028). **Pulled ahead of T-023** so enforcement never turns on around a stub | `b653ad4` |
| — | `specs/08-audit-chain.md` + `db/audit.py` + `verify_audit_chain.py` — tamper, deletion, reordering and **head truncation** detected; the append lock proven load-bearing | `753b3f7` |
| — | `demo/tools.py` — four stub upstreams, deterministic ids (a `hash()` bug caught by running it three times) | `f1ed115` |
| **T-023** | **The end-to-end thin slice.** `pipeline.py` wires all ten steps; `/readyz` reports `enforcing: true`. Found **TM-27** — the token's intent binding was enforced nowhere on the live path | `90e4a44` |
| T-025 | `bundles.py` + `policy_cache.py` — Ed25519 signatures over canonical JSON, rollback refused on a monotonic serial, staleness, hot reload (ADR-029) | — |
| T-026 | `activation.py` + 51-case corpus — operator activation gate for policy bundles, rejecting bundles that fail the test corpus (ADR-030) | — |
| T-027 | `agentiam-controlplane` Admin Console UI — Cedar authoring UI with live testing and diffing via Jinja+HTMX + premium styling | — |
| T-028 | `ollama_client.py` — Strictly local, deterministic `httpx`-based Ollama client with schema-constrained generation for the NL compiler | — |
| T-029 | `compiler.py` + auto-generated tests — compiles natural language into Cedar with test cases and evaluated against a 30-case corpus | — |
| T-030 | Verify-before-deploy loop — Integrated compiler into Admin Console UI with ambiguity handling and dual gating (auto-tests + corpus) | — |
| T-032 | `specs/06-drift-detection.md` + `drift.py` — Stateless Intent Binding via headers (`AgentIAM-Task-Intent`), Semantic drift oracle using local Ollama | — |
| T-036 | Drift modes wiring (`off`, `log_only`, `strict`) — Extractor configuration parsing, pipeline context integration, drift score evaluation | — |
| **T-033** | `drift_features.py` + `EmbeddingClient` — f1/f2/f5 and a startup warm-up. **Probing found two defects in T-032**: a 14,244 ms cold embedding against a 2 s timeout (drift was *absent*, not slow, on a cold PEP), and a 724 ms `httpx.Client` built per cache miss on the event loop — 748 ms → 83 ms per miss. f3/f4/f6 deferred (ADR-036, ADR-037) | — |
| **T-037** | `escalation.py` (pure workflow: request/approve/deny, EC-A07…EC-A10) + `escalations` table, `SELECT ... FOR UPDATE` for exactly-once resolution under real concurrency, `/v1/escalations` (open/list/approve/deny), a read-only console queue page, and the PEP wiring that opens one automatically on an `ESCALATE` outcome and puts its id in the response body (spec 09 §11). Root key and approver set are both config-list stopgaps ahead of an issuance service and T-043 (ADR-041) | — |
| **T-038** | `specs/07-revocation.md` (closes the last spec gap) + `revocations` table (`db.revocations`: persist-then-publish, idempotent on `block_id`) + `/v1/revocations` (revoke/pull) + `RedisRevocationSet` — the PEP-side consumer that keeps `decide()`'s synchronous `is_revoked()` fed by a Redis pub/sub fast path and an HTTP pull backstop. **EC-R06 and EC-R07 proven against real Redis**, not mocked: one integration test points the consumer's push connection at a dead port and shows pull alone still converges; another actually stops the Redis container mid-revoke and shows the row still persists. `redis` added as a real dependency for the first time (ADR-042); T-039's Bloom filter and T-040's e2e subtree-propagation measurement build on this | — |
| **T-039** | PEP revocation cache — a Rust-backed counting Bloom filter (`fastbloom-rs`, ADR-044) as the first check inside `RedisRevocationSet.is_revoked()`; a negative returns immediately, a positive falls through to the existing exact set, which stays authoritative. **The obvious pure-Python choice (`pyprobables`) was probed and rejected**: ~92 µs/lookup at 10k ids, ~900x slower than a plain `set`, which would have made the "performance layer" a net loss — `fastbloom-rs` measured ~0.25 µs/lookup at the same sizing. Zero-false-denial property test at 10,000 ids (`test_pep_revocation.py::TestBloomFilterZeroFalseDenials`, includes a reachability audit proving real Bloom collisions occurred rather than passing vacuously). **NFR-4 measured**: 3 real `RedisRevocationSet` instances against real Redis + Postgres, 60 propagation samples per run (`test_revocation_nfr4.py`). Across five runs, p99 ranged **~11 µs to ~16 ms** (one run's slowest sample hit ~12 ms; the rest stayed single-digit-µs) — push (Redis pub/sub, loopback) wins almost every sample, with occasional scheduler jitter, not a full `pull_interval_s` wait. Loopback-only, not a network-separated deployment number, but 100–180,000x inside the 2 s budget across every run | — |
| **T-040** | Subtree revocation e2e (closes M8) — `tests/integration/test_subtree_revocation.py` mints a real 12-agent tree with `attenuate()` (T-011): three independent depth-4 chains under one mandate, not a single branching tree, so a sibling pair shares **no** block id below the root by construction (ADR-045). Revoking the root denies all 12 through the real `decide()` pipeline (`ANCESTOR_REVOKED`); revoking subtree A's own block denies its 4 agents (`TOKEN_REVOKED` for itself, `ANCESTOR_REVOKED` for its descendants) while subtrees B and C — 8 agents — stay `ALLOW` throughout, the explicit negative test `PLAN.md` calls out ("over-revocation is also a bug"). One `RedisRevocationSet` oracle, not three: T-039's NFR-4 test already proved the multi-instance claim: **propagation measured 11–79 µs** for both scenarios, loopback-only (same caveat as T-039's number) | — |
| **T-043** | Keycloak OIDC integration (M9 started) — `auth.py`'s `/auth/login` `/auth/callback` `/auth/logout` against real Keycloak via `authlib`, `principal_id` derived as `kc:<sub>` from the verified ID token, stored only in a signed session cookie (`SessionMiddleware`, new `ControlPlaneSettings.session_secret_key`). `POST /v1/escalations/.../approve` and `.../deny` now require that session unconditionally — ADR-041 point 2's request-body `approver` field is gone, not just superseded (ADR-046). `docker-compose.yml` gains a `keycloak` service; `deploy/keycloak/realm-export.json` pins two demo users' `sub` so `AGENTIAM_CONTROLPLANE_APPROVERS` can name them in advance — Keycloak's Admin REST API cannot pin a user id, only a realm import can (measured). `tests/integration/test_oidc_login.py` drives the real authorization-code flow against a real container, including a wrong-user 403 and a real-signature-verified session | — |
| **T-046** | **Live decision stream** (M10) — `db/decisions.py` + `decisions_api.py` + `console/decisions.html`. `GET /v1/decisions` pages and `GET /v1/decisions/stream` streams the *same* query over SSE, filtered by outcome / agent / scope **in SQL** because the audit chain grows without bound. Reads the chain rather than a side channel, so a `seq` cursor gives replay for free and survives a PEP restart. The ticket's real content is `explain()`: a refusal names the caveat kind, the block that carried it and the caveat's own detail — *"block 2's budget_ceiling caveat refused it: spend_bdt 60000 exceeds 50000"*, never "denied by policy". Three fallbacks guarantee a refusal is never rendered as an empty cell. **Also fixed a hang of my own making:** `request.is_disconnected()` never fires under an in-process ASGI transport, so an unbounded generator hung the whole test session — the same trap that left T-045's SSE test a bare `pass`. `MAX_STREAM_S` now recycles a connection and emits an `event: recycle` frame, which is correct in production too (proxies kill idle streams; `EventSource` reconnects and the cursor resumes) | — |
| **T-047** | **Budget & lease dashboard** (M10) — `db/budget_dashboard.py` + `budgets_api.py` + `console/budgets.html`. The spend gauge draws a pool's `total` as `committed + leased + allocated + available`, which are exactly the terms of the pool invariant (spec 04 §2.1) — so the gauge *is* a picture of the invariant, not a second accounting of it, and a test asserts the four sum to the total. Lease utilization is `settled / granted` over **active** leases, which exposes stranding that the spend gauge cannot see. Top-up rate counts `ACQUIRE`s in a one-minute window from `leases.granted_at`. T-016's sweep runs live as the green/red lamp — `check_invariants` gained a session-taking form (`check_in_session`) so a request handler reuses the connection it already holds instead of opening one per poll. **Polls rather than streams, unlike T-046, on purpose:** this page shows *levels*, where a missed intermediate value costs nothing; T-046 carries *events*, where it does. Money is `Decimal` to the wire — amounts serialise as strings, and a test pins that. Driven in tests through the real `acquire`/`ledger_commit`/`release`, and the lamp is verified **red** on a corrupted ledger as well as green on a healthy one, because an indicator only ever seen green is indistinguishable from a painted-on light | — |
| **T-048** | **Audit explorer + custody view** (M10) — `db/audit_search.py` + `audit_api.py` + `console/audit.html`. Search is a new JSONB-filtered query (`decision_id`, `task_id`, `agent_id`, `principal_id`, `scope`, `outcome`), newest first — deliberately the opposite order from T-046's `read_since`, because a search result is a snapshot a judge reads top to bottom, not a feed a client resumes with a cursor. The custody view and "verify chain" button are **not new logic**: they call T-023's `custody()` and `verify_chain()` directly, already proven against real tampering, deletion, reordering and head truncation — T-048 is exposing that proof live over HTTP, not rebuilding it. Custody entries carry T-046's `explain()` sentence, so a refusal in the historical record names its exact caveat the same way the live feed does. **Extended `DecisionEvent` with a `task_id` field** (`db/decisions.py`, additive, T-046's existing tests untouched) — the live stream never needed it, but a search hit does, to link into its task's custody chain without a second round trip | — |
| **T-049** | **Grafana dashboards + OTEL wiring** (M10, ADR-047) — `docker-compose.observability.yml` (its own file, per `PLAN.md` §4.2) brings up the OTEL Collector, Tempo, Prometheus and Grafana; `deploy/grafana/dashboards/{decisions,budgets}.json` are the two committed dashboards, provisioned automatically. Metrics go straight from each app to Prometheus — `agentiam_controlplane.metrics_api` (new) joins `agentiam_pep.app`'s existing `/metrics` (T-018) — rather than a second OTLP-metrics path through the collector, which is traces-only. **Found and closed a real gap while wiring this**: T-022's `decision_span` had never been called from production code since it shipped — grepped the whole tree — so `current_trace_id()` had never had a real span to read outside its own unit test, and every `DecisionRecord.trace_id` fell back to `str(decision_id)`. `Pipeline.request_span()`/`child_span()` (new) fix that: the span opens in `agentiam_pep.app.proxy()` *before* `authorize()` runs and stays open across the upstream `httpx` call, which is also the literal fix for "decision spans linked to upstream calls." `configure_tracing` (new `tracing.py`) installs a real `opentelemetry-sdk` exporter only when `AGENTIAM_PEP_OTEL_EXPORTER_ENDPOINT` is set — unset in every unit test and benchmark, so `emitter.py`'s measured 5.58 µs "no SDK attached" figure is unaffected. Loki is not stood up — no ticket needs log shipping yet | — |
| **T-050** | **Escalation queue UI** (M10) — most of the backend already existed and was already tested: `agentiam_core.escalation.approve()` (T-037) already enforced grant ⊆ request on both scope and amount (`NarrowingWidensRequest`, EC-A09), `ApproveRequest` already carried `narrowed_scopes` and `max_amount`, and approve/deny already required a real OIDC session (T-043). Verified all three against the running code before writing anything, rather than trusting the prior handoff's claim. The actual gap was `escalations.html` itself: T-037's queue page rendered read-only rows with buttons that posted empty approve/deny bodies, so an approver could only ever grant exactly what was requested. Added a checkbox per requested scope and an amount field capped at the requested amount; `approve()`'s `fetch` body now carries the row's own checked scopes and amount value. A row's Approve button disables client-side on an obvious widening (unchecked scopes, amount above the cap) as a convenience — the server's `NarrowingWidensRequest` is the actual gate. Two new integration tests exercise `max_amount` through the real HTTP router for the first time (only scope-narrowing had an end-to-end test before); a third asserts the console page emits the narrowing controls with the right `value`/`max` | — |
| **T-051** | **Red-team suite** (M11 opens) — `ROADMAP.md` line 288's precise attack list (A-01…A-09, A-10…A-13, A-17…A-18, A-23…A-26, A-28…A-30, 22 attacks) taken as authoritative over `PLAN.md` §12's rounder "15-20" summary line, including A-06 (bearer replay, reported as accepted risk per TM-01) which a stale prior handoff note had wrongly marked out of scope — plus `threat-model.md` §6's two open coverage gaps, TM-19 and TM-20, both now closed. Split across `tests/security/test_redteam_suite.py` (32 unit tests) and `tests/integration/test_redteam_suite.py` (4 tests needing real Postgres/Redis: A-17's 20-sibling swarm, A-18's reserve-then-abandon TTL reclaim, A-29's pull-backstop convergence, A-30's audit-tamper detection), because `tests/security/` has no `conftest.py` reaching `tests/integration/`'s fixtures (ADR-048). Most attacks already had exhaustive coverage elsewhere and are restated here with the specific adversarial framing `PLAN.md` §12 asks for; TM-19 and A-04 (block splicing) had no test anywhere in the tree before this, confirmed by grepping first. TM-19's test hand-builds the wrong existential Datalog encoding against a real `biscuit_auth` chain and proves it actually authorizes an unintended operation before proving the shipped encoding refuses it — the same "probe the wrong design, then prove the guard" shape as TM-24 (T-011). Caught and fixed three of its own tests along the way: `VerifiedToken.scopes` never reflects attenuation-block narrowing, only the authority block's own grant (spec 09 §4) — surfaced by a truncation probe, not by review (ADR-048) | — |

### Next

| Ticket | Delivers | Milestone |
|---|---|---|
| T-052…T-059 | Chaos, load, security scanning, evidence pack, deployment, submission, drills | M7/M11 |

**Carrying forward, unticketed:** the audit-record gap below (gap 15) blocks persisting both
the drift score and the T-033 feature vector, and should be the next thing fixed on the drift
path — spec 06 §5 says so explicitly.

### Deferred — 9 tickets

Each is a genuine roadmap item with a resumption trigger, recorded in `PLAN.md` §21. Stating
them explicitly is part of the submission's honesty story, not an omission to hide.

| Ticket | Why | Resumption trigger |
|---|---|---|
| T-010 | Token reference for oversized chains. **Measured unreachable**: at `max_depth = 8` a token is 4,940 base64 chars — 60% of the 8 KB limit (ADR-006) | `max_depth` above ~16 |
| T-031 | Template fallback for NL compiler. The compiler path demonstrates without it — but **F-2 has no implementation while it is deferred**, so beat 5 hangs if Ollama is down. Cost stated in `PLAN.md` §T-031 | Preparing the F-2 drill for real (T-058) |
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
| ~~5~~ | ~~**Specs 05–09 unwritten**~~ — closed. `07-revocation` (the last one) written and read back before T-038 started, per the spec-first rule | Its ticket cannot start spec-first, which is the rule that caught seven design errors | Closed |
| 6 | **A1 re-verification is manual.** The security rests on biscuit scoping block facts; nothing fails if a library upgrade changes it | Silent collapse of INV-1 on a dependency bump | Should become a test — see §4 |
| 7 | **`budgets.mandate_id` carries no foreign key.** No `mandates` SQL table exists yet — T-005 built `Mandate` as a pure Pydantic model, no persistence (ADR-014) | A budget row can reference a mandate id that was never issued; nothing in the schema catches it | Whichever ticket first persists mandates (issuance service, `PLAN.md` §8 — not yet its own ticket) |
| 8 | **`ACQUIRE` does not clamp by `max_fraction`.** Spec 04 §4.1's clamp is mathematically incompatible with T-013's own acceptance test, applied to a fixed caller-`requested` amount (ADR-015, measured) | No single-PEP-crash blast-radius bound beyond `ttl` — a PEP can be granted more than a quarter of the pool in one `ACQUIRE` | T-015 (adaptive lease sizing, deferred) — that's where the formula actually applies |
| ~~9~~ | ~~**CI ran none of the 45 integration tests.**~~ Closed while integrating M3: `make test` and the CI workflow both excluded the `integration` marker, so every ledger race test ran only by hand | Three tickets of ledger correctness — `FOR UPDATE` serialization, the 50-concurrent-acquire bound, the ADR-017 dedup race — were gated by nothing and would have rotted silently | Closed: `integration` CI job + `make test-integration` |
| ~~13~~ | ~~**`test_inv1_attenuation_never_widens` is intermittently flaky**~~ — closed. **Two** unrelated flakes, and the recorded cause was wrong twice. INV-1 is *not* violated: zero violations across ~15,000 contexts in two brute-force campaigns. The real cause is `biscuit-python`'s authorizer defaulting to `max_time = 1 ms` of **wall clock** — measured at 2 of 42,014 authorize calls under suite load, both inside `verify()`. The harness read that as a denial, so the parent "denied" what the child allowed. Fault injection proved it: 10 of 10 timeouts injected on a parent check reproduce the false `child authorized what the parent did not`, 9 of them with `FlakyStrategyDefinition` on top. The second flake was `test_strategies.py::test_zero_ceilings_occur`, a shape audit sampling the nine-kind union — 3 misses in 60 campaigns | The printed counterexample was **spurious**, and INV-1 stands. But this was a *product* bug on the PEP hot path, not only a test bug: a loaded PEP would have denied legitimate requests for want of scheduling, and 1 ms is the whole of NFR-1's decision budget | Closed: ADR-021 (explicit Datalog limits, harness re-raises, INV-1 draws its scenario as one composite value), ADR-022 (`caveats_of_kind`), TM-25 |
| ~~11~~ | ~~**The PEP enforces nothing.**~~ — **closed in T-023.** `/readyz` reports `enforcing: true`, and the flag is derived from the wiring rather than declared, so an app built without a pipeline still reports `false` | Was the single most misleading state in the repo | Closed. The estimate moved four times (T-019 → T-020 → T-021 → T-023); three were guesses written before reading `decide()`'s signature, and the fourth was the decision to build T-024 first (ADR-027) |
| 12 | **No HTTP trailer support, and none available.** Measured across httpx, Starlette and uvicorn: no layer exposes trailers (ADR-020) | One T-018 acceptance criterion consciously unmet. Costs nothing for the demo; trailers are rare on HTTP/1.1 | Only reopens if T-041 (MCP, deferred) needs them — and the fix would be in the ASGI server, not here |
| 10 | **`tests/integration/test_budget_schema.py` duplicates `conftest.py`'s fixtures.** T-012 predates the shared conftest; noted in its header and left alone | Two copies of the container and migration fixtures drift apart | Same cleanup as §4.3 |
| 14 | **Coverage is reported but never gated.** No `fail_under` in `pyproject.toml`, and CI's `quality` job uploads `coverage.xml` without asserting on it. This page claimed 100% across four packages while the tree measured 98% | The "core stays at 100%" rule is discipline, not a check. It has already slipped once — `agentiam-core` dropped to 96% during T-033 and was caught by hand, not by CI | One line in `[tool.coverage.report]`, plus a per-package floor for `agentiam-core` |
| ~~15~~ | ~~**`DecisionRecord.drift_score` is never written.**~~ — **closed.** `decide()` populated `Decision.drift_score` and `pipeline._record()` never read it, so the field was `None` on every record, including on a `DRIFT_ESCALATION` denial where the score is the whole justification. The pipeline now records it, and `DecisionRecord.drift_features` carries T-033's f1/f2/f5 vector alongside | `log_only` was paying two embedding round-trips for nothing observable — spec 06 §3's stated purpose unimplemented. Also unblocked persisting the feature vector, which spec 06 §5 named as the blocker | Closed. Absent features are omitted rather than stored as null, so a deferred dataset can still tell *not measured* from *measured as zero* |
| ~~16~~ | ~~**`POST /policy/activate` gates nothing.**~~ — **closed.** It assigned `store.current_source` and returned 200 with no corpus, no auto-tests and no parse; `can_activate` was computed for the template, so the gate was UI-only and a direct POST installed unparseable Cedar. The endpoint now parses, runs the full 51-case corpus, and refuses with **409** naming the failing cases (ADR-039) | Was a direct contradiction of ADR-034, T-030's acceptance criterion and `PLAN.md` §907. The test that existed asserted the *ungated* behaviour as correct | Closed. Signature and serial gates still await real bundle signing — the console store is a stub |
| ~~17~~ | ~~**`scripts/evaluate_compiler.py` measures nothing.**~~ — **harness closed, and the result is bad.** It passed bare names (`"admin"`) where Cedar needs entity uids (`User::"admin"`), so every request returned `NoDecision`: 0/30 regardless of compiler quality. Fixed and verified against a control policy (0/30 → 1/30, correct, since only case 1 concerns admins) | The harness now discriminates. **See gap 19 for what it then measured** | Closed |
| ~~18~~ | ~~**The NL compiler's model is not installed.**~~ — closed; `qwen2.5:7b-instruct-q4_0` is present and resident on GPU (5.32 GB VRAM). Meeting a real generation immediately exposed the 30 s timeout as below the *warm median* — ADR-038 | Every first call and most warm calls timed out, reported indistinguishably from Ollama being down | Closed: 300 s timeout, `keep_alive`, `warm()` |
| ~~19a~~ | **CLOSED — 27/30 (90%) on a clean run, all 30 attempted, zero throttling.** `passed 27 · wrong 1 · unparseable 2 · asked for clarification 0 · errors 0`; latency median 2.6 s. Backend `gemini-flash-lite-latest`, which resolved to **`gemini-3.5-flash-lite`** — logged per call, because the alias is otherwise unattributable. Journey: **0% → 43% (local, inflated) → 90%**, from rebuilding the dataset, removing three leaked cases from the prompt, and teaching the three Cedar mistakes that were actually measured | Beat 5 is demonstrable. The compiler is now measurable *and* good, and `--validate` keeps dataset iteration free of model quota | Remaining: 1 wrong policy and 2 unparseable, out of 30. Worth one more prompt pass, but no longer blocking |
| ~~19a-partial~~ | ~~**Measured on Gemini 2.5 Flash, leak-free prompt: 17/30 (57%) — or 17 of the 19 that got an answer (89%).**~~ — superseded by the clean run above; that one was throttled at 11 of 30. Both numbers are stated because either alone misleads. Full breakdown: **passed 17 · wrong decision 2 · unparseable Cedar 0 · asked for clarification 0 · errors/timeouts 11**; latency median 7.6 s. The 11 errors are rate-limit exhaustion, not compiler failures — Gemini's free tier throttled harder than the 15 s pacing assumed | **The two failure classes that dominated are gone.** Unparseable Cedar 8 → 0 (teaching `&&`/`||` and that only `context.amount` is a decimal); clarification refusals 23 → 0. What remains is 2 genuinely wrong policies out of 19 answered. Beat 5 is demonstrable | Lower `PACING_S` for Gemini, or use a paid tier, then re-measure for a clean 30/30-attempted number. Prompt work should target the 2 wrong cases, not the throttle |
| ~~19~~ | **Rebuilt, and the number moved 0% → 43% on the same local model.** Dataset v2 evaluates against AgentIAM's own entity model through the shared `evaluate_case`, so a case is won by generalising (`principal.role == "senior"`) rather than by guessing an id, and it is **self-validating**: `evaluate_compiler.py --validate` scores the reference policies with no model at all and must hit 30/30 before any run is worth starting. The prompt now teaches the fixed schema and the exact Cedar mistakes that were measured. Result on `qwen2.5:7b-instruct-q4_0`: **passed 13 (43%) · wrong 7 · unparseable 8 · asked for clarification 0 · errors 2**; latency median 4.4 s. **Refusals went 23 → 0.** This is the documented *local* baseline for the ADR-040 migration | Beat 5 is demonstrable on the local model for the first time, though 43% is not good enough to put in front of a judge unrehearsed. Remaining failures are Cedar-syntax slips, several of them a missing trailing `;` on an otherwise-correct policy | Hosted inference is now the prototype default (ADR-040). Re-measure on Groq; the gap between the two numbers is what the migration back to local will have to close |
| ~~19-original~~ | ~~**The NL→Cedar compiler scores 0/30 — but the dataset cannot be passed.**~~ — kept for the record, because the diagnosis is the useful part. First real measurement, now the harness works: **passed 0 · wrong decision 4 · unparseable Cedar 2 · asked for clarification 23 · timeout 1**; latency median 25.9 s, max 300.6 s. Three independent defects, established by probe: **(a)** the ambiguity instruction over-fires, refusing 77% of the corpus on prompts as plain as *"Managers can approve expenses"*; **(b)** the model's Cedar is wrong in a small fixed way — `Resource::*` for an unconstrained resource, `resource in Expense` where Cedar wants `resource is Expense`, conditions in the scope instead of a `when` clause (a narrowed prompt turned all 6 sampled refusals into compile attempts, but only 1 of 6 parsed); **(c)** — the root cause — **the dataset is not a valid instrument** | **Fixing the compiler cannot fix this.** Of 30 cases the positive principal id appears verbatim in the English in only **10**; **13** need an arbitrary suffix guessed (*"Admins"*→`admin`, *"HR"*→`hr_rep`) and **7** name an id the prompt never mentions (`alice`, `manager1`, `guest`). So ≤10/30 is the ceiling for *any* compiler — and reaching it requires emitting `principal == User::"manager1"`, a policy about one named person, which is the wrong generalisation. The right role-based policy cannot work either: the harness passes `entities=[]`, so `principal.role` and ownership do not exist. The 25.9 s median is also flattered by the refusals, since a refusal is a short generation — real compilations took 53–242 s against beat 5's 90 s budget | **Rebuild the instrument before touching the prompt.** Ids derivable from the prompt, entities supplied so role-based policies are expressible, and expectations that reward generalisation rather than id-guessing. Then re-measure, then decide on T-031. Prompt work against the current dataset optimises toward a target that is both unreachable and wrong |

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
