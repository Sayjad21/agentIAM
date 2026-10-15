# Final Rush — completing AgentIAM

**Written 2026-10-16.** The plan that takes AgentIAM from "a strong core with a console that
looks finished" to a system where every page, every claim and every number is true on the
deployed stack. It continues `docs/PLAN.md` §9; ticket ids pick up at **T-062**, and the
deferred tickets keep their original ids (T-015, T-031, T-034, T-035, T-041, T-042, T-044,
T-057…T-061).

Read this file first, then the workstream file that holds your ticket.

---

## 1. Why this plan exists

The core is real: tokens, attenuation, the ledger, Cedar, the audit chain, revocation, and
2,600+ passing tests. But an audit of the running system on 2026-10-16 found that several
things the console and the docs present as working are not wired to anything:

| What the console or docs say | What is actually true | Ticket |
|---|---|---|
| "Policy Activated Successfully!" — enforced on the next call | `/policy/activate` reassigns `DummyBundleStore.current_source` in the console process. The PEP read its bundle from disk at boot and never sees the change | T-070…T-073, T-099 |
| A judge sets a budget ceiling for a new task | Every task reuses one hard-coded `DEMO_MANDATE_ID`, because a PEP serves exactly one mandate (gap 25) | T-074…T-078 |
| An approved escalation elevates the agent | Approval mints a root token and returns it to the *approver* in the HTTP response. Nothing delivers it to the agent that asked | T-081 |
| Agents act; the console watches | No AI agent exists. All traffic comes from `scripts/seed_demo.py` and `scripts/demo_concurrency.py` | T-085…T-088 |
| "One URL change to install" (MCP gateway) | T-041/T-042 deferred; no MCP code anywhere | T-089, T-041, T-042 |
| Drift detection | Rule-based v0; f3/f4/f6 deferred; NFR-9 never measured; a hung Ollama stalls the event loop (gap 22) | T-092…T-096, T-034, T-035 |
| "mTLS in place; SPIFFE deferred" (`PLAN.md` T-044) | No TLS between any services | T-109, T-044 |
| "Periodic checkpoints signed by the audit service key" (`PLAN.md` §6.8) | No checkpoints, no signatures | T-108 |
| "Full OpenAPI is generated and committed to `docs/openapi/`" (`PLAN.md` §8) | The directory does not exist | T-123 |
| "Model-checked" (spec 04 header) | 400 random interleavings in a scratchpad script that was never committed | T-100, T-101 |
| Revoking an agent from the console | The identity tree shows revocations; it cannot make one | T-083 |
| Roles are attested | Static JSON map read at boot (gap 28) | T-079 |

`research/gap-analysis.md` and `research/threat-model.md` §6 list the rest. Every one of
those is either a ticket here or is explicitly re-deferred with a reason.

## 2. What "complete" means

The project is complete when all of the following are true, each checked by a test or a
script rather than by reading:

1. **Every console page is backed by a real backend path.** No in-memory stand-ins
   (`DummyBundleStore`, process-local `TaskRegistry`) on any path an operator uses.
2. **Every operator action an IAM product needs exists**: issue a mandate, set and adjust a
   budget, manage agent roles, publish and roll back policy, approve or deny an escalation
   with the agent receiving the result, revoke a token or subtree.
3. **A real AI agent runs through the PEP**, over plain HTTP and over MCP, and the demo shows
   it — not a script.
4. **Every claim in `README.md`, `docs/DEMO.md` and `docs/PLAN.md` holds on the deployed
   stack**, or is reworded to what holds.
5. **Every deferred ticket is done or re-deferred with a measured reason**, recorded in
   `docs/DECISIONS.md` and `PLAN.md` §21.
6. **Every published number is reproducible** from a `make` target.

## 3. Rules for whoever picks up a ticket

**One ticket per sitting.** Never work several tickets in one change, even when they look
small. Each ticket ends with a review before the next one starts.

**Picking the next ticket.** Unless a specific ticket was named, take the first ticket in §5's
order whose status in §8 is `todo` and whose dependencies are all `done`. After §5 is exhausted,
follow §6's phase order, then §8's order within a phase. Mark it `in progress` in §8 when you
start, and `done` only after review and a green CI run.

