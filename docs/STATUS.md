# Status

What is built, what is left, and what is known to be wrong — current state only. The history
lives in [`JOURNAL.md`](JOURNAL.md): every ticket's narrative, the fix queue from the manual
passes (former `TODO.md`, items 1–34), and every closed gap with its original finding.

**Last updated:** 2026-10-16. Keep this current at every ticket — a stale status page is worse
than none.

---

## 1. At a glance

| | |
|---|---|
| **Tickets** | **49 of 53 in scope done** · T-057 partly done · T-058, T-059, T-061 (reduced) left · 8 deferred · 61 defined (`PLAN.md` §9) |
| **Milestones** | Everything through hardening (T-051…T-056) is complete. Demo and submission (T-057…T-061) is in progress |
| **Tests** | 3,257 collected. `make check` runs 2,906, all passing; the other 351 are `integration`, `e2e`, `chaos` and `perf`, each run by its own target and CI job |
| **Coverage** | `agentiam-core` 100% of statements, held since T-005. Whole tree ~98% when last measured (T-051). Reported, not gated — gap 14 |
| **CI** | Nine jobs on push/PR: lint, types and tests with the NFR-1 benchmark · integration against real Postgres · end-to-end slice · core purity · evidence pack, `performance.md` and `chaos-results.md` drift checks · compose health · full demo stack (NFR-8) · security scan (bandit, pip-audit, trivy, gitleaks, SBOM, log secret scanner) · nightly chaos. `release.yml` is tag-triggered and has never run against a real tag |
| **Specs** | 10 (`specs/01`…`10`), each written before its code |
| **ADRs** | 78 (ADR-001…ADR-078) |

---

## 2. Tickets

### Done

| Area | Done | Deferred |
|---|---|---|
| Specs, models, tokens, attenuation, SDK (T-001…T-011) | T-001…T-009, T-011 | T-010 |
| Ledger, leases, invariants, sibling budgets (T-012…T-017) | T-012…T-014, T-016, T-017 | T-015 |
| PEP, decision pipeline, extractor, pool, emitter, Cedar, thin slice, bundles (T-018…T-025) | all | — |
| Activation gate, Cedar UI, NL compiler (T-026…T-031) | all (T-031 in APICTA-01 Part D) | — |
| Drift (T-032…T-036) | T-032, T-033, T-036 | T-034, T-035 |
| Escalation, revocation, Bloom cache, subtree revocation (T-037…T-040) | all | — |
| MCP, Keycloak, SPIRE (T-041…T-044) | T-043 | T-041, T-042, T-044 |
| Console screens, Grafana/OTEL, escalation UI (T-045…T-050) | all | — |
| Red team, chaos, load, security scan, evidence pack, deployment (T-051…T-056) | all | — |
| Demo and submission (T-057…T-061) | — | T-060 |

What each ticket delivered, and what it found, is in `JOURNAL.md`. Work done outside a
ticket number — the settlement path (ADR-049), the scheduled reaper (gap 27), the Datalog
reader (gap 2), the tool catalogue and per-path roles (ADR-069), lease renewal on a timer
(ADR-070) — is there too, under the fix queue.

### Remaining

**The current sprint is [`APICTA_TICKETS.md`](APICTA_TICKETS.md)** — eight tickets, in order,
that wire up the rows of §3 before APICTA. Its status table is the live record of that work;
this page is updated as each ticket closes a gap.

| Ticket | State |
|---|---|
| **T-057** Demo scenario scripting | **Partly done.** `make demo-seed` (paced 138-call scenario over a 14-agent tree, one refusal per enforcement layer), `make demo-conc` (three-PEP sibling race), `make demo-tamper` / `demo-untamper`, `make demo-reset`; `make demo-seed` ends on the escalation the agent completes itself (APICTA-03) and `make demo-agent` runs a live LLM agent into a prompt injection (APICTA-05). Not met: `PLAN.md`'s "all 8 beats scripted, each individually runnable" — beats 1, 4 and 6 cannot be shown as written today (`MASTER.md` §7.8) |
| **T-058** Failure drills | Not started. `DEMO.md` §2 lists F-1…F-7. F-1/F-2 now have a real recovery: the offline template compiler (T-031) |
| **T-059** Submission package | Not started. `PLAN.md` §19 |
| **T-061** OSS release (reduced) | Public repo, README, demo video. Whether the repo goes public is a team decision |

### Deferred — 8 tickets

Each has a resumption trigger, recorded in `PLAN.md` §21.

