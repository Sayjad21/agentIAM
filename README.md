# AgentIAM

**Identity, authorization, and spend control for AI agents.**

AgentIAM gives every AI agent and sub-agent its own cryptographic identity carrying a strictly
narrower set of permissions and a bounded spend budget than its parent, enforced at the tool
boundary in under a millisecond, with a complete chain of custody for every action taken.

Framed plainly: **the credit limit and the chain of custody for AI agents.**

---

## Why

An agent that can call tools can spend money, read data, and send email. Today it does so
holding the same credential its operator gave it, with no way to narrow that authority when it
spawns a sub-agent, and no quantitative ceiling on what it may consume. OAuth cannot express
"this sub-agent may read invoices and spend nothing." A JWT holder cannot narrow a JWT without
going back to the issuer.

AgentIAM addresses three things nobody currently enforces correctly:

1. **Holder-side attenuation** — a parent mints a strictly narrower child token offline, with
   no issuer round-trip. Built on [biscuit](https://www.biscuitsec.org/); cryptographically
   sound and offline-verifiable.
2. **Quantitative mandates** — spend, call count, rows read, wall clock, external emails. Real
   ceilings, enforced under concurrency and network partition via a lease protocol.
3. **Chain of custody** — every action traces back to the human who approved the task and the
   exact caveat that permitted it, over a hash-chained tamper-evident ledger.

---

## Status

**Milestone 1 — Foundation, in progress.** The specification is complete. The workspace,
tooling, and CI are in place (T-001); the protocol specs and domain models are next.

Work proceeds ticket by ticket against `docs/PLAN.md`, sequenced by `docs/ROADMAP.md`.

---

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Python 3.12, and Docker.

```bash
uv sync                  # create the virtualenv and install the workspace
uv run pre-commit install

make up                  # start Postgres and Redis, wait for healthy
make check               # lint, type check, test — everything CI runs
make down
```

On Windows, `make` is unavailable; `make.ps1` mirrors every target (ADR-003):

```powershell
.\make.ps1 up
.\make.ps1 check
```

`make help` lists the rest.

### Layout

```
packages/
  agentiam-core/          pure domain logic — zero I/O, the correctness core
  agentiam-sdk/           what agent developers import
  agentiam-pep/           the enforcement point (hot path)
  agentiam-controlplane/  issuance, ledger, policy, revocation, audit, console
  agentiam-demo/          reference procurement agent, stub tools, demo scenarios
tests/                    unit · property · integration · e2e · security · chaos · perf
docs/                     specification, roadmap, engineering rules, ADRs
```

`agentiam-core` is I/O-free by contract, enforced statically on every CI run by
[`tests/unit/test_core_purity.py`](tests/unit/test_core_purity.py). Every correctness claim
this project makes is a claim about that package.

---

## Documentation

| Document | What it governs |
|---|---|
| [`docs/PLAN.md`](docs/PLAN.md) | Authoritative on **what** to build — architecture, protocol specs, data model, API contracts, the full ticket list, testing strategy |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Milestone sequencing, exit gates, scope decisions |
| [`docs/ENGINEERING-RULES.md`](docs/ENGINEERING-RULES.md) | Authoritative on **how** to build — non-negotiable rules, Definition of Done, review cadence |
| [`docs/DEMO.md`](docs/DEMO.md) | Demo runbook, failure drills, judge handling |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Append-only ADR log |
| `docs/specs/` | Protocol specifications — written before the code that implements them |

---

## Stack

Python 3.12 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 16 ·
Redis 7 · biscuit-python · cedarpy · Ollama (Qwen2.5-7B, open weights, self-hosted) ·
pytest · hypothesis · testcontainers · ruff · mypy --strict · OpenTelemetry · Grafana

**Every dependency is free and open source.** There is no paid service and no proprietary
black-box API anywhere in this system, and all model weights are open-weight and self-hosted.
That is a design constraint, not a budget compromise.

---

## Design commitments

- **The hot path never blocks on the network.** Verification, policy evaluation, revocation
  checks, and budget reservation are all local. Leases and revocation lists propagate
  asynchronously.
- **Fail closed.** No lease, stale policy bundle, unreachable control plane → deny. Fail-open
  is per-scope, opt-in, and audited.
- **Tokens are immutable.** Elevation issues a new token. Narrowing creates a new token.
- **Every deny is explainable.** A decision record names the exact caveat, policy statement, or
  budget that caused it. A deny without a reason is a bug.
- **The ledger is the only authority on budget.** Enforcement points hold leases, not truth.

---

## Known limitations

Stated up front, because they are real:

- **Bearer semantics.** A stolen token is usable until it expires or is revoked. Mitigated by
  short TTLs and fast revocation; proof-of-possession binding is documented future work. This
  is the same trust model as every OAuth deployment in production today.
- **Stranded lease window.** A hard-killed enforcement point strands its lease until TTL
  expiry. Bounded, measured, and reclaimed by the reaper.
- **Slow-drift evasion.** An adversary who drifts intent gradually enough can stay under the
  detection threshold. A genuine open problem, named rather than hidden.
- **Agent-reported amounts.** Where the enforcement point cannot independently determine the
  actual spend, it relies on the agent's report. The discrepancy is flagged and audited; the
  trust boundary is real.

---

## Licence

Apache-2.0.