`docs/ENGINEERING-RULES.md` governs. In short, per ticket:

```
read this README, then the ticket, then every file under "Read"
  → restate the acceptance criteria; write down any spec ambiguity
  → do the "Probe first" step against the running system and record what you found
  → write the tests first and confirm they FAIL
  → implement
  → make check            (lint, format, mypy --strict, unit tests)
  → make test-integration (anything touching agentiam-controlplane or agentiam-pep)
  → make test-e2e / make chaos when the ticket says so
  → update the spec / PLAN / STATUS if a contract or a claim changed
  → append an ADR to docs/DECISIONS.md for any non-obvious choice or new dependency
  → tick the ticket in §8 below and in docs/STATUS.md's ledger
  → commit "T-0XX: <imperative summary>", push, confirm the CI run is green
```

Non-negotiables that bite most often in this plan:

- **Spec first.** Tickets marked *spec* produce a reviewed spec section before any code.
  The tickets after them assume that spec exists.
- **Probe first.** Every ticket has a "Probe first" step. Do it and write the result into
  the commit message or the ADR. This project has found more than ten design errors this
  way, two of them in its own specs.
- **A guard never seen to fire is not a guard.** Every new check gets a test that removes or
  bypasses it and watches the test fail.
- **New dependency → ADR** (rule 7). Several tickets add one; each says so.
- **No paid service** (rule 8). Hosted LLMs stay optional and free-tier; the default path is
  local.
- **Never log user-supplied natural language verbatim** — `tests/security/test_secret_scanning.py`
  enforces it. Log a `sha256[:16]` digest and the length.
- **Confirm CI.** A ticket is not done until its pushed run is green.

## 4. Calendar

| Window | Dates | What happens |
|---|---|---|
| **W1** | 17–31 Oct 2026 | The Jakarta-critical path (§5). Humans review and merge daily |
| Exams | 1–30 Nov | No human work. Optionally, long unattended jobs only: drift dataset generation (T-034), the TLA+ model check (T-100), chaos runs. Nothing that needs review lands on `main` |
| **W2** | 1–6 Dec | **Code freeze.** Bug fixes that break the demo, failure drills (T-058), rehearsals (H-04). No features |
| APICTA | 7–11 Dec | Jakarta |
| **W3** | 14 Dec onward | Everything else, in phase order (§6) |

## 5. The Jakarta-critical path (W1)

Twenty tickets, in this order. Each one makes something the panel will see real, or removes a
claim that is false. If W1 throughput is lower than planned, **cut from the bottom** — the top
half alone makes the demo honest.

| # | Ticket | Why it is on the critical path |
|---|---|---|
| 1 | T-062 | Confirm the demo-concurrency work (`dca459f`) is green in CI and measured |
| 2 | T-063 | Stop saying false things (README status, "open source", "model-checked", "mTLS in place") |
| 3 | T-064 | The assumption the whole design rests on gets a CI test |
| 4 | T-092 | A hung model must not freeze enforcement on stage |
| 5 | T-070 | Spec for real policy publication |
| 6 | T-071 | Signed, persisted bundles in the control plane |
| 7 | T-072 | The PEP actually receives them |
| 8 | T-099 | English sentence → enforced on the next PEP call, end to end |
| 9 | T-031 | The compiler survives no internet and no model |
| 10 | T-097 | Pick the local model that fits a 6 GB laptop GPU, measured |
| 11 | T-085 | SDK surface from `PLAN.md` §8, which the agent needs |
| 12 | T-086 | A real LLM agent doing a procurement task through the PEP |
| 13 | T-087 | The poisoned-invoice attack and five others, deterministic and live |
| 14 | T-081 | The agent actually receives an approved elevation |
| 15 | T-083 | Revoke from the console, with the propagation time shown |
| 16 | T-073 | The policy page shows real bundle history |
| 17 | T-057 | The 10-minute demo rebuilt around the real agent |
| 18 | T-058 | Every failure drill is a script, not a hope |
| 19 | T-059 | The written nomination and submission package |
| 20 | T-125 | Slides, speaker script, judge one-pagers |

Not on the W1 path, deliberately: multi-mandate PEPs (T-076/T-077) touch the money hot path
and need careful review; the demo works with a per-task ceiling enforced in the token
(`tasks.py` measured this). They are the first thing in W3.