| Ticket | Why | Resumption trigger |
|---|---|---|
| T-010 | Token reference for oversized chains. **Measured unreachable**: at `max_depth = 8` a token is 4,892 base64 chars — 60% of the 8 KB limit (ADR-006) | `max_depth` above ~16 |
| T-015 | Adaptive lease sizing. Fixed leases behave identically for the demo; the algorithm is specified in spec 04 §12. Also where gap 8's `max_fraction` clamp belongs | Production traffic with variable rate |
| T-034 | Drift dataset — 2,000+ labelled pairs, weeks of human labelling | Research phase |
| T-035 | Calibrated drift classifier | After T-034 |
| T-041 | MCP streamable-HTTP gateway. The HTTP PEP proves the pipeline; MCP is the adoption path | Adoption push |
| T-042 | MCP scope extractor | With T-041 |
| T-044 | SPIRE workload identity. The original rationale — "mTLS gives the same outcome" — does not hold: there is no TLS between any services (§3) | Enterprise SPIFFE requirement, after TLS exists |
| T-060 | Paper draft. BIIN scores the demo and report. The research track is planned in `research/` | Post-BIIN |

**Built in reduced scope:** 8 of 9 caveat kinds (`RateLimit` dropped) · Cedar only, no OPA ·
2 of 4 Grafana dashboards · 22 of 33 red-team attacks · 5 of 12 chaos scenarios · 100 and 500
RPS load profiles, and 500 could not be offered on one machine · drift features f1, f2, f5 of
six · no `mutmut` run.

---

## 3. Presented as working, but not wired

Found by an audit of the running system on 2026-10-16. Each row is something the console or
the docs present as working that is not connected to anything. Either wire it or reword the
claim; `MASTER.md` §8 has the talk-track version.

| What the console or docs say | What is actually true | Gap |
|---|---|---|
| A judge sets a budget ceiling for a new task | Every task reuses one hard-coded `DEMO_MANDATE_ID`, because a PEP serves exactly one mandate | 25 |
| "One URL change to install" (MCP gateway) | No MCP code anywhere; T-041/T-042 deferred | — |
| Drift detection | Rule-based v0, three of six features, opt-in Ollama profile, NFR-9 never measured. Every demo route has drift `off` | — |
| "mTLS in place; SPIFFE deferred" (`PLAN.md` T-044) | No TLS between any services | — |
| "Periodic checkpoints signed by the audit service key" (`PLAN.md` §6.8) | No checkpoints, no signatures | — |
| "Full OpenAPI is generated and committed to `docs/openapi/`" (`PLAN.md` §8) | The directory does not exist | — |
| "Model-checked" (spec 04 header) | 400 random interleavings in a scratchpad script that was never committed | — |
| Roles are attested | A static JSON map read at boot | 28 |

`research/gap-analysis.md` and `research/threat-model.md` §6 list the rest of the
claim-versus-code mismatches.

---

## 4. Known gaps

Open gaps only, most consequential first. Closed gaps keep their numbers and their original
findings in `JOURNAL.md`'s appendix; a number is never reused.

