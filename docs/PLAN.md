# AgentIAM — Master Implementation & Testing Plan

**Project:** AgentIAM — Identity, Authorization & Spend Control Plane for AI Agents
**Target:** Bangladesh ICT & Innovation Awards (BIIN), Tertiary Student Project (HC-TSP) → APICTA
**Cross-nominations:** CT-AI, CC-RD
**Stack decision (locked):** Python everywhere
**Timeline in this doc:** 12 months (M1–M12). Compressible — see §16 for the compression map.
**Execution:** single developer. Actual milestone sequencing and exit gates live in `docs/ROADMAP.md`; engineering standards in `docs/ENGINEERING-RULES.md`.
**Document status:** authoritative on *what* to build. If code and this document disagree, update this document in the same commit.

---

## Table of Contents

0. [Engineering workflow](#0)
1. [Scope lock — what we are and are not building](#1)
2. [Glossary](#2)
3. [System architecture](#3)
4. [Technology stack](#4)
5. [Repository layout](#5)
6. [Core specifications](#6)
   - 6.1 Mandate & token format
   - 6.2 Caveat language
   - 6.3 Attenuation semantics + invariants
   - 6.4 Budget lease protocol
   - 6.5 Policy layer
   - 6.6 Intent binding & drift detection
   - 6.7 Revocation
   - 6.8 Audit ledger
   - 6.9 Decision record
7. [Data model](#7)
8. [API contracts](#8)
9. [Phase plan M1–M12 with tickets](#9)
10. [Testing strategy](#10)
11. [Edge case catalogue](#11)
12. [Adversarial / red-team test suite](#12)
13. [Performance & chaos engineering](#13)
14. [Evidence pack for judges](#14)
15. [Demo runbook](#15)
16. [Timeline compression map](#16)
17. [Risk register](#17)
18. [Research & IP plan](#18)
19. [Submission checklist](#19)
20. [Appendix: ticket format](#20)
21. [Future work (deferred from BIIN submission)](#21)

---

<a name="0"></a>
## 0. Engineering workflow

This document is the root context for the project and the single source of truth. Work proceeds
ticket by ticket against §9, sequenced by `docs/ROADMAP.md`. Day-to-day standards — the
non-negotiable rules, the Definition of Done, the periodic self-review — live in
`docs/ENGINEERING-RULES.md`; this section states the shape of the work.

### 0.1 Bootstrap sequence

1. `git init`
2. This file lives at `docs/PLAN.md`.
3. Create `docs/ENGINEERING-RULES.md` — the standing project standards.
4. Create `docs/DECISIONS.md` (append-only ADR log).
5. Start at ticket `T-001` and nothing beyond it. Resist scaffolding the whole project up
   front; unbuilt structure encodes assumptions that have not been tested yet.

### 0.2 Rules of engagement

**One ticket at a time.** Each ticket in §9 has an ID, a dependency list, deliverables, and
acceptance criteria. Finish one before opening the next. Work that spans several half-finished
tickets accumulates wrong assumptions that surface at integration time.

**Specs before code, always.** For every ticket touching a contract (token format, API,
protocol), write or update the spec section in `docs/specs/` first, read it back critically,
and only then write code. Bugs in specs are cheap; bugs in code that encoded a wrong spec are
expensive.

**Tests are part of the deliverable, not a follow-up ticket.** A ticket is not done until its
acceptance criteria are covered by automated tests that failed before the change and pass after.

**The caveat language, the token format, and the lease protocol are fixed.** They are specified
in §6. A "simpler" variant is not a shortcut — those three are the research contribution and
the differentiator.

**Fixed loop per ticket:**
```
read PLAN.md section(s) named in the ticket
  → restate the acceptance criteria in your own words
  → write/extend tests (they must fail)
  → implement
  → run: ruff check . && mypy --strict . && pytest
  → update docs/ if a contract changed
  → append to docs/DECISIONS.md if a non-obvious choice was made
  → commit
```

**Never:** add a dependency outside §4 without an ADR; introduce a paid service; weaken a test
to make it pass; add `# type: ignore` without a comment explaining why; catch-and-swallow
exceptions in the hot path.

**Definition of Done (every ticket):**
- [ ] Acceptance criteria all covered by tests
- [ ] `ruff check .` clean
- [ ] `mypy --strict` clean on changed packages
- [ ] `pytest` green, coverage on changed files ≥ 85%
- [ ] Docstrings on all public functions
- [ ] `docs/PLAN.md` or relevant spec updated if a contract changed
- [ ] No `TODO` without a ticket ID next to it

### 0.3 Cadence

Aim for 2–4 tickets per week. If a ticket takes more than ~3 sittings, it is too big — split it
and record the split in `docs/DECISIONS.md`. Run the §4 self-review in
`docs/ENGINEERING-RULES.md` every five tickets, and the spec-drift check at every milestone
boundary.

---

<a name="1"></a>
## 1. Scope lock — what we are and are not building

### 1.1 One-sentence definition

> AgentIAM gives every AI agent and sub-agent its own cryptographic identity carrying a strictly narrower set of permissions and a bounded spend budget than its parent, enforced at the tool boundary in under a millisecond, with a complete chain of custody for every action taken.

### 1.2 Positioning (locked)

Primary framing for the pitch: **"the credit limit and chain of custody for AI agents."**
Adoption shape: **MCP gateway** — one URL change to install.
Research/IP artifact: **attenuable delegation + quantitative mandates** protocol spec.

### 1.3 In scope

| # | Capability | Why it's in |
|---|---|---|
| C1 | Per-agent and per-sub-agent cryptographic identity | Core primitive |
| C2 | Holder-side offline attenuation (parent mints narrower child token) | The differentiator vs OAuth |
| C3 | Quantitative mandates: spend, call count, data volume, wall-clock | Novel; nobody enforces this correctly |
| C4 | Distributed lease-based budget enforcement, correct under concurrency and partition | The distributed-systems contribution |
| C5 | Org-level policy layer (Cedar) evaluated in the hot path | Enterprise legibility |
| C6 | Natural-language → policy compiler **with verification step** | CT-AI nomination hook |
| C7 | Intent binding + goal-drift detection | CT-AI nomination hook |
| C8 | Just-in-time elevation with human approval | Demo gold, easy sell |
| C9 | Fast revocation (whole subtree) | Demo gold |
| C10 | Hash-chained tamper-evident audit ledger + chain-of-custody query | Bank/compliance judges |
| C11 | MCP gateway + generic HTTP PEP | Adoption |
| C12 | Admin console: identity tree, live decisions, escalation queue, audit explorer | Demo surface |
| C13 | Reference demo agent (procurement/payments) | Demo substrate |
| C14 | Observability: OTEL traces, Prometheus metrics, Grafana dashboards | Evidence pack |
| C15 | Load + chaos + adversarial test suites with published results | Evidence pack |

### 1.4 Explicitly out of scope

Do not build these. When one of them starts to look tempting mid-build, come back and read this list — that temptation is risk R-6 (scope creep) arriving on schedule.

- Moving real money. We emit *settlement instructions*; a stub PSP adapter consumes them. Legal non-starter otherwise.
- Our own cryptographic primitives. Use `biscuit-python`. Zero exceptions.
- Our own policy language. Use Cedar (and biscuit's embedded Datalog for token-level checks).
- Our own database, queue, graph store, or key-value store.
- Training a foundation model. We fine-tune nothing larger than a small classifier.
- Mobile app.
- Blockchain of any kind. (The audit ledger is a hash chain, and we call it a hash chain.)
- Multi-region active-active. Single region, documented as future work.
- Full NHI discovery/CSPM (that is a different product — Direction B, rejected).
- SCIM / enterprise SSO connector catalogue.

### 1.5 Non-functional requirements (these become tests)

| ID | Requirement | Measurement |
|---|---|---|
| NFR-1 | Authorization decision latency p99 < 1 ms (in-process, excluding network I/O) | pytest-benchmark, reported separately from proxy overhead |
| NFR-2 | End-to-end PEP proxy overhead p99 < 8 ms at 500 RPS single instance | Locust + Prometheus histogram |
| NFR-3 | Budget invariant `Σ spend ≤ mandate` holds under all tested concurrency and partition scenarios | Invariant checker in chaos suite |
| NFR-4 | Revocation propagates to all PEPs in < 2 s p99 | Integration test with 3 PEP instances |
| NFR-5 | Zero plaintext secret in any log line | Log-scanning test |
| NFR-6 | Audit chain verification detects any single-record tampering | Property test |
| NFR-7 | PEP fails **closed** on control-plane unavailability by default, configurable to fail-open per policy | Chaos test |
| NFR-8 | Cold start of full stack via `docker compose up` < 90 s | CI timing test |
| NFR-9 | Drift detector false-positive rate on benign corpus < 5% at chosen threshold | Offline eval, reported honestly |
| NFR-10 | 100% of development performed in Bangladesh; all model weights open-weight and self-hosted | BIIN compliance; documented in submission |

**On NFR-1, be precise and honest.** Python cannot proxy a request in under a millisecond. It *can* evaluate an authorization decision in well under a millisecond. Always report the two numbers separately, and label them. A judge who catches you conflating them will discount everything else you said. A judge who sees you separate them voluntarily will trust the rest.

---

<a name="2"></a>
## 2. Glossary

Use these terms consistently in code, docs, and the pitch.

| Term | Meaning |
|---|---|
| **Principal** | Human who ultimately authorizes work. Authenticated via OIDC. |
| **Task** | A unit of work a principal approves. Has an id, a natural-language description, an intent hash, and a mandate. |
| **Mandate** | The root grant for a task: scopes + quantitative budgets + expiry + max delegation depth. |
| **Agent** | A process acting under a token. Has a role. |
| **Sub-agent** | An agent spawned by another agent, holding an attenuated token. |
| **Token** | A biscuit. Carries identity, task binding, and caveats. Verifiable offline. |
| **Caveat** | A restriction inside a token. Monotonic: adding caveats can only narrow authority. |
| **Attenuation** | Holder-side creation of a narrower child token, offline, no issuer round-trip. |
| **Scope** | A capability string, e.g. `invoice:read`, `payment:initiate`. |
| **Budget** | A quantitative limit: `spend_bdt`, `tool_calls`, `rows_read`, `wall_clock_s`, `external_emails`. |
| **Lease** | A revocable, TTL-bounded allocation of budget from the ledger to one PEP instance. |
| **PEP** | Policy Enforcement Point. The gateway/sidecar. Verifies, decides, spends, logs. |
| **PDP** | Policy Decision Point. In our design, embedded in the PEP for speed. |
| **Intent hash** | Hash of the canonicalized task description, bound into the token. |
| **Drift** | Divergence between an attempted action and the approved task intent. |
| **Elevation** | Temporary widening via a fresh, short-lived token issued after human approval. Never mutation of an existing token. |
| **Decision record** | The immutable record of one authorization decision. |
| **Chain of custody** | The verifiable path principal → task → agent → sub-agent → caveat → action. |

---

<a name="3"></a>
## 3. System architecture

### 3.1 Component diagram

```
┌────────────────────────────────────────────────────────────────────┐
│ CLIENT SIDE                                                        │
│                                                                    │
│  Reference agent (procurement)          agentiam-sdk (Python)      │
│    root agent ──spawns──▶ sub-agents      · attenuate()            │
│         │                                 · spend context mgr      │
│         └── all tool calls ───────┐       · escalate()             │
└───────────────────────────────────┼────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ DATA PLANE — PEP (agentiam-pep)         [HOT PATH, must be fast]   │
│                                                                    │
│  1. extract token          (µs)                                    │
│  2. verify biscuit chain   (µs)   ← offline, public key only        │
│  3. check revocation       (µs)   ← in-memory bloom + set          │
│  4. evaluate token Datalog (µs)   ← biscuit authorizer             │
│  5. evaluate Cedar policy  (µs)   ← in-process, cached bundle      │
│  6. drift check            (cached / async / inline-strict)         │
│  7. reserve from lease     (µs)   ← local counter, no network       │
│  8. forward upstream       (network)                               │
│  9. commit/refund actual   (µs)                                    │
│ 10. emit decision record   (async, buffered)                       │
│                                                                    │
│  MCP mode: same pipeline, MCP-aware scope + arg extraction         │
└───────────────┬──────────────────────────────────┬─────────────────┘
                │ async: leases, revocation, records│ upstream
                ▼                                  ▼
┌────────────────────────────────────────────┐  ┌──────────────────┐
│ CONTROL PLANE (FastAPI services)           │  │ TOOL SERVERS     │
│                                            │  │ MCP servers,     │
│  issuance     — mandates, root tokens      │  │ internal APIs,   │
│  ledger       — budgets, leases, invariant │  │ stub PSP         │
│  policy       — Cedar bundles, versioning  │  └──────────────────┘
│  compiler     — NL→Cedar + verifier        │
│  drift        — embeddings + classifier    │
│  revocation   — revoke, subtree, gossip    │
│  audit        — hash-chained ledger        │
│  escalation   — approval workflow          │
│                                            │
│  Postgres · Redis · Ollama · MinIO(opt)    │
└────────────────────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────────────────┐
│ SURFACES & OBSERVABILITY                                           │
│  Admin console (Next.js or FastAPI+HTMX)                           │
│  OTEL Collector → Tempo (traces) · Prometheus (metrics) · Loki     │
│  Grafana dashboards: decisions, latency, budgets, denials, drift   │
│  Keycloak (human OIDC) · Vault dev mode (keys)                     │
└────────────────────────────────────────────────────────────────────┘
```

### 3.2 Design principles

1. **Hot path never blocks on the network.** Verification, policy, revocation, and budget spend are all local. Leases and revocation lists are pushed/pulled asynchronously. This is the reason the latency claim is defensible.
2. **Fail closed by default.** No lease, no stale-window allowance, expired policy bundle beyond max staleness → deny. Fail-open is per-scope, opt-in, and audited.
3. **Tokens are never mutated.** Elevation issues a new token. Narrowing creates a new token. Tokens are immutable values.
4. **Every deny is explainable.** A decision record names the exact caveat, policy statement, or budget that caused it. "Denied" without a reason is a bug.
5. **Two-layer authorization.** Token-embedded Datalog checks travel with the token (portable, offline-verifiable). Cedar policy is org-level and centrally managed. Both must pass.
6. **The ledger is the only authority on budget.** PEPs hold leases, not truth.
7. **Observability is a feature, not instrumentation.** The Grafana dashboards *are* part of the demo.

### 3.3 Request lifecycle (narrative)

A principal approves a task in the console: *"Procure 500 units of packaging film, budget ৳500,000."* The issuance service canonicalizes the description, computes the intent hash, creates the mandate in the ledger, and mints a root biscuit bound to `task_id` and `intent_hash`.

The root agent receives that token. It decides to spawn a document-reader sub-agent. It calls `sdk.attenuate(scopes=["invoice:read"], budget={"spend_bdt": 0}, depth=1)` — entirely locally, no network. The child token cryptographically cannot exceed `invoice:read` or spend anything.

The sub-agent calls a tool. The PEP verifies the biscuit chain against the public key, checks the revocation set, runs the token's Datalog checks, evaluates the Cedar bundle, checks drift against the intent hash, and attempts to reserve from its local lease. Because the child's spend budget is zero, a payment call is denied at step 5 or 7 with the exact failing caveat named. Total time: microseconds. No network call was made to deny it.

When the root agent does initiate a legitimate payment, the PEP reserves the amount from its local lease, forwards the call, then commits the actual amount (which may differ from the estimate) and refunds the difference. If the lease is insufficient, the PEP requests a top-up from the ledger; if the mandate is exhausted, the request is denied and an escalation is offered.

Every one of these decisions becomes a hash-chained audit record. Asking "who authorized this payment?" walks the chain back to the human and the specific caveat that permitted it.

---

<a name="4"></a>
## 4. Technology stack

**Everything here is free and open source. No paid service appears anywhere in this project.** That is a scoring advantage under BIIN's IP rules, not a compromise — see §14.4.

### 4.1 Core

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | Locked |
| Package manager | `uv` | Fast, lockfile, workspaces |
| Web framework | FastAPI + Pydantic v2 | Control plane |
| ASGI server | Uvicorn + `uvloop` + `httptools` | |
| JSON | `orjson` | Hot path |
| HTTP client | `httpx` (async, connection pooling) | PEP upstream |
| Tokens | `biscuit-python` | **Do not substitute** |
| Policy | `cedarpy` (Rust-backed, in-process) | Hot path. OPA sidecar as optional second backend behind the same interface |
| DB | PostgreSQL 16 | |
| ORM/migrations | SQLAlchemy 2.0 (async) + Alembic | |
| Cache/leases/gossip | Redis 7 | Also pub/sub for revocation gossip |
| Local LLM | Ollama + Qwen2.5-7B-Instruct (fallback: Llama-3.1-8B) | Policy compiler. Open weights = we own the IP |
| Embeddings | `sentence-transformers` / `bge-small-en-v1.5` | Drift detection, CPU-fine |
| Classifier | scikit-learn logistic regression + `CalibratedClassifierCV` | Drift; calibration matters more than accuracy |
| Human auth | Keycloak (OIDC) | |
| Workload identity | SPIRE (optional, M9) | Nice-to-have; document if skipped |
| Secrets | HashiCorp Vault dev mode / env in dev | |
| Object store | MinIO (only if we store bundles/artifacts) | |

### 4.2 Observability

OpenTelemetry Python SDK → OTEL Collector → Tempo (traces), Prometheus (metrics), Loki (logs), Grafana (dashboards). All OSS, all in `docker-compose.observability.yml`.

### 4.3 Frontend

Default: **FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind.** Rationale: one language, one deploy, no build step, fast enough. Next.js would only pay for itself if React were already a deep strength — the identity-tree visualization is the one piece that benefits, and D3 in a Jinja template handles it. A second language and a build step is not a trade worth making for one screen.

Identity tree visualization: `d3-hierarchy` for the tree, or Cytoscape.js. Must animate on token mint/revoke.

### 4.4 Testing & quality

| Concern | Tool |
|---|---|
| Unit / integration | `pytest`, `pytest-asyncio` |
| Property-based | `hypothesis` — **mandatory for attenuation and lease invariants** |
| Fixtures with real deps | `testcontainers[postgres,redis]` |
| API contract fuzzing | `schemathesis` (against OpenAPI) |
| Coverage | `pytest-cov` |
| Microbenchmarks | `pytest-benchmark` |
| Load | `locust` (primary), `k6` (secondary, for the report) |
| Fault injection | `toxiproxy` |
| Mutation testing | `mutmut` — on `attenuation/`, `ledger/`, `policy/` only |
| Lint/format | `ruff` |
| Types | `mypy --strict` |
| Security scan | `bandit`, `pip-audit`, `trivy` (images) |
| Secret scan | `gitleaks` |
| Pre-commit | `pre-commit` |
| CI | GitHub Actions (free tier) |

### 4.5 Deployment

Dev: `docker compose`. Demo: `docker compose` on a single VM, plus a k3s manifest set to show Kubernetes-readiness. Free hosting: Oracle Cloud always-free ARM (4 vCPU / 24 GB — genuinely enough), plus university lab machines. GPU (only for the policy compiler and only if you want faster inference): Kaggle 30 GPU-hr/week, Colab. CPU-only Qwen-7B-Q4 works; it is just slower, which is fine because the compiler is not in the hot path.

---

<a name="5"></a>
## 5. Repository layout

Monorepo, `uv` workspace.

```
agentiam/
├── README.md
├── pyproject.toml                 # uv workspace root
├── uv.lock
├── .pre-commit-config.yaml
├── Makefile                       # make up | test | bench | chaos | demo
├── docker-compose.yml
├── docker-compose.observability.yml
├── docker-compose.demo.yml
│
├── docs/
│   ├── PLAN.md                    # THIS FILE — authoritative on what to build
│   ├── ROADMAP.md                 # milestone sequencing and exit gates
│   ├── ENGINEERING-RULES.md       # project standards — authoritative on how to build
│   ├── DEMO.md                    # demo runbook, failure drills, judge handling
│   ├── DECISIONS.md               # ADR log, append-only
│   ├── specs/
│   │   ├── 01-token-format.md
│   │   ├── 02-caveat-language.md
│   │   ├── 03-attenuation.md
│   │   ├── 04-lease-protocol.md
│   │   ├── 05-policy-layer.md
│   │   ├── 06-drift-detection.md
│   │   ├── 07-revocation.md
│   │   ├── 08-audit-ledger.md
│   │   └── 09-decision-record.md
│   ├── openapi/                   # generated, committed
│   ├── benchmarks/                # results, committed — these go in the submission
│   ├── threat-model.md
│   └── paper/                     # LaTeX
│
├── packages/
│   ├── agentiam-core/             # pure logic, ZERO I/O
│   │   └── src/agentiam_core/
│   │       ├── models.py          # Pydantic: Mandate, Caveat, Budget, DecisionRecord
│   │       ├── tokens.py          # biscuit wrap: mint, attenuate, verify
│   │       ├── caveats.py         # caveat DSL ↔ Datalog
│   │       ├── attenuation.py     # narrowing algebra + invariant checks
│   │       ├── budget.py          # budget arithmetic, Decimal only
│   │       ├── intent.py          # canonicalization + intent hashing
│   │       ├── policy.py          # PolicyEngine protocol
│   │       ├── decision.py        # decision pipeline (pure, injected deps)
│   │       ├── errors.py
│   │       └── hashing.py         # canonical serialization + hash chain
│   │
│   ├── agentiam-sdk/              # what agent developers import
│   │   └── src/agentiam_sdk/
│   │       ├── client.py          # attenuate, spend, escalate
│   │       ├── context.py         # contextvars token propagation
│   │       ├── transports.py      # httpx + MCP client wrappers
│   │       └── decorators.py      # @requires_scope, @budgeted
│   │
│   ├── agentiam-pep/              # the gateway
│   │   └── src/agentiam_pep/
│   │       ├── app.py             # ASGI
│   │       ├── pipeline.py        # the 10 steps
│   │       ├── extractors/        # http.py, mcp.py — scope+args from a request
│   │       ├── lease_client.py    # local lease pool + top-up + reclaim
│   │       ├── revocation_cache.py# bloom + exact set + pubsub subscriber
│   │       ├── policy_cache.py    # bundle fetch, verify sig, staleness guard
│   │       ├── drift_client.py    # cached / async / strict modes
│   │       ├── emitter.py         # buffered decision records
│   │       └── config.py
│   │
│   ├── agentiam-controlplane/
│   │   └── src/agentiam_controlplane/
│   │       ├── main.py
│   │       ├── db/                # models, alembic
│   │       ├── issuance/
│   │       ├── ledger/            # THE HARD PART — leases
│   │       ├── policy/
│   │       ├── compiler/          # NL→Cedar + verifier
│   │       ├── drift/
│   │       ├── revocation/
│   │       ├── audit/
│   │       ├── escalation/
│   │       └── console/           # Jinja+HTMX templates
│   │
│   └── agentiam-demo/
│       └── src/agentiam_demo/
│           ├── procurement_agent.py
│           ├── subagents/
│           ├── tools/             # mcp servers: invoice, vendor, payment(stub), email(stub)
│           └── scenarios/         # scripted demo beats, deterministic
│
├── tests/
│   ├── unit/
│   ├── property/                  # hypothesis
│   ├── integration/
│   ├── e2e/
│   ├── security/                  # red team, §12
│   ├── chaos/                     # §13
│   ├── perf/                      # locust, benchmarks
│   └── fixtures/
│
├── deploy/
│   ├── k3s/
│   └── grafana/                   # dashboards as JSON, committed
└── scripts/
    ├── seed_demo.py
    ├── verify_audit_chain.py
    ├── run_invariant_checker.py
    └── generate_evidence_pack.py
```

**Hard rule:** `agentiam-core` has no I/O, no DB, no HTTP, no clock reads (clock is injected). It is pure, fully unit-testable, and property-testable. Every correctness claim in the paper is a claim about `agentiam-core`. Guard this with a CI check that fails if `agentiam-core` imports `httpx`, `sqlalchemy`, `redis`, or `datetime.now`.

---

<a name="6"></a>
## 6. Core specifications

### 6.1 Mandate & token format

```python
class Budget(BaseModel):
    spend_bdt: Decimal = Decimal(0)      # money — Decimal, never float
    tool_calls: int = 0
    rows_read: int = 0
    external_emails: int = 0
    wall_clock_s: int = 0
    model_config = ConfigDict(frozen=True)

class Mandate(BaseModel):
    mandate_id: UUID
    task_id: UUID
    principal_id: str                     # OIDC subject
    intent_hash: str                      # sha256 of canonicalized description
    scopes: frozenset[str]
    budget: Budget
    max_depth: int                        # delegation depth cap
    not_before: datetime
    expires_at: datetime
    model_config = ConfigDict(frozen=True)
```

> **Superseded in detail by [`docs/specs/01-token-format.md`](specs/01-token-format.md) (T-002).**
> The sketch below is correct in intent. Three details in it do not survive contact with
> biscuit's actual semantics — budget values must be scaled integers rather than strings, depth
> must come from the block count rather than a block fact, and checks must constrain
> verifier-supplied request facts rather than the token's own grant facts. All three were
> measured, not assumed; see ADR-005. Where the two documents disagree, the spec wins.

**Token = biscuit.** Structure:

- **Authority block** (signed by the issuance service, the only block using the root keypair):
  - facts: `mandate(id)`, `task(id)`, `principal(sub)`, `intent(hash)`, `depth(0)`, `issued_at(ts)`
  - facts: `scope("invoice:read")` … one per granted scope
  - facts: `budget("spend_bdt", "500000")` … one per dimension
  - checks: expiry, `not_before`, depth ≤ `max_depth`
- **Attenuation blocks** (appended by holders, each signed by an ephemeral key the holder generates):
  - facts: `agent(id)`, `role(name)`, `depth(n)`
  - checks: scope subset, budget ceilings, time window, tool allow/deny lists, argument predicates

**Why biscuit and not JWT:** a JWT holder cannot narrow a JWT — narrowing requires the issuer. Biscuit's append-only attenuation with per-block signatures makes holder-side narrowing cryptographically sound and offline-verifiable. Say exactly this on stage; it is the sentence that separates you from every "we put agents in Okta" competitor.

**Token size guard:** every attenuation grows the token. Deep chains hit HTTP header limits. Specify: `max_depth ≤ 8`, warn above 4 KB, hard error above 8 KB, and if a chain must go deeper use a **token reference** (opaque handle, token body fetched from a local PEP-side store). Test this — see §11.

### 6.2 Caveat language

A thin, typed Python DSL that compiles to biscuit Datalog. The DSL exists so that the *narrowing check* is decidable in our code; raw Datalog is not.

| Caveat | Form | Semantics |
|---|---|---|
| `ScopeSubset` | `scopes ⊆ S` | Allowed scopes limited to `S` |
| `BudgetCeiling` | `dimension ≤ v` | Quantitative ceiling |
| `TimeWindow` | `[t0, t1]` | Valid only within window |
| `ToolAllow` / `ToolDeny` | set of tool ids | Deny wins over allow |
| `ArgPredicate` | `arg.path op value` | e.g. `payment.amount ≤ 50000`, `email.domain in {...}` |
| `RateLimit` | `n per window` | Calls per rolling window |
| `DepthLimit` | `depth ≤ n` | Further delegation cap |
| `IntentBound` | `intent == hash` | Pins the token to a task intent |
| `RequiresApproval` | scope set | Forces escalation path |

**Every caveat type must implement:**

```python
class Caveat(Protocol):
    def to_datalog(self) -> str: ...
    def narrows(self, other: "Caveat") -> bool:
        """True iff self is at least as restrictive as other (same type)."""
    def evaluate(self, ctx: RequestContext) -> CaveatResult: ...
```

`narrows()` is what makes the attenuation invariant checkable, and it is what the property tests hammer. If a new caveat type cannot implement `narrows()` soundly, it does not go in the language.

### 6.3 Attenuation semantics + invariants

**Definition.** `attenuate(T, C) → T'` appends caveat set `C` to token `T`, producing `T'`.

**Invariants (these become property tests, and they are the formal core of the paper):**

| ID | Invariant |
|---|---|
| **INV-1 Monotonicity** | `authority(T') ⊆ authority(T)` for all `T`, `C`. No caveat set can widen. |
| **INV-2 Transitivity** | `attenuate(attenuate(T,C1),C2)` has authority `⊆` both intermediate tokens. |
| **INV-3 Offline soundness** | Verification requires only the root public key and the token. No network, no state. |
| **INV-4 Non-forgeability** | Without a parent's block key, a valid extension cannot be produced. |
| **INV-5 Budget subadditivity** | For any dimension `d`, `Σ over children(budget_d) ≤ parent budget_d` is *enforced at spend time* even when statically it is not (siblings can each be granted the full parent ceiling; the ledger prevents the sum from being exceeded). |
| **INV-6 Depth bound** | `depth(T') = depth(T) + 1`, and any token with `depth > max_depth` fails verification. |
| **INV-7 Intent stability** | `intent_hash` is set once in the authority block and cannot be changed by attenuation. |
| **INV-8 Deny precedence** | If any caveat in the chain denies, the decision is deny, regardless of any allow. |
| **INV-9 Expiry contraction** | `expiry(T') ≤ expiry(T)`. |
| **INV-10 No resurrection** | A token whose ancestor is revoked cannot authorize anything. |

**INV-5 deserves emphasis because it is subtle and it is the reason the ledger exists.** Static token inspection cannot bound total spend when a parent hands the same ৳50,000 ceiling to three children — each child's token is individually valid, and together they could spend ৳150,000. Two mitigations, and we implement both: *proportional split* (parent explicitly divides the budget, enforced statically) and *shared pool* (children reference the same ledger budget id, enforced dynamically by the lease system). Default is shared pool, because it is what real workflows want. This distinction is a genuinely publishable observation — most capability-token literature does not address quantitative resources shared across siblings.

### 6.4 Budget lease protocol

**This is the hardest component and your strongest technical claim. Specify it fully before any code.**

**Problem.** Enforce a global limit (`Σ spend ≤ mandate`) without a network round-trip on every call, under concurrent sub-agents, PEP crashes, and network partition.

**Design: lease-based allocation with reservation two-phase spend.**

Ledger state per `(mandate_id, dimension)`:
```
total          Decimal    # the mandate ceiling
committed      Decimal    # irrevocably spent
leased         Decimal    # currently held by PEPs, not yet committed
available      Decimal    # = total - committed - leased  (derived, never stored)
```

Lease record: `lease_id, mandate_id, dimension, pep_id, amount, granted_at, expires_at, state ∈ {active, released, expired, revoked}`.

**Operations.**

```
ACQUIRE(mandate_id, dimension, requested, pep_id, ttl) → Lease | Insufficient
  BEGIN; SELECT ... FOR UPDATE on budget row
  available = total - committed - leased
  grant = min(requested, available)
  if grant <= 0: return Insufficient
  leased += grant; INSERT lease(active, expires_at = now + ttl)
  COMMIT

RESERVE(lease, amount) → local, no network
  if lease.remaining_local >= amount:
      lease.remaining_local -= amount
      return Reservation(id, amount)
  else: trigger TOP_UP, else Insufficient

COMMIT(reservation, actual)
  # actual may be < or > estimate
  delta = actual - reservation.amount
  if delta > 0: RESERVE(lease, delta) or escalate
  if delta < 0: lease.remaining_local += -delta
  enqueue committed_delta(actual) → ledger (batched, async)

LEDGER_COMMIT(lease_id, amount)   # ledger side, idempotent by reservation_id
  BEGIN; FOR UPDATE
  committed += amount; leased -= amount; lease.amount -= amount
  COMMIT

RELEASE(lease)     # graceful shutdown or idle
  BEGIN; leased -= lease.remaining; lease.state = released; COMMIT

REAP()             # background, every TTL/4
  for each lease where expires_at < now and state = active:
      leased -= lease.remaining; state = expired

REVOKE(mandate_id) # immediate hard stop
  mark all leases revoked; publish to revocation channel; PEPs drop leases
```

**Lease sizing policy.** Fixed size is wrong: too small ⇒ constant network chatter; too large ⇒ a crashed PEP strands budget until TTL. Use adaptive sizing: `lease = clamp(EWMA(spend_rate) × target_horizon, min_lease, max_fraction × available)`. Defaults: `target_horizon = 30 s`, `min_lease` = one typical call, `max_fraction = 0.25`, `ttl = 60 s`. **Make all of these configurable and put a Grafana panel on lease utilization** — that panel is a genuine "this team understands distributed systems" signal to the judges.

**Correctness argument (write this out in `docs/specs/04-lease-protocol.md`):**

- *Safety* (never overspend): every path that increases `committed` or `leased` does so inside a serialized transaction on the budget row, and the guard `total - committed - leased ≥ 0` is enforced there. A PEP can only spend from `remaining_local ≤ lease.amount`, and `lease.amount` was already deducted from `available` at acquire time. Therefore `committed + leased ≤ total` always, and `Σ spend ≤ total`.
- *Liveness*: leases expire, so a crashed PEP's budget returns within TTL. Formally, availability is degraded by at most `Σ stranded leases` for at most `ttl`.
- *Partition behaviour*: a partitioned PEP continues spending its lease (bounded, safe) and cannot top up. On TTL expiry it fails closed. **This is CP, not AP, and that is the correct choice for money** — say so explicitly; a bank CTO will specifically ask.
- *Idempotency*: commits are keyed by `reservation_id`; replay is a no-op.
- *Clock skew*: TTLs use ledger-issued absolute expiry plus a skew allowance; PEPs treat lease expiry conservatively (expire early, never late).

**Known limitation to state honestly:** stranded budget during a PEP crash is real. The mitigations are short TTL, graceful-shutdown release, and a heartbeat-based early reclaim. Do not pretend it does not exist. Listing a limitation and its bound is what senior engineers do, and the academic judges will read it as maturity.

### 6.5 Policy layer

Two layers, both must pass:

1. **Token-embedded Datalog** (biscuit) — travels with the token, offline, per-delegation. Answers "what did this specific chain of delegation permit?"
2. **Org policy (Cedar)** — centrally managed, versioned, signed bundles. Answers "what does the organization permit at all, regardless of token?"

Cedar entity model:
```
Principal:  AgentIAM::Agent      (attrs: role, depth, task_id, principal_id)
Action:     AgentIAM::Action     ("invoice:read", "payment:initiate", …)
Resource:   AgentIAM::Tool       (attrs: tool_id, server, sensitivity, is_external)
Context:    { time, amount, arg_digest, drift_score, elevated, environment }
```

Bundle: `{version, cedar_source, entity_schema, signature, created_at}`. PEP fetches, verifies signature against the policy service public key, caches in memory, and refuses to serve on a bundle older than `max_staleness` (default 300 s) — fail closed.

**Policy engine abstraction** (`PolicyEngine` protocol) with two implementations: `CedarEngine` (default) and `OpaEngine` (sidecar). Having two backends behind one interface is a small amount of work and a disproportionately good architecture-slide talking point.

### 6.6 Intent binding & drift detection

**Canonicalization + hashing** (`intent.py`): lowercase, collapse whitespace, strip punctuation, NFKC normalize, sort nothing (order matters for meaning), then `sha256`. Bind the hash into the authority block. This makes "which task authorized this?" cryptographically answerable.

**Drift detection pipeline:**

```
inputs:  task_description, action = (scope, tool_id, canonical_args)
  ↓
features:
  f1  cosine(embed(task_description), embed(action_template))
  f2  cosine(embed(task_description), embed(rendered_action_with_args))
  f3  scope-in-task-plan prior (does this scope normally appear for this task type?)
  f4  step position / sequence anomaly (this action at this point in the trace)
  f5  argument entity overlap with task description (vendors, amounts, names)
  f6  historical frequency of (task_type, scope) pair
  ↓
calibrated logistic regression → P(drift)
  ↓
bands:  < 0.3  allow
        0.3–0.7 allow + flag (async review)
        > 0.7  escalate to human   ← NOT deny
```

**Three critical design decisions, all defensible on stage:**

1. **Escalate, don't deny.** A false positive that blocks legitimate work destroys adoption. Escalation costs a human 5 seconds. This is a product decision *and* a safety decision.
2. **Report calibration, not accuracy.** Ship a reliability diagram, Brier score, and the false-positive rate at your operating threshold. Professors Mridha, Mainul, and Kibria will trust a calibrated model with 82% accuracy over an uncalibrated one claiming 97%.
3. **Log-only mode is the default in production.** Enforcement is opt-in per scope. Ship it observing before it ship it blocking.

**Dataset construction (start this in M6, it is the long-lead item).** Generate task/action pairs from the demo workflows: benign pairs (actions genuinely in service of the task) and drift pairs (actions from a *different* task, injected instructions, escalating-privilege actions, exfiltration-shaped actions). Target ≥ 2,000 labelled pairs, ≥ 30% drift. Hold out by *task*, not by row, or you will leak and your numbers will be fiction. If a second annotator can be recruited to label 200 pairs blind, report inter-annotator agreement — that single number buys enormous credibility. Solo, label the 200 twice with a gap of at least a week between passes and report intra-annotator agreement instead, labelled honestly as such.

### 6.7 Revocation

Three granularities: single token (by chain id), subtree (a token and everything attenuated from it), whole mandate.

**Mechanism:**
- Revocation ids: biscuit gives each block a revocation id. A token is revoked if *any* block id is in the revoked set (this gives subtree revocation for free — revoke the parent's block id and every descendant fails, because every descendant contains that block).
- Distribution: Redis pub/sub push (fast path) + periodic full-set pull (correctness backstop, in case a PEP missed a message).
- PEP-side: counting Bloom filter for the negative fast path + exact set for confirmation. Bloom false positives fall through to the exact set, so there are no false denials.
- Short token TTLs (default 15 min) bound the damage if distribution fails entirely.

**Test target (NFR-4):** revoke → all three PEP instances deny within 2 s p99. Measure it, put it in the evidence pack, show it in the demo.

### 6.8 Audit ledger

Hash-chained, append-only:

```
record_n.prev_hash = sha256(canonical_json(record_{n-1}))
```

Per-record: `seq`, `prev_hash`, `record_hash`, timestamp, decision record body. Periodic checkpoints signed by the audit service key. `scripts/verify_audit_chain.py` walks the chain and reports the first inconsistent seq.

**Say "hash chain," never "blockchain."** Judges who know the difference will respect the precision; judges who do not will hear "tamper-evident audit log," which is exactly the right message.

**Chain-of-custody query** — this is the single most valuable API for the banking judges:

```
GET /audit/custody?action_id=...
→ {
    principal:      {sub, name, approved_at},
    task:           {id, description, intent_hash},
    mandate:        {scopes, budget, expires_at},
    delegation_chain: [
      {agent_id, role, depth, block_id, caveats_added:[...]},
      ...
    ],
    permitting_caveat: {type, value, source_block},
    policy_version:  "v14",
    decision:        {allow, latency_us, drift_score, budget_state_before/after},
    audit_proof:     {seq, record_hash, checkpoint_signature}
  }
```

### 6.9 Decision record

```python
class DecisionRecord(BaseModel):
    decision_id: UUID
    trace_id: str                      # ties to OTEL
    timestamp: datetime
    pep_id: str
    token_chain_ids: list[str]
    principal_id: str
    task_id: UUID
    agent_id: str
    depth: int
    scope: str
    tool_id: str
    arg_digest: str                    # hash, NOT the args — args may contain PII
    outcome: Literal["allow","deny","escalate","allow_with_flag"]
    reason_code: str                   # machine-readable, e.g. BUDGET_EXHAUSTED
    reason_detail: str                 # human-readable, names the exact caveat
    failing_caveat: CaveatRef | None
    policy_version: str
    budget_before: Budget
    budget_after: Budget
    reservation_id: UUID | None
    drift_score: float | None
    latency_us: int
    elevated_by: str | None
```

**Reason codes** — closed enum, fixed early, never freeform strings:
`OK`, `TOKEN_INVALID_SIGNATURE`, `TOKEN_EXPIRED`, `TOKEN_NOT_YET_VALID`, `TOKEN_REVOKED`, `ANCESTOR_REVOKED`, `SCOPE_NOT_GRANTED`, `SCOPE_ATTENUATED_AWAY`, `TOOL_DENIED`, `ARG_PREDICATE_FAILED`, `DEPTH_EXCEEDED`, `BUDGET_EXHAUSTED_MANDATE`, `BUDGET_EXHAUSTED_CAVEAT`, `LEASE_UNAVAILABLE`, `RATE_LIMITED`, `POLICY_DENIED`, `POLICY_BUNDLE_STALE`, `DRIFT_ESCALATION`, `APPROVAL_REQUIRED`, `INTENT_MISMATCH`, `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`, `MALFORMED_REQUEST`, `TOKEN_TOO_LARGE`, `UPSTREAM_ERROR`.

Every deny path in the codebase maps to exactly one code. A CI test asserts that every code is reachable and every deny in the source cites one.

---

<a name="7"></a>
## 7. Data model

```sql
-- principals mirrored from OIDC
principals(id PK, oidc_sub UNIQUE, display_name, email, created_at)

tasks(id PK, principal_id FK, description TEXT, intent_hash,
      status ENUM(draft,approved,active,completed,revoked,expired),
      created_at, approved_at)

mandates(id PK, task_id FK, scopes TEXT[], max_depth INT,
         not_before, expires_at, root_token_chain_id, created_at)

budgets(id PK, mandate_id FK, dimension TEXT,
        total NUMERIC(20,4), committed NUMERIC(20,4), leased NUMERIC(20,4),
        version INT,                        -- optimistic concurrency
        UNIQUE(mandate_id, dimension))
-- INVARIANT (also a DB CHECK): committed >= 0 AND leased >= 0
--                          AND committed + leased <= total

leases(id PK, budget_id FK, pep_id, amount NUMERIC(20,4),
       remaining NUMERIC(20,4), granted_at, expires_at,
       state ENUM(active,released,expired,revoked),
       INDEX(budget_id,state), INDEX(expires_at) WHERE state='active')

reservations(id PK, lease_id FK, amount NUMERIC(20,4), actual NUMERIC(20,4),
             state ENUM(reserved,committed,refunded),
             created_at, settled_at)
-- idempotency: PK is client-generated UUID

agents(id PK, task_id FK, parent_agent_id FK NULL, role, depth INT,
       token_chain_id, block_id, created_at, terminated_at NULL)

tokens(chain_id PK, agent_id FK, parent_chain_id NULL, block_ids TEXT[],
       caveats JSONB, issued_at, expires_at, size_bytes INT)

revocations(id PK, block_id UNIQUE, scope ENUM(token,subtree,mandate),
            reason, revoked_by, revoked_at, expires_at)
-- expires_at = original token expiry; safe to prune after

policy_bundles(id PK, version INT UNIQUE, cedar_source TEXT,
               entity_schema JSONB, signature, created_by, created_at,
               activated_at NULL)

policy_tests(id PK, bundle_id FK, request JSONB, expected ENUM(allow,deny),
             actual ENUM(allow,deny) NULL, passed BOOL NULL)

decision_records(id PK, seq BIGSERIAL, prev_hash, record_hash,
                 trace_id, timestamp, body JSONB,
                 INDEX(trace_id), INDEX(timestamp),
                 INDEX((body->>'task_id')), INDEX((body->>'outcome')))

audit_checkpoints(id PK, seq BIGINT, root_hash, signature, created_at)

escalations(id PK, decision_id, task_id, agent_id, requested_scope,
            requested_amount NUMERIC(20,4) NULL, reason,
            state ENUM(pending,approved,denied,expired),
            requested_at, resolved_at, resolver_principal_id,
            issued_token_chain_id NULL)

drift_observations(id PK, decision_id, task_id, action_digest,
                   features JSONB, score FLOAT, band,
                   human_label BOOL NULL, labelled_by, labelled_at)
```

**Money rule:** `NUMERIC(20,4)` in Postgres, `Decimal` in Python. A `float` anywhere near a currency amount is a bug and CI should catch it (grep-based lint rule is fine).

---

<a name="8"></a>
## 8. API contracts

Full OpenAPI is generated and committed to `docs/openapi/`. Key endpoints:

### Issuance
```
POST /v1/tasks                        {description, scopes[], budget{}, max_depth, expires_at}
                                      → {task_id, intent_hash, status: draft}
POST /v1/tasks/{id}/approve           (principal auth required)
                                      → {mandate_id, root_token, expires_at}
GET  /v1/tasks/{id}                   → task + mandate + live budget state
POST /v1/tasks/{id}/revoke            {reason} → {revoked_block_ids[], propagated_at}
```

### Ledger
```
POST /v1/leases                       {mandate_id, dimension, requested, pep_id, ttl_s}
                                      → {lease_id, amount, expires_at} | 409 Insufficient
POST /v1/leases/{id}/topup            {requested} → {amount} | 409
POST /v1/leases/{id}/release          {remaining} → 204
POST /v1/leases/{id}/commits          {reservation_id, actual}  (idempotent) → {committed_total}
GET  /v1/budgets/{mandate_id}         → per-dimension {total, committed, leased, available}
GET  /v1/budgets/{mandate_id}/invariant → {holds: bool, detail}   # for the invariant checker
```

### Policy
```
GET  /v1/policy/bundle/current        → signed bundle
POST /v1/policy/bundles               {cedar_source} → {version, id}   (draft)
POST /v1/policy/bundles/{id}/verify   → {test_results[], passed, failed}
POST /v1/policy/bundles/{id}/activate → 200 | 409 if tests failing
POST /v1/policy/compile               {natural_language} → {cedar_source, explanation,
                                                            generated_tests[], confidence}
```

### Drift
```
POST /v1/drift/score                  {task_id, scope, tool_id, arg_digest, features?}
                                      → {score, band, contributing_features[]}
POST /v1/drift/observations/{id}/label {label: bool}
```

### Revocation
```GET /v1/revocations?since=seq → {entries[], next_seq}```
Redis channel `agentiam:revocations` for push.

### Audit
```
GET /v1/audit/records?task_id=&from=&to=&outcome=
GET /v1/audit/custody?action_id=            # ← the money endpoint, see §6.8
GET /v1/audit/verify?from_seq=&to_seq=      → {valid, first_bad_seq}
```

### Escalation
```
POST /v1/escalations                  {decision_id, requested_scope, requested_amount, reason}
GET  /v1/escalations?state=pending
POST /v1/escalations/{id}/approve     {ttl_s, narrowed_scope?, max_amount?}
                                      → {elevated_token}
POST /v1/escalations/{id}/deny        {reason}
```

### PEP
```
ANY  /proxy/{upstream_path}           # generic HTTP PEP
POST /mcp                             # MCP streamable-HTTP gateway
GET  /healthz  /readyz  /metrics
GET  /debug/decide                    # dry-run a decision, dev only, disabled in prod
```

**SDK surface** (what an agent developer actually touches — keep it this small):

```python
client = AgentIAM(token=root_token, pep_url="http://localhost:8080")

child = client.attenuate(
    scopes=["invoice:read"],
    budget={"spend_bdt": 0, "tool_calls": 50},
    ttl_s=600,
    role="doc-reader",
)                                    # local, offline, no network

with client.spend("spend_bdt", estimate=Decimal("12000")) as res:
    result = client.call_tool("payment.initiate", {...})
    res.actual = result.amount        # commit the real number

try:
    client.call_tool("payment.initiate", {...})
except ApprovalRequired as e:
    token = client.await_escalation(e.escalation_id, timeout=120)
```

---

<a name="9"></a>
## 9. Phase plan M1–M12 with tickets

Each ticket: **ID · title · depends on · deliverables · acceptance criteria.** Feed one at a time.

### M1 — Foundations & specs

> Goal: repo, CI, and every core spec written and reviewed *before* implementation.

**T-001 · Repo scaffold + CI**
Deps: none.
Deliverables: uv workspace with the five packages (stubs only), `ruff`+`mypy --strict`+`pytest` configured, pre-commit, GitHub Actions running lint/type/test on push, Makefile targets `up/test/bench/lint`, `docker-compose.yml` with Postgres + Redis, `docs/ENGINEERING-RULES.md`, `docs/DECISIONS.md`.
Accept: `make test` green on an empty suite; CI green; `docker compose up` reaches healthy in < 90 s; a purity test asserts `agentiam-core` imports no I/O library.

**T-002 · Write `docs/specs/01-token-format.md`**
Deps: T-001. Spec only, no code.
Accept: authority-block fact list, attenuation-block fact list, all checks, size limits, worked example of a 3-level chain with byte counts. Read back and signed off before T-005.

**T-003 · Write `docs/specs/02-caveat-language.md` and `03-attenuation.md`**
Deps: T-002.
Accept: all 9 caveat types with Datalog mapping, `narrows()` semantics per type, all 10 invariants INV-1…INV-10 stated formally, plus at least 3 counterexamples showing what would violate each of INV-1/INV-5/INV-9.

**T-004 · Write `docs/specs/04-lease-protocol.md`**
Deps: T-001.
Accept: full pseudocode of all 7 operations, state machine diagram for lease states, safety and liveness arguments written out, partition behaviour, clock-skew handling, idempotency scheme, and an explicit "known limitations" section.

**T-005 · Domain models in `agentiam-core`**
Deps: T-002, T-003.
Deliverables: `models.py` — frozen Pydantic models for `Budget`, `Mandate`, `Caveat` union, `RequestContext`, `DecisionRecord`, `CaveatRef`; `errors.py`; `hashing.py` with canonical JSON serialization.
Accept: 100% branch coverage on `models.py`; property test that canonical serialization is stable across dict ordering and Unicode normalization; `Decimal` enforced for all money fields (test asserts float rejection).

**T-006 · Threat model document**
Deps: T-002.
Accept: `docs/threat-model.md` covering ≥ 12 threats using STRIDE, each with mitigation and the test id that covers it. Must include: token theft, replay, confused deputy, sibling budget race, lease stranding, revocation lag, policy-bundle rollback, prompt-injection-driven privilege escalation, drift-detector evasion, audit tampering, log PII leakage, control-plane DoS.

**M1 exit gate:** all four spec docs written, read back critically against §6, and committed. Do not start M2 until this is true — the specs are what T-009's property tests will be written *from*, and a property test derived from a wrong spec proves nothing.

---

### M2 — Token layer

**T-007 · biscuit wrapper: mint & verify**
Deps: T-005. Deliverables: `tokens.py` — keypair management, `mint_root(mandate) → Token`, `verify(token, public_key) → VerifiedToken | error`.
Accept: round-trip mint/verify; tampered byte fails; wrong public key fails; expired fails with `TOKEN_EXPIRED`; not-yet-valid fails; unit tests for each.

**T-008 · Caveat DSL → Datalog** `[REDUCED — 8 caveat types; RateLimit deferred]`
Deps: T-003, T-007.
Accept: all 8 caveat types (ScopeSubset, BudgetCeiling, TimeWindow, ToolAllow, ToolDeny, ArgPredicate, DepthLimit, IntentBound) compile to valid Datalog and evaluate correctly; table-driven tests with ≥ 5 cases per type including boundary values; malformed caveat raises at construction, not at evaluation. RateLimit deferred — see §21.

**T-009 · Attenuation + `narrows()`**
Deps: T-008. **This is the most important ticket in the project.**
Accept:
- `attenuate()` produces a verifiable child token, offline, with no network access (test asserts by monkeypatching socket to raise).
- Hypothesis property tests for **all** of INV-1, INV-2, INV-6, INV-7, INV-9: generate random mandates and random caveat chains up to depth 8; assert authority is non-increasing at every step.
- `narrows()` is reflexive, transitive, and antisymmetric-modulo-equivalence for every caveat type — property-tested.
- Attempted widening (child requests a scope the parent lacks) raises `AttenuationError` and never produces a token.
- `mutmut` on `attenuation.py` and `caveats.py`: surviving-mutant rate ≤ 10%.

**T-010 · Token reference (large-chain overflow)** `[DEFERRED — see §21]`
Deps: T-009.
Accept: chains beyond 4 KB emit a warning; beyond 8 KB the SDK switches to an opaque reference handle and the PEP resolves it locally; end-to-end test with a depth-8 chain over HTTP proving no header-size failure.
*Rationale: demo uses depth 3–4; the 8 KB problem will not appear. Deferred to post-submission.*

**T-011 · SDK: context propagation + attenuate**
Deps: T-009. Deliverables: `contextvars`-based token propagation, `attenuate()`, `@requires_scope`.
Accept: token propagates correctly across `asyncio.gather` and nested tasks; no cross-task leakage (test spawns 100 concurrent tasks with distinct tokens and asserts isolation); thread-pool boundary handled explicitly.

---

### M3 — Ledger & leases

**T-012 · Budget schema + migrations**
Deps: T-005. Accept: Alembic up/down clean; DB-level CHECK constraints for the invariant; `NUMERIC(20,4)` everywhere; testcontainers integration test.

**T-013 · ACQUIRE / RELEASE / REAP**
Deps: T-012, T-004.
Accept: row-level `FOR UPDATE` serialization verified by a concurrency test (50 concurrent acquires against a budget that fits 10 — exactly 10 succeed, 40 get `Insufficient`, `leased` is exact); reaper reclaims expired leases; released leases decrement `leased` exactly once (idempotent).

**T-014 · RESERVE / COMMIT / refund, idempotent**
Deps: T-013.
Accept: over-estimate refunds precisely; under-estimate tops up or escalates; duplicate commit with the same `reservation_id` is a no-op; commit after lease expiry is rejected with a clear code; Decimal arithmetic exact to 4 dp (test with values like `0.0001` and `999999.9999`).

**T-015 · Adaptive lease sizing** `[DEFERRED — see §21]`
Deps: T-013.
Accept: EWMA sizing implemented; simulation test over 3 traffic shapes (steady / bursty / spiky) shows top-up RPS reduced ≥ 60% vs fixed minimum sizing while stranded budget stays below `max_fraction`; results written to `docs/benchmarks/lease-sizing.md`.
*Rationale: fixed-size leases work identically for the demo. Algorithm specified in `docs/specs/04-lease-protocol.md`; implementation deferred as production optimization.*

**T-016 · Invariant checker (standalone tool)**
Deps: T-014. Deliverables: `scripts/run_invariant_checker.py` — continuously samples ledger state and asserts `committed + leased ≤ total` and `Σ committed = Σ settled reservations`.
Accept: runs as a sidecar during all chaos tests; detects a deliberately injected violation (a test that bypasses the transaction) within 1 s. **This tool is demo material — put it on screen.**

**T-017 · Sibling budget semantics: proportional split + shared pool**
Deps: T-014, T-009. Implements INV-5.
Accept: proportional split enforced statically in tokens; shared pool enforced dynamically; the key test — 3 sibling sub-agents each holding a token with the full parent ceiling, spending concurrently, total spend never exceeds the parent mandate. Run this with 3 PEP instances, not 1.

---

### M4 — PEP v1 (HTTP)

**T-018 · PEP skeleton + reverse proxy**
Deps: T-007. Accept: transparent proxying of GET/POST/streaming; header and trailer handling; upstream timeout and retry policy; `httpx` connection pooling; `/healthz` `/readyz` `/metrics`.

**T-019 · Decision pipeline (pure) in core**
Deps: T-008, T-009. Deliverables: `decision.py` — the 10-step pipeline as a pure function with injected dependencies (clock, revocation set, policy engine, lease pool, drift client).
Accept: table-driven tests over ≥ 40 scenarios covering every reason code; every deny names a `failing_caveat` where applicable; `pytest-benchmark` shows p99 < 1 ms for the pure decision with warm caches — **record this number, it is NFR-1**.

**T-020 · HTTP scope + argument extractor**
Deps: T-018. Accept: config-driven mapping (method, path pattern) → scope; argument extraction via JSONPath; `arg_digest` computed over canonicalized args; unmapped route → configurable default (deny by default); tested against ≥ 15 route patterns including path params and query params.

**T-021 · Local lease pool in PEP**
Deps: T-014, T-018. Accept: local reserve with zero network calls (asserted by a socket monkeypatch); async top-up at a low-water mark; graceful shutdown releases remaining budget; crash leaves stranded budget bounded by TTL (tested with SIGKILL).

**T-022 · Decision record emitter**
Deps: T-019. Accept: buffered async emit; back-pressure policy defined and tested (when the buffer is full: block, drop, or deny — default is *deny*, because losing audit records is a compliance failure, and this choice must be recorded in DECISIONS.md); zero PII in emitted records (only `arg_digest`); OTEL span per decision with `trace_id` correlation.

**T-023 · End-to-end thin slice**
Deps: T-018…T-022. Accept: an agent with a root token calls a stub tool through the PEP, spends budget, is denied when exhausted, and the decision appears in the audit ledger. **First demoable milestone — record a screencast and keep it.**

---

### M5 — Policy layer

**T-024 · Cedar engine + `PolicyEngine` protocol** `[REDUCED — CedarEngine only; OpaEngine stub]`
Deps: T-019. Accept: protocol with `CedarEngine` implementation; conformance suite of ≥ 30 request/expectation pairs; benchmark `CedarEngine`. `OpaEngine` implemented as a stub raising `NotImplementedError` to demonstrate the protocol abstraction. Full OPA implementation deferred — see §21.

**T-025 · Signed policy bundles + PEP cache**
Deps: T-024. Accept: bundle signature verified before use; unsigned or badly-signed bundle rejected; hot reload without dropping in-flight requests; staleness beyond `max_staleness` → fail closed with `POLICY_BUNDLE_STALE`; **rollback attack test** — an older signed bundle is rejected because bundle version must increase monotonically.

**T-026 · Policy test corpus + activation gate**
Deps: T-025. Accept: a bundle cannot be activated while any attached policy test fails (409); the corpus ships with ≥ 50 cases derived from the demo workflows.

**T-027 · Cedar authoring UI**
Deps: T-025. Accept: edit, run tests, see the diff of decisions vs the current bundle on the corpus, then activate. Showing *which decisions change* before activation is a great demo beat.

---

### M6 — NL→Policy compiler

**T-028 · Ollama client + constrained generation**
Deps: T-001. Accept: local Qwen2.5-7B via Ollama; grammar/schema-constrained output; deterministic settings (temperature 0, fixed seed) so the demo is repeatable; timeout and fallback path; **never** an external API (test asserts no non-localhost egress).

**T-029 · NL→Cedar compiler + auto-generated tests**
Deps: T-028, T-024.
Accept: for each of 30 curated English policy statements, the compiler produces Cedar that (a) parses, (b) passes the human-written expectation set. Report the success rate honestly — if it is 24/30, say 24/30 and show the 6 failures. **The compiler also generates candidate test requests for its own output**; a human accepts or edits them. This is the step that makes it engineering rather than a party trick.

**T-030 · Verify-before-deploy loop**
Deps: T-029, T-026. Accept: compiled policy is *never* activatable without passing tests; the UI shows the generated Cedar, the generated tests, the pass/fail table, and the decision diff; a deliberately ambiguous English input surfaces a clarifying question instead of guessing.

**T-031 · Template fallback**
Deps: T-029. Accept: on model failure, timeout, or low confidence, fall back to pattern-matched templates covering the 10 most common policy shapes. **Demo insurance — the compiler must never hang on stage.**

---

### M7 — Drift detection

**T-032 · Intent canonicalization + hashing**
Deps: T-005. Accept: canonicalization is idempotent and Unicode-stable (test with Bengali text, emoji, mixed scripts, NFKC edge cases); hash bound into the authority block; `INTENT_MISMATCH` path tested.

**T-033 · Feature extraction**
Deps: T-032. Accept: all 6 features f1–f6 computed; embedding model cached at startup; per-feature unit tests; p99 feature-extraction latency measured and reported (this one *is* allowed to be slow — it runs cached or async).

**T-034 · Drift dataset** `[DEFERRED — see §21]`
Deps: T-033. **Start early; this is the long-lead item.**
Accept: ≥ 2,000 labelled pairs, ≥ 30% drift class; held out **by task**, not by row; generation scripts committed and reproducible; 200-pair blind double-label with reported inter-annotator agreement (Cohen's κ).
*Rationale: dataset curation requires sustained human effort that no AI can shortcut. Rule-based drift v0 deployed instead. Dataset generation pipeline documented as roadmap.*

**T-035 · Calibrated classifier** `[DEFERRED — see §21]`
Deps: T-034. Accept: logistic regression with `CalibratedClassifierCV`; report AUROC, AUPRC, Brier score, reliability diagram, and FPR at the chosen threshold; FPR < 5% on the benign holdout (NFR-9); **model card** committed to `docs/`; failure-mode analysis of the 20 worst errors written up.
*Rationale: coupled to T-034 (deferred dataset). Rule-based drift v0 provides the same demo experience (escalation on drift). ML pipeline designed and documented as the research roadmap.*

**T-036 · Three drift modes in the PEP** `[MODIFIED — depends on drift v0 rule-based instead of T-035]`
Deps: drift v0 (rule-based), T-019. Accept: `off` / `log_only` (default) / `strict` per scope; cached scoring keyed by `(task_id, scope, arg_digest)`; async path never blocks the decision; strict path measured for added latency and reported separately; band → outcome mapping matches §6.6 exactly (>0.7 escalates, never denies).

---

### M8 — Escalation & revocation

**T-037 · Escalation workflow**
Deps: T-014. Accept: `ApprovalRequired` raised with an escalation id; pending queue in the console; approval issues a *new* short-lived token (never mutates the old one — test asserts the original token is unchanged); TTL expiry auto-denies; denial reason propagates to the agent; the elevated token is narrower than or equal to what was requested.

**T-038 · Revocation service + gossip**
Deps: T-007. Accept: token / subtree / mandate granularity; Redis pub/sub push plus periodic full pull; a PEP that misses a push still converges via pull within one pull interval (test by dropping the channel); revocation records prunable after original token expiry.

**T-039 · PEP revocation cache**
Deps: T-038. Accept: counting Bloom filter plus exact set; **zero false denials** (property test: 10,000 random non-revoked ids, none denied); false positives fall through to the exact set; NFR-4 met — 3 PEP instances all deny within 2 s p99, measured and recorded.

**T-040 · Subtree revocation e2e**
Deps: T-039, T-011. Accept: revoke root → a depth-4 tree of 12 agents all fail within 2 s; a *sibling* subtree is unaffected (this negative test matters — over-revocation is also a bug); measured propagation time recorded for the evidence pack.

---

### M9 — MCP gateway & identity substrate

**T-041 · MCP streamable-HTTP gateway** `[DEFERRED — see §21]`
Deps: T-018. Accept: `initialize`, `tools/list`, `tools/call`, notifications, and error mapping all proxied correctly; `tools/list` is *filtered by the caller's scopes* — an agent literally cannot see tools it may not call, which is a strong demo beat; a denial returns a well-formed MCP error, not a transport failure.
*Rationale: HTTP PEP demonstrates the full authorization pipeline. MCP gateway is the adoption path, not the core capability. Deferred to post-submission.*

**T-042 · MCP scope extractor** `[DEFERRED — see §21]`
Deps: T-041. Accept: `tool_id` → scope mapping; arguments extracted for `ArgPredicate` caveats; tested against ≥ 3 real MCP servers (filesystem, fetch, and one custom).
*Rationale: coupled to T-041 (deferred MCP gateway).*

**T-043 · Keycloak OIDC integration**
Deps: T-001. Accept: human login; task approval requires a valid session; `principal_id` from the OIDC `sub` flows into the mandate and appears in the custody chain.

**T-044 · SPIRE workload identity (optional)** `[DEFERRED — see §21]`
Deps: T-041. Accept: PEP↔control-plane mTLS via SPIFFE SVIDs, *or* a written decision in `DECISIONS.md` explaining the deferral with the mTLS alternative used instead. Do not let this ticket block M10.
*Rationale: mTLS between services achieves the same demo-visible outcome. SPIRE is infra complexity for zero demo value. Decision recorded in DECISIONS.md: "mTLS in place; SPIFFE deferred as production hardening."*

---

### M10 — Console & observability

**T-045 · Identity tree visualization**
Deps: T-011, T-038. Accept: live D3 tree of agents; each node shows role, depth, scopes, and remaining budget; animates on mint and on revoke; clicking a node shows its caveat chain and the biscuit block ids. **This is the single most important demo screen — budget real polish time here.**

**T-046 · Live decision stream**
Deps: T-022. Accept: SSE/WebSocket stream of decisions; filters by outcome, agent, scope; a denial shows the exact failing caveat inline, not a generic message.

**T-047 · Budget & lease dashboard**
Deps: T-016. Accept: per-mandate spend gauge; lease utilization; top-up rate; the invariant checker's status shown as a live green/red indicator.

**T-048 · Audit explorer + custody view**
Deps: T-022. Accept: search records; the custody view renders the full principal→caveat chain as a readable narrative; a "verify chain" button runs verification live and shows the result. **Bank judges will linger here.**

**T-049 · Grafana dashboards + OTEL wiring** `[REDUCED — 2 dashboards instead of 4]`
Deps: T-022. Accept: two dashboards committed as JSON in `deploy/grafana/` — Decisions (rate, outcome mix, reason codes) and Budgets (per-mandate spend, lease utilization); traces visible in Tempo with decision spans linked to upstream calls. Latency and Drift dashboards deferred — see §21.

**T-050 · Escalation queue UI**
Deps: T-037. Accept: approve/deny with an optional narrowing; the approver can reduce the requested scope or amount; approvals are audited with the approver's identity.

---

### M11 — Hardening, performance, evidence

**T-051 · Red-team suite** `[REDUCED — 15–20 attacks instead of 33]`
Deps: everything. See §12. Accept: 15–20 attacks covering key categories (A-01→A-09 token, A-10→A-13 privilege, A-17→A-18 budget, A-23→A-26 prompt injection, A-28→A-30 infra); all mitigated or explicitly documented as accepted risk with a rationale. Remaining attacks deferred — see §21.

**T-052 · Chaos suite** `[REDUCED — 5 scenarios instead of 12]`
Deps: T-016. See §13. Accept: 5 scenarios automated (CH-1 Postgres down, CH-3 PEP SIGKILL, CH-4 partition, CH-8 Ollama down, CH-10 rolling restart); the invariant holds in every run; results table committed. Remaining 7 scenarios deferred — see §21.

**T-053 · Load test + published numbers** `[REDUCED — 2 RPS profiles instead of 3]`
Deps: T-023. Accept: Locust profiles for 100 / 500 RPS; NFR-1 and NFR-2 measured and reported *separately*; latency breakdown by pipeline step; the flame graph committed. Report the numbers you actually get — a truthful 6 ms with a breakdown beats a claimed 1 ms that a judge can poke a hole in. 1000 RPS profile deferred — see §21.

**T-054 · Security scanning + SBOM**
Deps: T-001. Accept: `bandit`, `pip-audit`, `trivy`, `gitleaks` all in CI and clean (or with documented waivers); SBOM generated; secret-scanning test asserts no token, key, or PII in any log line at any log level.

**T-055 · Evidence pack generator**
Deps: T-051…T-054. Accept: `scripts/generate_evidence_pack.py` produces a single PDF/HTML bundle containing architecture diagrams, benchmark tables, chaos results, red-team results, the drift model card, the audit-chain verification transcript, and the coverage report. See §14.

**T-056 · Deployment artifacts**
Deps: all. Accept: one-command `docker compose` demo bring-up; k3s manifests; signed container images; a documented rollback procedure; cold start < 90 s (NFR-8).

---

### M12 — Demo, submission, paper

**T-057 · Demo scenario scripting**
Deps: T-045…T-050. Accept: all 8 demo beats (§15) scripted, deterministic, idempotently resettable via `make demo-reset`; every beat individually runnable in case of a stage failure.

**T-058 · Demo failure drills**
Deps: T-057. Accept: rehearsed fallbacks for — no network, Ollama down, GPU absent, Postgres restart mid-demo, judge input that breaks the parser, projector resolution change. Each drill has a documented recovery under 30 s. **Do this. Demos fail. Recovering gracefully in front of judges reads as maturity.**

**T-059 · Submission package**
Deps: T-055. Accept: architecture document, TAM analysis, scalability roadmap, IP statement (open weights, self-hosted, 100% BD development), socio-economic impact, evidence pack, cross-nomination materials for CT-AI and CC-RD. See §19.

**T-060 · Paper draft** `[DEFERRED — see §21]`
Deps: T-009, T-017, T-035, T-052. Accept: 8–10 pages, submittable to a USENIX Security / CCS / NDSS workshop. Contributions: (1) formal attenuation semantics with quantitative mandates, (2) the lease protocol with its safety proof and measured partition behaviour, (3) intent binding and calibrated drift detection with an honest evaluation, (4) the sibling-budget problem statement and both solutions.
*Rationale: BIIN judges score demo + technical report, not a published paper. The specs and evidence pack cover the academic content. Paper in preparation for post-submission.*

**T-061 · OSS release** `[REDUCED — public repo + README + demo video only]`
Deps: T-056. Accept: public GitHub repo, Apache-2.0 LICENSE, README.md with architecture overview and usage, recorded demo video. Full OSS release (CONTRIBUTING.md, verified quickstart, example integration, traction tracking) deferred — see §21.

---

<a name="10"></a>
## 10. Testing strategy

### 10.1 The pyramid, with actual targets

| Layer | Count target | Runtime | What lives here |
|---|---|---|---|
| Unit | 400+ | < 30 s | pure logic in `agentiam-core` |
| Property (hypothesis) | 25+ properties | < 3 min | attenuation invariants, budget arithmetic, canonicalization, Bloom filter |
| Integration | 100+ | < 5 min | DB, Redis, PEP↔control-plane, testcontainers |
| Contract | auto | < 2 min | schemathesis against OpenAPI |
| E2E | 25+ | < 10 min | full stack via compose |
| Security | 30+ | < 5 min | §12 |
| Chaos | 12 scenarios | < 20 min | §13, nightly |
| Performance | 6 profiles | < 30 min | nightly + before submission |

CI: unit + property + integration + contract on every push. E2E + security on PR to main. Chaos + performance nightly.

### 10.2 Coverage policy

Global ≥ 85%. **`attenuation.py`, `caveats.py`, `budget.py`, `decision.py`, and the ledger operations require 100% branch coverage.** These five files are where a bug is a security vulnerability rather than an inconvenience.

Mutation testing (`mutmut`) on those same five files, surviving-mutant rate ≤ 10%. Mutation testing is unusual in student projects and it is *exactly* the kind of rigor Prof. Mridha and Prof. Mainul will notice. It is worth a slide.

### 10.3 Property tests to write (non-negotiable list)

```
P-01  attenuate never widens authority                       (INV-1)
P-02  attenuation chains are transitively narrowing          (INV-2)
P-03  narrows() is reflexive                                 per caveat type
P-04  narrows() is transitive                                per caveat type
P-05  verify() needs no network                              socket monkeypatch
P-06  depth strictly increases; > max_depth always fails     (INV-6)
P-07  intent_hash immutable under attenuation                (INV-7)
P-08  expiry monotonically contracts                         (INV-9)
P-09  deny beats allow anywhere in the chain                 (INV-8)
P-10  Σ committed ≤ total for any interleaving of ops        (INV-5) — stateful hypothesis
P-11  reserve→commit→refund conserves budget exactly         Decimal exactness
P-12  duplicate commit is idempotent                         same reservation_id
P-13  canonical JSON stable under key reordering
P-14  intent canonicalization idempotent + Unicode stable
P-15  Bloom filter never false-negatives                     no false denials
P-16  hash chain detects any single-record mutation
P-17  token size grows monotonically with depth
P-18  every deny yields exactly one reason code
P-19  policy engines (Cedar, OPA) agree on the conformance suite
P-20  lease expiry never over-releases (leased ≥ 0 always)
P-21  revocation of any ancestor block kills all descendants (INV-10)
P-22  elevated tokens are ⊆ the approved request
P-23  drift score ∈ [0,1] for all inputs, no NaN
P-24  arg_digest is stable and collision-free on the corpus
P-25  decision pipeline is deterministic given fixed inputs + clock
```

**P-10 must be a stateful `hypothesis` `RuleBasedStateMachine`.** Rules: acquire, reserve, commit, refund, release, expire, crash, revoke. Invariant checked after every step: `committed + leased ≤ total` and `committed ≥ 0` and `leased ≥ 0`. This single test is the highest-value test in the entire project and probably the single best thing to show an academic judge.

### 10.4 Test data & fixtures

- Deterministic keypairs for tests, clearly marked as test-only, in `tests/fixtures/keys/`.
- Frozen clock fixture — **never** call `datetime.now()` outside an injected clock (CI grep-lints this).
- Factory fixtures for mandates, tokens, and delegation chains at each depth.
- A "golden chain" fixture: a canonical depth-4 delegation used across many tests so failures are comparable.
- Demo seed script producing a reproducible world state.

---

<a name="11"></a>
## 11. Edge case catalogue

Every row is a test. Group by area; the ID is the test name.

### 11.1 Tokens

| ID | Case | Expected |
|---|---|---|
| EC-T01 | Empty token / missing header | `MALFORMED_REQUEST`, 401 |
| EC-T02 | Truncated biscuit bytes | signature failure, no crash |
| EC-T03 | Single flipped bit in any block | signature failure |
| EC-T04 | Valid token, wrong root public key | reject |
| EC-T05 | Token from a rotated (old) root key | reject unless key still in the accepted set; test both |
| EC-T06 | `expires_at` exactly now | reject (boundary is exclusive — document the choice) |
| EC-T07 | `not_before` in the future | `TOKEN_NOT_YET_VALID` |
| EC-T08 | Clock skew ±30 s between PEP and ledger | configured tolerance honoured, no spurious denials |
| EC-T09 | Depth exactly `max_depth` | allow |
| EC-T10 | Depth `max_depth + 1` | `DEPTH_EXCEEDED` |
| EC-T11 | Token size at 4 KB / 8 KB / 8 KB+1 | warn / hard-error / reference mode |
| EC-T12 | Duplicate caveat of the same type | idempotent; the more restrictive wins |
| EC-T13 | Contradictory caveats (`amount ≤ 100` and `amount ≥ 200`) | deny everything, no crash |
| EC-T14 | Empty scope set | deny all scopes |
| EC-T15 | Scope with wildcard, if supported | explicit decision documented; default is no wildcards |
| EC-T16 | Unicode / Bengali in role names and task descriptions | handled; no encoding errors anywhere in the chain |
| EC-T17 | Attenuation adding a scope the parent lacks | `AttenuationError` at mint time |
| EC-T18 | Attenuation raising a budget ceiling | `AttenuationError` |
| EC-T19 | Attenuation extending expiry | `AttenuationError` |
| EC-T20 | Replay of a captured token from a different agent | allowed by design (bearer) — **document this**; mitigations are short TTL and optional PoP binding, listed as future work |

### 11.2 Budget & leases

| ID | Case | Expected |
|---|---|---|
| EC-B01 | Spend exactly equal to remaining | allow; remaining becomes 0 |
| EC-B02 | Spend of remaining + 0.0001 | deny (`BUDGET_EXHAUSTED_*`) |
| EC-B03 | Zero-amount spend | allow, no-op, still audited |
| EC-B04 | Negative amount | reject as `MALFORMED_REQUEST` |
| EC-B05 | Very large amount (10^18) | reject or handle without overflow; NUMERIC bounds respected |
| EC-B06 | Fractional currency (0.0001) | exact Decimal handling |
| EC-B07 | Float sneaks into the path | test fails loudly |
| EC-B08 | 3 siblings each holding the full parent ceiling, concurrent | total ≤ parent (INV-5) |
| EC-B09 | 50 concurrent acquires on a budget fitting 10 | exactly 10 granted |
| EC-B10 | PEP SIGKILLed holding a lease | budget stranded ≤ TTL, then reclaimed |
| EC-B11 | Lease expires mid-request | in-flight reservation honoured if already reserved; new reservations denied |
| EC-B12 | Commit arriving after lease expiry | rejected with a clear code; ledger consistent |
| EC-B13 | Duplicate commit, same reservation_id | no-op |
| EC-B14 | Commit with actual > estimate | top-up or escalate; never silently overspend |
| EC-B15 | Commit with actual < estimate | exact refund |
| EC-B16 | Commit never arrives (agent crashes post-reserve) | lease TTL reclaims; reservation marked abandoned |
| EC-B17 | Ledger unavailable at top-up time | PEP spends remaining lease, then fails closed |
| EC-B18 | Two PEPs, same mandate, both at low water | both top up; total never exceeds |
| EC-B19 | Mandate revoked while leases are outstanding | leases revoked; in-flight denied |
| EC-B20 | Budget dimension not in the mandate | deny with a clear code, not a KeyError |
| EC-B21 | `total = 0` mandate | every spend denied; non-spending scopes still work |
| EC-B22 | Reaper races a legitimate release | idempotent; `leased` decremented once (property-tested) |
| EC-B23 | Clock jumps backward on the ledger host | lease expiry logic unaffected (monotonic clock for intervals) |

### 11.3 Policy

| ID | Case | Expected |
|---|---|---|
| EC-P01 | Bundle signature invalid | reject, keep serving the previous bundle, alert |
| EC-P02 | Bundle version lower than current (rollback attack) | reject |
| EC-P03 | Bundle syntactically invalid Cedar | reject at upload, never reaches a PEP |
| EC-P04 | Bundle staleness exceeds max | fail closed, `POLICY_BUNDLE_STALE` |
| EC-P05 | Hot reload during in-flight requests | in-flight complete on the old bundle; no dropped requests |
| EC-P06 | Policy denies but token allows | deny (both layers must pass) |
| EC-P07 | Token denies but policy allows | deny |
| EC-P08 | No policy matches | default deny; documented |
| EC-P09 | Policy references a missing entity attribute | deny with a clear error, no crash |
| EC-P10 | Cedar and OPA disagree on a conformance case | test fails; treated as a bug in one adapter |
| EC-P11 | Compiler emits Cedar that parses but is semantically wrong | caught by the generated + human tests; activation blocked |
| EC-P12 | Compiler times out | template fallback engages |
| EC-P13 | Ambiguous English input | clarifying question returned, no guess |
| EC-P14 | Prompt injection inside the English policy text | compiler output is still schema-constrained and test-gated; **this is the key security test for the compiler** |

### 11.4 Drift

| ID | Case | Expected |
|---|---|---|
| EC-D01 | Embedding model unavailable at startup | fail closed on strict scopes; log-only scopes proceed |
| EC-D02 | Empty task description | high drift; escalate |
| EC-D03 | Task description in Bengali, action in English | handled; document the multilingual limitation honestly |
| EC-D04 | Adversarial action text crafted to mimic the task | detection likely fails — **document as a known limitation**, do not overclaim |
| EC-D05 | Score exactly at a band boundary | boundary is inclusive/exclusive per spec, tested |
| EC-D06 | Same action twice | cached, identical score |
| EC-D07 | Drift service down | log-only degrades to allow; strict degrades to escalate (never silently allow) |
| EC-D08 | Cache poisoning via `arg_digest` collision | digest is sha256; collision test on the corpus |

### 11.5 Revocation

| ID | Case | Expected |
|---|---|---|
| EC-R01 | Revoke a leaf token | only that agent fails |
| EC-R02 | Revoke a mid-tree token | that subtree fails; siblings unaffected |
| EC-R03 | Revoke the root | everything fails within 2 s |
| EC-R04 | Revoke a nonexistent id | no-op, 200, idempotent |
| EC-R05 | Revoke twice | idempotent |
| EC-R06 | PEP misses the pub/sub message | converges on the next pull |
| EC-R07 | Redis down during revocation | revocation persisted in Postgres; PEPs converge on pull; alert raised |
| EC-R08 | Bloom filter false positive | falls through to the exact set; **no false denial** |
| EC-R09 | Revocation of an already-expired token | accepted, no-op |
| EC-R10 | 10,000 revocations | Bloom sized correctly; memory bounded; latency unaffected |

### 11.6 PEP & transport

| ID | Case | Expected |
|---|---|---|
| EC-X01 | Upstream returns 500 | proxied; the decision was still allow and is audited as such |
| EC-X02 | Upstream times out | 504; reservation refunded |
| EC-X03 | Upstream streams a large response | streamed, not buffered; memory stable |
| EC-X04 | Client disconnects mid-request | reservation refunded; no leak |
| EC-X05 | Malformed JSON body | `MALFORMED_REQUEST`, 400 |
| EC-X06 | Body exceeds max size | 413 before any expensive work |
| EC-X07 | Unmapped route | default deny; configurable |
| EC-X08 | Concurrent requests on one token | isolated; budget correct |
| EC-X09 | Control plane fully unavailable | fail closed by default; fail-open only where explicitly configured, and audited (NFR-7) |
| EC-X10 | Decision-record buffer full | deny (audit loss is a compliance failure) — the choice is recorded in DECISIONS.md |
| EC-X11 | PEP restarts mid-flight | in-flight requests fail cleanly; no partial spend |
| EC-X12 | MCP `tools/list` with a narrow token | only permitted tools listed |
| EC-X13 | MCP notification / streaming | handled per spec |
| EC-X14 | HTTP/2 and chunked transfer | handled |

### 11.7 Audit & escalation

| ID | Case | Expected |
|---|---|---|
| EC-A01 | Tamper with one record body | verification reports that exact seq |
| EC-A02 | Delete a record | chain break detected at the gap |
| EC-A03 | Reorder records | detected |
| EC-A04 | Concurrent writes | seq is gap-free and monotonic (integration test at 200 writes/s) |
| EC-A05 | Custody query on an unknown action | 404 with a clear message |
| EC-A06 | Custody query on a revoked mandate | full history still returned (audit is immutable) |
| EC-A07 | Escalation approved after TTL | rejected |
| EC-A08 | Escalation approved by a non-authorized principal | rejected and audited as an attempt |
| EC-A09 | Approver narrows the request | narrowed token issued, verified ⊆ request |
| EC-A10 | Two approvers race the same escalation | first wins; second gets 409 |
| EC-A11 | Agent proceeds without waiting for approval | denied; no leak |
| EC-A12 | PII appears in `reason_detail` | test fails — reasons cite caveats, never argument values |

---

<a name="12"></a>
## 12. Adversarial / red-team test suite

`tests/security/`. Each is a named test. This suite is a slide in the pitch and a section in the paper.

### 12.1 Token attacks
- **A-01** Forge a block without the parent key → fail
- **A-02** Strip an attenuation block to widen authority → fail (biscuit blocks are chained)
- **A-03** Reorder blocks → fail
- **A-04** Splice blocks from two different tokens → fail
- **A-05** Replay an expired token → fail
- **A-06** Replay a valid token from a different agent → **succeeds by design (bearer semantics)**; documented, with PoP binding listed as future work. Being upfront about this beats being caught.
- **A-07** Algorithm-confusion / downgrade attempt → fail
- **A-08** Oversized token as a DoS vector → rejected before parsing cost is incurred
- **A-09** Deeply nested chain (depth 100) as a DoS vector → rejected at the depth check

### 12.2 Privilege escalation
- **A-10** Sub-agent requests a scope its parent lacks → denied at mint
- **A-11** Sub-agent spawns a sibling to route around its own caveat → denied (narrowing is monotonic)
- **A-12** **Confused deputy:** sub-agent tricks a higher-privileged agent into making the call for it → the higher agent's own caveats still apply; the audit chain shows who actually acted. Document the residual risk clearly.
- **A-13** Depth-limit bypass via token re-minting → denied
- **A-14** Elevation replay after TTL → denied
- **A-15** Self-approval of an escalation → denied
- **A-16** Race the policy hot-reload window to slip through → old bundle applies; both bundles deny

### 12.3 Budget attacks
- **A-17** Sibling swarm: 20 concurrent sub-agents all spending → total ≤ mandate
- **A-18** Reserve-then-never-commit to strand budget → TTL reclaims
- **A-19** Under-report `actual` to hide spend → the PEP, not the agent, determines actual where possible; where it cannot, the discrepancy is flagged and audited. **Be honest that agent-reported amounts are a trust boundary.**
- **A-20** Rapid top-up loop as ledger DoS → rate-limited per PEP
- **A-21** Negative or NaN amounts → rejected
- **A-22** Currency-unit confusion (paisa vs taka) → single canonical unit enforced at the type level

### 12.4 Prompt-injection-driven abuse (the CT-AI showpiece)
- **A-23** Injected instruction in a tool result telling the agent to exfiltrate → the action is out of scope and denied by the token
- **A-24** Injection that redirects to a different task → **drift detection escalates**; this is the demo beat
- **A-25** Injection that asks the agent to spawn a wider sub-agent → attenuation makes it impossible
- **A-26** Injection targeting the NL policy compiler → schema-constrained output plus the test gate blocks it
- **A-27** Slow-drift attack: 20 small steps, each individually plausible, cumulatively off-task → **likely evades a per-action detector**. Document honestly, and implement a trajectory-level score as future work. This limitation is itself a good paper contribution.

### 12.5 Infrastructure
- **A-28** Policy bundle rollback → version monotonicity blocks it
- **A-29** Revocation suppression by blocking pub/sub → pull backstop converges
- **A-30** Audit tampering → hash chain detects
- **A-31** Log injection via crafted role names → log fields escaped/structured
- **A-32** Timing side channel on deny reasons → constant-time-ish response shape; measured and reported (accept a small leak, document it)
- **A-33** Secret exposure in error responses → error bodies scrubbed; test asserts

**Reporting rule.** For every attack, the test records `mitigated | partially mitigated | accepted risk` with a rationale. A red-team table that honestly contains three "accepted risk" rows is far more credible than one claiming 33/33 mitigated. Judges have seen the second kind and discount it.

---

<a name="13"></a>
## 13. Performance & chaos engineering

### 13.1 Benchmarks (all published in `docs/benchmarks/`)

| ID | Measurement | Method | Target |
|---|---|---|---|
| PB-1 | Pure decision latency, warm | pytest-benchmark | p99 < 1 ms |
| PB-2 | Per-step breakdown (verify / revocation / Datalog / Cedar / lease) | instrumented | each reported individually |
| PB-3 | End-to-end proxy overhead | Locust, 500 RPS | p99 < 8 ms |
| PB-4 | Throughput per PEP instance | Locust ramp | report the knee |
| PB-5 | Latency vs token depth (1…8) | benchmark sweep | growth characterized |
| PB-6 | Cedar vs OPA decision latency | benchmark | report both |
| PB-7 | Ledger acquire throughput | Locust on ledger | report TPS + contention curve |
| PB-8 | Top-up RPS vs lease sizing policy | simulation | ≥ 60% reduction vs fixed |
| PB-9 | Revocation propagation time, 3 PEPs | integration harness | p99 < 2 s |
| PB-10 | Drift scoring latency (cached vs cold) | benchmark | reported separately |
| PB-11 | Memory under 10k revocations + 100 policies | soak | bounded |
| PB-12 | Cold start | CI timer | < 90 s |

Latency measurement discipline: HDR histograms, no averages in any reported number, coordinated-omission-aware load generation, ≥ 3 runs with variance reported. Averages in a latency table are a red flag to any infrastructure engineer on the panel.

### 13.2 Chaos scenarios (nightly; the invariant checker runs during all of them)

| ID | Scenario | Expected |
|---|---|---|
| CH-1 | Kill Postgres for 30 s | PEPs spend leases, then fail closed; recovery is clean; invariant holds |
| CH-2 | Kill Redis for 30 s | revocation falls back to pull; leases unaffected |
| CH-3 | SIGKILL one PEP of three | its lease strands ≤ TTL then reclaims; others unaffected |
| CH-4 | Partition PEP↔ledger (toxiproxy) | bounded spend, then fail closed |
| CH-5 | 500 ms latency injection on the ledger | top-ups slow, decisions unaffected (they are local) |
| CH-6 | Packet loss 10% | retries with backoff; no double-spend |
| CH-7 | Clock skew +60 s on one PEP | tolerance honoured; no spurious denials or expiries |
| CH-8 | Ollama down | template fallback; no hot-path impact |
| CH-9 | Embedding service down | strict scopes escalate, log-only allows |
| CH-10 | Rolling restart under load | zero dropped requests; invariant holds |
| CH-11 | Postgres connection-pool exhaustion | graceful 503; fail closed |
| CH-12 | Disk full on the audit ledger | requests denied (audit loss unacceptable); alert raised |

Every chaos run emits a JSON result; `docs/benchmarks/chaos-results.md` is regenerated from those. That table in the submission is worth more than any prose claim about robustness.

---

<a name="14"></a>
## 14. Evidence pack for judges

The BIIN report is explicit that documentation must read like a startup prospectus and that vaporware is heavily penalized. The evidence pack is how you prove you are not vaporware without asking anyone to take your word for anything.

### 14.1 Contents
1. Architecture: component, sequence, and deployment diagrams
2. Specs: the token format, attenuation invariants, and lease protocol documents
3. **Formal invariants table** with the property test that proves each
4. Benchmark tables PB-1…PB-12, with methodology stated
5. Chaos results CH-1…CH-12
6. Red-team results A-01…A-33, including the accepted risks
7. Drift model card: dataset construction, calibration curve, FPR, failure analysis, κ
8. Coverage + mutation-testing report
9. Audit-chain verification transcript, including a deliberate tamper detection
10. Threat model with mitigation-to-test mapping
11. IP & compliance statement
12. OSS traction: stars, forks, external users, issues closed

### 14.2 Judge-facing one-pagers
Write four, one per judge archetype:
- **Bank CTO:** fail-closed behaviour, audit chain, latency numbers, partition semantics, rollback procedure
- **Payments executive:** mandate ceilings, hard stops, reconciliation, chain of custody for a disputed transaction
- **Professor:** invariants, the lease safety argument, drift calibration, mutation testing, paper draft
- **Trade/commerce judge:** exportability (protocol, no localization), TAM, pricing, deployment model

### 14.3 The three numbers to lead with
1. **Decision latency p99** (in-process) — proves the architecture
2. **Budget invariant held across N chaos runs** — proves correctness
3. **Revocation propagation p99** — proves control

### 14.4 BIIN compliance statement (write this early, not at the deadline)
- ≥ 51% of research, design, and engineering performed in Bangladesh → 100%. Document with commit history.
- IP ownership → all core code original and Apache-2.0, sole-authored. Dependencies are permissively licensed OSS. **No proprietary black-box API anywhere in the system.** All model weights are open-weight and self-hosted.
- This is the strongest possible answer to the report's warning about teams that "merely own the interface." Make it a slide.

---

<a name="15"></a>
## 15. Demo runbook

10 minutes. Every beat is scripted, deterministic, and individually runnable. `make demo-reset` returns to a known state in under 10 seconds.

| # | Beat | Time | What the judge sees |
|---|---|---|---|
| 0 | Setup | 0:00–0:30 | Console open, identity tree empty, Grafana on a second screen |
| 1 | Human approves a task | 0:30–1:30 | "Procure 500 units, budget ৳500,000." Root token minted. Intent hash shown. |
| 2 | Delegation tree grows | 1:30–2:30 | Root spawns 3 sub-agents, animated. Each node's scopes visibly smaller. Biscuit block ids shown. |
| 3 | Least privilege enforced | 2:30–3:30 | The doc-reader attempts a payment. **Denied in 0.4 ms**, naming the exact caveat. Latency panel visible. |
| 4 | **Judge sets the ceiling** | 3:30–5:00 | Hand over the keyboard. They set ৳50,000. Agent spends up to the line across 3 concurrent sub-agents and hard-stops. **Invariant checker green throughout.** |
| 5 | **Judge writes a policy in English** | 5:00–6:30 | Their sentence → Cedar → generated tests → pass/fail table → decision diff → activate → enforced on the next call. |
| 6 | Goal drift | 6:30–7:30 | Inject an instruction redirecting the agent. Drift score crosses the band. **Escalation raised to a human, not a silent block.** |
| 7 | Revocation | 7:30–8:15 | Revoke the root. 12 agents across 3 PEPs die. Timer on screen shows the actual propagation time. |
| 8 | Chain of custody | 8:15–9:30 | "Who authorized this payment?" → full chain to the human and the permitting caveat. Verify the audit chain live. Then tamper with a record and watch verification catch it. |
| 9 | Close | 9:30–10:00 | Three numbers, the OSS repo, the APICTA export story. |

**Beats 4, 5, and 8 are the ones they will remember.** Beat 4 in particular: a judge setting a limit with their own hands and watching a system refuse to exceed it is a fundamentally different experience from watching a slide about it.

### Failure drills (rehearse all of them)
| Failure | Recovery |
|---|---|
| No internet | Everything is local. Say so — it is a selling point. |
| Ollama slow/down | Beat 5 falls back to templates; the flow is identical. |
| Postgres restarts | Fail-closed is *correct behaviour* — narrate it as a feature, then recover. |
| Judge input breaks the parser | Clarifying-question path engages. Also a feature. |
| Demo machine dies | Second laptop with the same compose stack, pre-warmed. |
| Projector resolution | Test at 1024×768; the console must be readable at that size. |

Have a 90-second screencast of the full demo on a phone as the last-resort fallback.

---

<a name="16"></a>
## 16. Timeline compression map

If you have 7 weeks instead of 12 months, cut in this order. The result is still a genuinely impressive demo — it just has less research depth.

**Must keep (the core is unrecognizable without these):**
T-001, T-002, T-003, T-004 (specs — compressed, but *written*), T-005, T-007, T-008, T-009 (+ property tests P-01/P-02/P-06), T-012, T-013, T-014, T-016, T-017, T-018, T-019, T-020, T-021, T-022, T-023, T-024, T-025, T-038, T-039, T-045, T-046, T-047, T-048, T-057, T-058.

**High value per hour, keep if at all possible:**
T-029/T-030 (compiler with the verification gate — beat 5), T-032 + a *rule-based* drift v0 (beat 6), T-053 (numbers), T-052 reduced to 4 chaos scenarios (CH-1, CH-3, CH-4, CH-10).

**Defer explicitly, and say you deferred them:**
T-034/T-035 (the trained drift model — ship rules-based v0 and present the ML version as the roadmap), T-041–T-044 (MCP + SPIRE), T-060 (paper), T-015 (adaptive sizing — use fixed leases), mutation testing.

**Compressed 7-week shape:**
- W1: T-001–T-005, T-007 (specs compressed to 1 page each but not skipped)
- W2: T-008, T-009 + property tests
- W3: T-012–T-014, T-016, T-017
- W4: T-018–T-023 (first end-to-end)
- W5: T-024, T-025, T-038, T-039, drift v0
- W6: T-045–T-048, T-029/T-030
- W7: T-053, reduced chaos, T-057, T-058, submission

**Never cut, no matter how short the timeline:** the property tests on attenuation, the invariant checker, and the demo failure drills. Those three are what separate this from a hackathon build, and they are cheap.

---

<a name="17"></a>
## 17. Risk register

| ID | Risk | L | I | Mitigation | Trigger to act |
|---|---|---|---|---|---|
| R-1 | Hyperscalers ship equivalent agent IAM before October | High | High | Compete on quantitative budgets + intent binding, not generic identity. Track AWS AgentCore / Okta / MCP spec monthly. | Any of them ships lease-style spend limits |
| R-2 | Python latency undermines the story | Med | High | Report decision latency separately from proxy overhead; optimize the hot path; if p99 decision > 2 ms by M8, port only `decision.py` to Rust via PyO3 | PB-1 misses target at M8 |
| R-3 | Lease protocol has a correctness bug found late | Med | Critical | Stateful hypothesis test from M3; invariant checker in every chaos run; spec-first | Any invariant violation in CI |
| R-4 | Drift detector has an unusable FPR | Med | Med | Escalate rather than deny; log-only default; rules-based fallback | FPR > 10% at M7 |
| R-5 | NL compiler embarrasses you on stage | Med | Med | Constrained decoding, deterministic settings, template fallback, rehearsed | Any hang during drill |
| R-6 | Scope creep (someone wants NHI discovery, or a blockchain) | High | Med | §1.4 is a contract. Point at it. | Any ticket outside §1.3 |
| R-7 | No design partner / no external validation | Med | High | Pursue one LOI from a bank, MFS, fintech, or a local AI team. Even a signed interest letter changes the commercial story | No contact by M6 |
| R-8 | Single developer: illness, burnout, or a blind spot with no second reviewer | Med | High | Every contract specced in writing before implementation; property tests on attenuation and the invariant checker act as the standing second reviewer on the two components where a silent bug is fatal; no undocumented component; milestone exit gates are hard stops, not aspirations | Any milestone gate slipping twice, or a §6 contract implemented without its spec written first |
| R-9 | Demo fails live | Med | Critical | §15 drills; second machine; screencast fallback | — |
| R-10 | Judges read it as "just a gateway" | Med | High | Lead with spend control and the custody chain, not with "we built a proxy." The word "gateway" should appear late, not early | — |
| R-11 | Token bearer semantics attacked in Q&A | Med | Med | Pre-write the answer: short TTL, revocation, PoP binding as documented future work, and note that this is the same trust model as every OAuth deployment in production today | — |
| R-12 | Free-tier compute insufficient | Low | Med | Oracle always-free ARM is genuinely adequate; university lab as backup | — |

---

<a name="18"></a>
## 18. Research & IP plan

### 18.1 Paper: contributions in priority order
1. **Attenuable delegation with quantitative mandates.** Formal semantics; the ten invariants; the observation that classic capability systems do not address quantitative resources shared across siblings, and two solutions (proportional split, shared pool).
2. **Lease-based distributed enforcement.** Protocol, safety argument, liveness bound, explicit CP choice, measured behaviour under partition, adaptive sizing results.
3. **Intent binding and calibrated drift detection.** Canonicalization, features, calibration-first evaluation, honest limitations — specifically the slow-drift evasion (A-27), which is a genuine open problem worth naming.
4. **Empirical evaluation.** Latency breakdown, chaos results, red-team results.

Venues: USENIX Security / CCS / NDSS workshops (best fit), or an SoK-style submission. Realistic for a strong final-year student with this much measured data.

### 18.2 Patent-shaped claims (document with dates and inventors from M1)
- Holder-side capability attenuation bound to a hash of a natural-language task intent
- Lease-based distributed enforcement of quantitative authorization limits for delegated autonomous agents, with adaptive lease sizing
- Intent-drift-triggered escalation of authorization for autonomous agents
- Filtering of tool discovery responses by the caller's attenuated capability set

Keep a dated invention log in `docs/` from day one. Cheap, and it matters if you ever file.

### 18.3 Defensibility
Short-term: engineering depth. Medium-term: protocol adoption via OSS. Long-term: the audit corpus and the integration surface. Be honest with yourself that the concept is copyable; the *adoption* is the moat, which is exactly why the OSS-first strategy is not optional.

---

<a name="19"></a>
## 19. Submission checklist

**Technical**
- [ ] `docker compose up` works on a clean machine (verified by someone who has never run it)
- [ ] All specs current and matching the code
- [ ] CI green; coverage ≥ 85%; 100% on the five critical files
- [ ] All property tests passing
- [ ] Benchmarks run and committed
- [ ] Chaos suite run and committed
- [ ] Red-team suite run, with accepted risks documented
- [ ] Security scans clean or waived with reasons
- [ ] Audit chain verification transcript included
- [ ] Deployment + rollback documented

**BIIN-specific**
- [ ] Architecture document with cloud topology, schemas, microservice layout, data flow
- [ ] Load-test metrics included (the report explicitly asks for this)
- [ ] TAM analysis
- [ ] Scalability roadmap
- [ ] Socio-economic impact section
- [ ] IP statement: 100% BD development, own IP, open weights, no black-box dependency
- [ ] CT-AI cross-nomination material (compiler + drift detection)
- [ ] CC-RD cross-nomination material (protocol + paper draft)
- [ ] Everything in flawless English
- [ ] Pitch structured exactly per the report: hook → live demo → architecture deep dive → business model → APICTA roadmap
- [ ] APICTA roadmap naming specific target economies (Indonesia, Vietnam, Malaysia) and why the protocol needs no localization
- [ ] No photos/video/audio during judging (per BIIN rules)

**Demo**
- [ ] All 8 beats scripted and individually runnable
- [ ] `make demo-reset` under 10 s
- [ ] All 7 failure drills rehearsed (`DEMO.md` §2)
- [ ] Second machine prepared and warm
- [ ] Screencast fallback on a phone
- [ ] Readable at 1024×768

---

<a name="20"></a>
## 20. Appendix: ticket format

Project standards — the non-negotiable rules, the Definition of Done, the five-ticket
self-review checklist, and the milestone spec-drift check — live in
`docs/ENGINEERING-RULES.md`. This appendix covers only the working format of a ticket.

### 20.1 Ticket format

Each ticket in §9 expands to this shape before work starts. Writing it out is not ceremony:
the act of restating the acceptance criteria is what surfaces spec ambiguity while it is still
cheap to fix.

```
TICKET: T-0XX — <title>

Read:        docs/PLAN.md §<sections>, docs/specs/<files>
Depends on:  T-0YY, T-0ZZ   (all committed and green)

Deliverables:
- <file>: <what>

Acceptance criteria:
- [ ] <criterion 1>
- [ ] <criterion 2>

Spec ambiguities found: <list, or "none">
Out of scope for this ticket: <specific adjacent things not to touch>
```

Then: failing tests → implementation → `ruff check . && mypy --strict . && pytest` → docs →
ADR if warranted → commit.

### 20.2 Milestone exit review

At each milestone boundary, before starting the next one:

1. Run the five-ticket self-review checklist (`ENGINEERING-RULES.md` §4) across the whole
   milestone, not just the last five tickets.
2. Run the spec-drift check (`ENGINEERING-RULES.md` §5) against §6.
3. Confirm the milestone's exit gate in `docs/ROADMAP.md` is fully green. A partially-green
   gate is a gate that has not been passed.
4. Record any deferral, with its resumption trigger, in `DECISIONS.md` and in §21.

---

<a name="21"></a>
## 21. Future work (deferred from BIIN submission)

The following items are deferred from the BIIN submission scope. They are listed in **ascending order of importance** — least important first, most important last. Each is a genuine roadmap item, not a cut corner. Stating these explicitly demonstrates engineering maturity.

| Priority | Item | Tickets | Rationale for deferral | Resumption trigger |
|---|---|---|---|---|
| 1 | Monthly spec-drift checks | §20.4 | Maintenance cadence irrelevant for sprint | Post-release, when code evolves independently of spec |
| 2 | Patent/invention log | §18.2 | Not scored at BIIN | Post-award, if filing is pursued |
| 3 | k6 as secondary load tester | §4.4 | Locust alone sufficient; two tools redundant | If k6-specific reporting is requested |
| 4 | Schemathesis API contract fuzzing | §4.4/§10.1 | Unit + integration + e2e tests cover correctness | Post-submission CI hardening |
| 5 | APICTA expansion deep-dive | §19 | One slide sufficient for BIIN; full doc is post-award | BIIN → APICTA advancement |
| 6 | Token reference / large-chain overflow | T-010 | Demo uses depth 3–4; 8 KB limit won't trigger | When production chains exceed depth 5 |
| 7 | RateLimit caveat type | §6.2 | 8 caveat types cover all demo scenarios | When rate-limiting per-scope is a production requirement |
| 8 | Full mutation testing iteration | §10.2 | One run for evidence; fixing all surviving mutants is optimization | Post-submission quality push |
| 9 | Remaining Grafana dashboards (Latency, Drift) | T-049 | 2 dashboards (Decisions + Budgets) sufficient for demo | Post-submission observability expansion |
| 10 | Remaining 7 chaos scenarios | T-052 | 5 scenarios demonstrate rigor; 12 is diminishing returns | Nightly CI after submission |
| 11 | Remaining 13 red-team attacks | T-051 | 20 attacks with 3 accepted risks is credible | Security audit phase |
| 12 | 1000 RPS load test profile | T-053 | 100/500 RPS sufficient for evidence pack | Scale testing on production hardware |
| 13 | SPIRE workload identity | T-044 | mTLS achieves the same demo-visible outcome | Enterprise deployment requiring SPIFFE SVIDs |
| 14 | OPA as second policy backend | T-024 | `PolicyEngine` protocol exists; `CedarEngine` is the production path | Enterprise customers requiring OPA |
| 15 | Adaptive lease sizing | T-015 | Fixed leases work for demo; algorithm specified in protocol doc | Production deployment with variable traffic |
| 16 | Full OSS release (CONTRIBUTING.md, verified quickstart, traction) | T-061 | Public repo + README + demo video sufficient | Community building post-award |
| 17 | MCP gateway + scope extractor | T-041, T-042 | HTTP PEP demonstrates full pipeline; MCP is the adoption path | Adoption push, post-submission |
| 18 | Trained ML drift model + dataset | T-034, T-035 | Dataset curation requires weeks of human effort; rule-based v0 provides the same demo experience | Research phase, post-submission |
| 19 | Academic paper draft | T-060 | BIIN judges score demo + technical report, not a published paper | Post-BIIN, targeting USENIX Security / CCS / NDSS workshop |

---

## Final note

Two things will decide whether this wins, and neither is the code.

**First: the demo beats where a judge's hands are on the keyboard.** Beat 4 (they set the spend ceiling and watch three concurrent agents respect it) and Beat 5 (they write a policy in English and watch it get compiled, tested, and enforced) are worth more than any architecture slide. Protect the time to polish them.

**Second: honesty about limitations.** The bearer-token replay issue, the slow-drift evasion, the stranded-lease window, the agent-reported-amount trust boundary. Every one of these is a real weakness, and every one of them, stated voluntarily with its bound and its mitigation, converts a skeptical professor into an advocate. Teams that claim 33/33 mitigated get discounted. Teams that say "30 mitigated, 3 accepted risks, here is why and here is the bound" get believed about everything else.

Build the specs first. Property-test the attenuation logic. Run the invariant checker on screen. Rehearse the failures.