## 6. Phases after Jakarta (W3)

| Phase | File | Theme |
|---|---|---|
| A | [`02-control-plane.md`](02-control-plane.md) | Multi-mandate PEPs, issuance, roles, budgets, RBAC — the product an operator uses |
| B | [`06-lease-and-research.md`](06-lease-and-research.md) | Make the lease contribution unattackable: TLA+, skew check, restricted role, baselines |
| C | [`03-agents-and-mcp.md`](03-agents-and-mcp.md) | MCP gateway, framework adapters, APort Vault |
| D | [`04-drift.md`](04-drift.md) | Dataset, classifier, trajectory scoring |
| E | [`07-security.md`](07-security.md) | TLS, proof-of-possession, key rotation, red-team completion, multi-tenancy |
| F | [`08-operations.md`](08-operations.md) | NFR-2 off-box, dashboards, OpenAPI, release |
| G | [`09-demo-and-submission.md`](09-demo-and-submission.md) | OSS release (T-061), paper (T-060 lives in file 06) |

Workstream files 00, 01 and 05 are mostly on the W1 path and should be finished first.

## 7. Working in parallel

Tickets in different lanes rarely touch the same files and can run side by side. Tickets in
the same lane should run one after another.

| Lane | Main files touched | Tickets |
|---|---|---|
| Docs and tests | `docs/`, `tests/unit/`, `pyproject.toml` | T-062…T-069, T-123 |
| Policy | `agentiam_core/bundles.py`, controlplane policy API, `agentiam_pep/policy_cache.py` | T-070…T-073, T-099, T-111, T-116 |
| Ledger and leases | `db/ledger.py`, `agentiam_pep/pool.py`, `lease.py`, `settlement.py` | T-076, T-077, T-080, T-102, T-103, T-106, T-015, T-107 |
| Control plane API and console | `app.py`, `*_api.py`, `console/` | T-074, T-075, T-078, T-079, T-081…T-084 |
| Agents and SDK | `agentiam-sdk`, new `agentiam-agents`, `agentiam-demo` | T-085…T-088, T-090, T-091 |
| MCP | new `agentiam_pep/mcp/` | T-089, T-041, T-042 |
| Drift | `agentiam_pep/drift.py`, `agentiam_core/drift_features.py`, new `research/drift/` | T-092…T-096, T-034, T-035 |
| Compiler | `nl_compiler/` | T-031, T-097, T-098 |
| Research | new `models/`, `scripts/sim/`, `tests/chaos/` | T-100, T-101, T-104, T-105, T-108 |
| Infra | `deploy/`, compose files, `.github/` | T-109, T-044, T-119…T-122, T-124 |

## 8. Ticket index

Size: **S** = one sitting, **M** = two, **L** = three (the maximum; anything larger is split).
GPU: **6** = needs the 6 GB laptop GPU, **4** = runs on the 4 GB one, **—** = no GPU.
**J** = on the Jakarta-critical path.