| # | Gap | Impact if left | Where it lands |
|---|---|---|---|
| 25 | **The PEP is scoped to exactly one mandate.** `LeasePool` binds a `mandate_id` at construction and `Pipeline.reserve()` reserves without one — measured against the real constructors while building T-056's production composition root, not assumed. Verification, revocation, policy, drift and audit are all mandate-agnostic; only the lease pool is bound | A deployed PEP enforces budget for one task at a time. Fine for the demo (depth 3-4, one mandate per beat); a real multi-tenant deployment needs either one PEP process per mandate or a multi-mandate pool | Changing it touches `LeasePool` and `Pipeline` on the money hot path, and ADR-049's settle-before-release invariant — a ticket of its own, not a T-056 side effect (ADR-056) |
| 28 | **An agent's Cedar role is static deployment configuration.** ADR-069 gave the PEP a per-agent role source — `AGENTIAM_PEP_ROLE_ASSIGNMENTS_PATH`, a JSON map of delegation path to role — which is what made the shipped bundle's `principal.role != "senior"` forbid able to discriminate at all; before it, one process-wide `default_role` made every role-conditioned policy always-on or always-off. It is organization-asserted (ADR-057 unchanged: never the parent's claim in the token) and keyed on the *path* rather than the `agent_id`, since the id is written by the delegating parent | Roles do not rotate, expire or attest, and an agent the organization has not written down gets `default_role`. Adding or re-roling an agent needs a file edit and a PEP restart. One residual is refused at boot rather than at request time: an ancestor assigned *less* than its own descendant could mint that descendant, so `_refuse_widening_roles` rejects the shape | The issuance service (gap 7, gap 26, ADR-039) is the real answer, and the path-keyed map is what is available without it |
| 7 | **`budgets.mandate_id` carries no foreign key.** No `mandates` SQL table exists yet — T-005 built `Mandate` as a pure Pydantic model, no persistence (ADR-014) | A budget row can reference a mandate id that was never issued; nothing in the schema catches it | Whichever ticket first persists mandates (issuance service, `PLAN.md` §8 — not yet its own ticket) |
| 21 | **A partitioned PEP cannot shut down, and a timeout does not bound it.** `LeasePool.aclose()` drains in-flight top-ups and then `RELEASE`s, both of which need the ledger. Worse, `asyncio.wait_for` around it does not help: the cancellation lands inside SQLAlchemy's greenlet bridge while asyncpg is blocked on the dead socket, and the driver's own rollback/close needs that socket — measured stuck for 5 minutes against a 5 s bound | Availability on restart, not correctness: the stranded lease is bounded by TTL + S (CH-3) and the control plane's reaper (every TTL/4 since gap 27 closed) reclaims it. An orchestrator's `SIGTERM` grace period also elapses and the PEP is killed anyway, turning a graceful shutdown into CH-3 | Pinned by CH-4's `test_a_partitioned_pep_cannot_shut_down_gracefully`. A fix needs a release path that can abandon its connection |
| 1 | **TM-22's PEP-side half is open.** TM-19 and TM-20 closed in T-051, TM-21 in T-014, TM-22's reaper side in T-013. Nothing refuses a lease to a PEP whose clock is skewed beyond `S` (`threat-model.md` §5.6), and spec 04's open question 1 is still open | Overspend within the excess-skew window | CH-7, not yet built; `research/lease-plan.md` GAP-C |
| 6 | **A1 re-verification is manual.** The security rests on biscuit scoping block facts; nothing fails if a library upgrade changes it | Silent collapse of INV-1 on a dependency bump | Partly covered: `tests/security/test_datalog_labels.py` shows an injected `admin(true)` block fact is invisible to the authorizer. Nothing injects `operation`, `scope`, `requested` or `current_depth` — see §6.1 |
| 8 | **`ACQUIRE` does not clamp by `max_fraction`.** Spec 04 §4.1's clamp is mathematically incompatible with T-013's own acceptance test, applied to a fixed caller-`requested` amount (ADR-015, measured) | No single-PEP-crash blast-radius bound beyond `ttl` — a PEP can be granted more than a quarter of the pool in one `ACQUIRE` | T-015 (adaptive lease sizing, deferred) — that's where the formula actually applies |
| 14 | **Coverage is reported but never gated.** No `fail_under` in `pyproject.toml`, and CI's `quality` job uploads `coverage.xml` without asserting on it. This page claimed 100% across four packages while the tree measured 98% | The "core stays at 100%" rule is discipline, not a check. It has already slipped once — `agentiam-core` dropped to 96% during T-033 and was caught by hand, not by CI | One line in `[tool.coverage.report]`, plus a per-package floor for `agentiam-core` |
| 4 | **`mutmut` not yet run.** T-009 asks for ≤10% surviving mutants on `attenuation.py` and `caveats.py` | Coverage says the lines run; mutation says the assertions bite. Untested claim | One run; `evidence-pack.html` states it has not happened |
| 10 | **`tests/integration/test_budget_schema.py` duplicates `conftest.py`'s fixtures.** T-012 predates the shared conftest; noted in its header and left alone | Two copies of the container and migration fixtures drift apart | Same cleanup as §4.3 |
| 12 | **No HTTP trailer support, and none available.** Measured across httpx, Starlette and uvicorn: no layer exposes trailers (ADR-020) | One T-018 acceptance criterion consciously unmet. Costs nothing for the demo; trailers are rare on HTTP/1.1 | Only reopens if T-041 (MCP, deferred) needs them — and the fix would be in the ASGI server, not here |
| 35 | **The deployed PEP's recorded decision latency is ~1–2 ms, not the 0.43 ms in-process figure.** `latency_us` spans `authorize()` steps 1–7. Measured on the demo stack (APICTA-05): warm, a scope refusal records 0.8–2.0 ms and an allowed read 1.1–2.6 ms; the first request after idle, 11.7 ms. T-053's 145 µs `decide()` + 209 µs `verify()` is the in-process benchmark and does not include extraction, chain-identity parsing, principal resolution or a containerized host. `DEMO.md` beat 3 said *"Denied in 0.4 ms"* with the latency panel visible, which shows the recorded number | A presenter quoting 0.4 ms beside a panel reading 1.7 ms | Profile where the deployed millisecond goes — APICTA-08, which re-measures the hot path anyway |
| 36 | **The console names the wrong block for a failing caveat.** `CaveatRef.block_index` is the caveat's position in the flattened list `token_caveats()` returns, and `explain()` prints it as *"block N"*. Measured live: the doc reader's scope refusal reads *"block 0's scope_subset caveat"* — block 0 is the authority block, and the caveat sits in block 1 — and a depth-2 settlement agent's ceiling reads *"block 3"* on a chain with blocks 0–2 | The drawer points an operator at the wrong delegation step. The reason code and caveat kind are right | Carry the block index through `token_caveats()` (one block per caveat group), or print the caveat kind without a block number |

---

## 5. Open follow-ups from the manual passes

The full entries are in `JOURNAL.md` under "the fix queue", with their original numbers.

| Item | What | Done when |
|---|---|---|
| 21 | **Re-measure NFR-2.** The harness now matches the deployed PEP (ADR-062), but the committed figures predate that and understate a depth-3 request by ~0.45 ms. A 5 s smoke run at 100 RPS gave enforcement p99 6.8 ms — one run, not evidence | `pb2-breakdown.json` and `nfr2-load.json` re-measured together in one sitting on one host, and `performance.md`'s "predate a change" notes come out |
| 23 | **One unexplained integration-job failure** at `ec616d1`, not reproduced in four local runs. `pytest-randomly` is not installed, so `-p no:randomly` does nothing | If it recurs, read the failing test's name from the Actions log first |
| 30 | **No demo beat shows role discrimination.** Both role-conditioned forbids are enforced, but every agent that can reach `payment_api` holds a senior role | A seeded agent with `payment:initiate` and no senior role is refused with `POLICY_DENIED`, and `DEMO.md` points at it |
| 33, 34 | Operational, no fix intended: demo tokens expire 8 h after seeding; re-seeding a used volume leaves older generations in the identity tree | Documented in `RUNNING.md` |

---

## 6. Improvements worth making

Not in any ticket. Ordered by value per hour.

### 6.1 Pin assumption A1 with a test — **high value, ~30 minutes**

The design rests on biscuit scoping block facts so a later block cannot widen authority
(`threat-model.md` §4). `tests/security/test_datalog_labels.py` shows an injected
`admin(true)` fact is invisible to the authorizer; nothing does the same for the facts that
matter. A test that appends a block injecting `operation`, `scope`, `requested` and
`current_depth` facts and asserts it authorizes nothing it was narrowed out of turns *"we
checked once"* into *"CI checks every push"*. It is also the guard against a `biscuit-python`
upgrade silently collapsing INV-1 (gap 6).

### 6.2 Gate coverage — **high value, ~15 minutes**

One `fail_under` line in `[tool.coverage.report]` plus a 100% floor for `agentiam-core`
(gap 14). It slipped once during T-033 and was caught by hand.

### 6.3 Property-test the `to_datalog` / `evaluate` agreement — **high value, ~1 hour**

The conformance suite covers 60 hand-written cases. The agreement is a security property
(ADR-008), and T-009's generators already exist. A hypothesis property searches the space
rather than the cases someone thought of — the move that found ADR-011.

### 6.4 Finish the test-fixture module — **medium value, ~40 minutes**

`tests/fixtures/tokens.py` holds `a_mandate()`, `a_root_client()` and a frozen clock; four
older modules still carry near-duplicate copies. The integration side has the same problem
(gap 10). Adding the deterministic keypair and golden depth-4 chain `PLAN.md` §10.4 asks for
would make integration tests cheaper to write and comparable.

### 6.5 Make the milestone review a script — **medium value, ~1 hour**

`ENGINEERING-RULES.md` §4 and §5 define a self-review checklist and a spec-drift check. Several
items are mechanical — TODOs without ticket ids, deny paths not mapped to a reason code, floats
near money, I/O in core. A `scripts/review.py` would make them reliably run rather than
reliably intended.

### 6.6 Record the `biscuit-python` version in the evidence pack — **low value, ~15 minutes**

Every measured claim in specs 01–03 is "against `biscuit-python` 1.x". Surfacing the exact
locked version next to the measurements makes them reproducible by a reviewer.

### 6.7 CI on Windows — **low value, ~30 minutes**

Two problems so far were platform-specific (mojibake, the missing `make`), and the SBOM is
OS-dependent. A Windows job would catch path, encoding and line-ending issues at push time.

---

## 7. Risks currently live

From `PLAN.md` §17, with the current reading.

| Risk | State |
|---|---|
| **R-2** Python latency undermines the story | **In-process: retired.** `decide()` is 430 µs p99 with real Cedar and real Datalog, inside NFR-1's 1 ms. **Under load: open.** NFR-2 is not established (§5 item 21; needs the load generator on a second machine) |
| **R-3** Lease protocol bug found late | **Reduced.** Protocol modelling in T-004 found one (ADR-009), guard-proof testing in T-014 a second (ADR-017), and the chaos suite found the double spend (gap 23, closed). Not formally model-checked (§3) |
| **R-8** Single developer, no second reviewer | Live and structural. Mitigated by specs-first and property tests |
| **R-6** Scope creep | Holding. Every deviation is an ADR with a stated cost |
| **R-1** Others ship equivalent agent IAM | Tracked in `research/related-work.md` §4: attenuation and single-node budgets have prior work; the distributed lease half is still unclaimed |