| Ticket | Title | File | Size | Depends on | GPU | J | Status |
|---|---|---|---|---|---|---|---|
| T-062 | Confirm and measure the demo-concurrency work (`dca459f`) | 00 | S | — | — | J | todo |
| T-063 | Reconcile every public claim with the running system | 00 | M | T-062 | — | J | todo |
| T-064 | Pin assumption A1 with a CI test | 00 | S | — | — | J | todo |
| T-065 | Gate coverage in CI | 00 | S | — | — | | todo |
| T-066 | Property-test `to_datalog` / `evaluate` agreement | 00 | M | — | — | | todo |
| T-067 | Mutation testing on attenuation and caveats | 00 | M | T-066 | — | | todo |
| T-068 | Consolidate test fixtures; clear deprecations | 00 | M | — | — | | todo |
| T-069 | Randomize test order; chase the order-dependent failures | 00 | S | T-068 | — | | todo |
| T-070 | *Spec*: policy bundle publication and distribution | 01 | S | — | — | J | todo |
| T-071 | Persisted, signed bundle store and policy API | 01 | L | T-070 | — | J | todo |
| T-072 | The PEP syncs bundles; `POLICY_BUNDLE_STALE` becomes reachable | 01 | M | T-071 | — | J | todo |
| T-073 | Console policy page on real bundles: history, diff, rollback, provenance | 01 | M | T-071 | — | J | todo |
| T-074 | *Spec*: mandate issuance and persistence | 02 | S | — | — | | todo |
| T-075 | Mandates table and issuance API | 02 | L | T-074 | — | | todo |
| T-076 | Multi-mandate PEP: lease-pool registry | 02 | L | T-075 | — | | todo |
| T-077 | Multi-mandate PEP: settlement, release, shutdown, metrics per mandate | 02 | M | T-076 | — | | todo |
| T-078 | Console mandate issuance and task detail page | 02 | M | T-075, T-076 | — | | todo |
| T-079 | Agent registry and role management | 02 | L | T-075 | — | | todo |
| T-080 | Audited budget administration | 02 | M | T-075 | — | | todo |
| T-081 | Escalation round-trip to the requesting agent | 02 | L | T-085 | — | J | todo |
| T-082 | Console RBAC, CSRF protection, operator-action audit | 02 | M | — | — | | todo |
| T-083 | Revoke from the console | 02 | S | — | — | J | todo |
| T-084 | Console UX and accessibility pass | 02 | M | T-078, T-083 | — | | todo |
| T-085 | SDK v1: the `PLAN.md` §8 surface | 03 | L | — | — | J | todo |
| T-086 | Reference agent: a real LLM doing procurement through the PEP | 03 | L | T-085, T-097 | 6 | J | todo |
| T-087 | Attack scenario library: deterministic and live | 03 | M | T-086 | 6 | J | todo |
| T-088 | Agent framework adapters | 03 | L | T-085 | 4 | | todo |
| T-089 | *Spec*: MCP gateway (spec 12) | 03 | S | — | — | | todo |
| T-041 | MCP streamable-HTTP gateway | 03 | L | T-089 | — | | todo |
| T-042 | MCP scope extractor against three real MCP servers | 03 | M | T-041 | — | | todo |
| T-090 | TypeScript SDK | 03 | L | T-085 | — | | todo |
| T-091 | APort Vault benchmark run, plus a budget track | 03 | L | T-086 | 6 | | todo |
| T-092 | Drift off the event loop (gap 22) and CH-9 | 04 | M | — | 4 | J | todo |
| T-093 | Drift features f3, f4, f6 | 04 | M | T-092 | 4 | | todo |
| T-034 | Drift dataset pipeline (synthetic + LLM-judge labels) | 04 | L | T-093 | 6 | | todo |
| T-094 | Gold-set labelling tool and agreement report | 04 | S | T-034 | — | | todo |
| T-035 | Calibrated drift classifier and model card | 04 | L | T-034, H-01 | — | | todo |
| T-095 | Trajectory-level drift scoring (slow drift, A-27) | 04 | M | T-035 | — | | todo |
| T-096 | Classifier in the PEP oracle | 04 | M | T-035 | — | | todo |
| T-031 | Template fallback for the NL compiler | 05 | M | — | — | J | todo |
| T-097 | Local model bake-off for 6 GB GPUs, with constrained decoding | 05 | M | — | 6 | J | todo |
| T-098 | Compiler dataset 30 → 150 with a held-out split | 05 | M | T-097 | 6 | | todo |
| T-099 | English sentence enforced on the next PEP call, end to end | 05 | M | T-072, T-031 | — | J | todo |
| T-100 | TLA+ model of the lease protocol, two clocks | 06 | L | — | — | | todo |
| T-101 | Committed lease simulator and guard-ablation matrix | 06 | M | — | — | | todo |
| T-102 | Clock-skew check at `acquire()` and CH-7 | 06 | M | T-100 | — | | todo |
| T-103 | Restricted Postgres role for the PEP | 06 | M | — | — | | todo |
| T-104 | Remaining chaos scenarios CH-2, 5, 6, 11, 12 | 06 | L | — | — | | todo |
| T-105 | Baselines B0–B3 through the chaos harness | 06 | L | T-104 | — | | todo |
| T-106 | A partitioned PEP shuts down within a bound (gap 21) | 06 | M | — | — | | todo |
| T-015 | Adaptive lease sizing | 06 | M | T-102 | — | | todo |
| T-107 | Ledger service: the PEP holds no database credentials | 06 | L | T-103, T-077 | — | | todo |
| T-108 | Signed audit checkpoints and external anchoring | 06 | M | — | — | | todo |
| T-060 | Paper draft | 06 | L | T-100…T-105 | — | | todo |
| T-109 | TLS and mTLS between all services | 07 | M | — | — | | todo |
| T-044 | SPIRE workload identity on k3s | 07 | L | T-109 | — | | todo |
| T-110 | Proof-of-possession tokens (TM-01) | 07 | L | — | — | | todo |
| T-111 | `RateLimit` caveat | 07 | M | — | — | | todo |
| T-112 | Per-scope opt-in fail-open (NFR-7) | 07 | S | — | — | | todo |
| T-113 | Root key rotation and key custody | 07 | M | — | — | | todo |
| T-114 | Remaining red-team attacks A-14…A-33 | 07 | L | — | — | | todo |
| T-115 | API contract fuzzing | 07 | S | T-123 | — | | todo |
| T-116 | OPA as a second policy backend | 07 | M | T-072 | — | | todo |
| T-117 | *Spec*: multi-tenancy | 07 | S | T-075, T-079 | — | | todo |
| T-118 | Multi-tenancy implementation | 07 | L | T-117 | — | | todo |
| T-119 | NFR-2 measured off-box on two machines | 08 | M | — | — | | todo |
| T-120 | 1,000 RPS profile and horizontal scaling | 08 | M | T-119, T-077 | — | | todo |
| T-121 | Latency and drift dashboards, alert rules | 08 | S | — | — | | todo |
| T-122 | Cut v1.0.0 and exercise the release pipeline | 08 | S | T-063 | — | | todo |
| T-123 | Generate and commit OpenAPI | 08 | S | — | — | | todo |
| T-124 | CI on Windows | 08 | S | — | — | | todo |
| T-057 | Demo rebuilt around the real agent | 09 | L | T-086, T-087, T-099 | 6 | J | todo |
| T-058 | Failure drills as scripts | 09 | M | T-057 | — | J | todo |
| T-059 | Written nomination and submission package | 09 | M | T-063 | — | J | todo |
| T-125 | Slides, speaker script, judge one-pagers | 09 | M | T-059 | — | J | todo |
| T-061 | Open-source release readiness | 09 | M | T-063, T-122 | — | | todo |

## 9. Human-only tasks

These cannot be delegated. Each blocks something above.

| Id | Task | When | Blocks |
|---|---|---|---|
| H-01 | Hand-label the ~200-pair drift gold set (two people, independently, for agreement) | Before 1 Nov, or W3 | T-035 |
| H-02 | Talk to 3–5 security or platform leads at banks, mobile-money providers or AI teams; ask for one letter of interest | W1 | T-059, T-125 |
| H-03 | Agree the team's truthful answer to "how did you build this, and with what tools?" | W1 | T-125 |
| H-04 | Timed rehearsals and two mock panels with outside judges | W1 + W2 | Jakarta |
| H-05 | Confirm with the BIIN coordinator: Technical Solutions sub-category, nomination deadline and format, whether a fallback video is allowed in the judging room | This week | T-059 |
| H-06 | Decide whether the repository goes public before Jakarta | This week | T-063, T-061 |
| H-07 | Review and merge each landed ticket | Ongoing | Everything |

## 10. Hardware notes

- **6 GB GPU (RTX 3060 Laptop)**: `qwen2.5:7b-instruct-q4_0` was measured at 5.32 GB VRAM on
  the development machine. It fits, barely, with nothing else on the GPU. Close the browser's
  hardware acceleration during model runs.
- **4 GB GPU (RTX 3050 Laptop)**: 3B-class models (e.g. `qwen2.5:3b`, `llama3.2:3b`) and the
  embedding model. Not the 7B compiler model.
- **Two laptops** make T-119's off-box load test possible: load generator on one, PEP stack
  on the other, wired Ethernet or a dedicated router — never venue or campus Wi-Fi.
- Long unattended jobs (T-034 generation, T-100 TLC, `make chaos`) write plain logs to a file.
  Do not pipe them through a filter that buffers.
