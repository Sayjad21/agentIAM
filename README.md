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

**The core is built; the demo and submission are in progress.** Tokens, attenuation, the
lease ledger, Cedar policy, the PEP, revocation, escalation, the audit chain, the console,
observability, and the red-team, chaos, load and security suites are done — 48 of 52 in-scope
tickets, ten specs, 73 ADRs, ~3,040 tests. What is left, and what is presented as working but
is not yet wired, is in [`docs/STATUS.md`](docs/STATUS.md). The one-page overview is
[`docs/MASTER.md`](docs/MASTER.md).

Work proceeds ticket by ticket against `docs/PLAN.md`, sequenced by `docs/ROADMAP.md`.

---

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Python 3.12, and Docker.

```bash
uv sync                  # create the virtualenv and install the workspace
uv run pre-commit install

make up                  # start Postgres, Redis and Keycloak, wait for healthy
make check               # lint, type check, test — everything CI runs
make down
```

On Windows, `make` is unavailable; `make.ps1` mirrors every target (ADR-003):

```powershell
.\make.ps1 up
.\make.ps1 check
```

To run the whole system — control plane, console, PEP and stub tools — and drive the demo
scenario through it:

```bash
make demo-up             # build and start everything, wait for healthy (~20 s)
make demo-seed           # send the demo traffic; then open http://localhost:8000
```

[`docs/RUNNING.md`](docs/RUNNING.md) walks every surface. `make help` lists the rest.

### Layout

```
packages/
  agentiam-core/          pure domain logic — zero I/O, the correctness core
  agentiam-sdk/           what agent developers import
  agentiam-pep/           the enforcement point (hot path)
  agentiam-controlplane/  issuance, ledger, policy, revocation, audit, console
  agentiam-demo/          stub upstream tools for the demo (no AI agent yet)
tests/                    unit · property · integration · e2e · security · chaos · perf
docs/                     plan, specs, status, ADRs, journal, runbooks
research/                 related work, gap analysis, the lease research plan
```

`agentiam-core` is I/O-free by contract, enforced statically on every CI run by
[`tests/unit/test_core_purity.py`](tests/unit/test_core_purity.py). Every correctness claim
this project makes is a claim about that package.

---

## Documentation

| Document | What it is for |
|---|---|
| [`docs/MASTER.md`](docs/MASTER.md) | The whole picture on one page |
| [`docs/STATUS.md`](docs/STATUS.md) | What is built, what is left, what is known to be wrong |
| [`docs/PLAN.md`](docs/PLAN.md) | Authoritative on **what** to build |
| [`docs/ENGINEERING-RULES.md`](docs/ENGINEERING-RULES.md) | Authoritative on **how** to build |
| [`docs/README.md`](docs/README.md) | The index of every other document |

---

## Stack

Python 3.12 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 16 ·
Redis 7 · Keycloak (OIDC) · biscuit-python · cedarpy · Ollama · pytest · hypothesis ·
testcontainers · ruff · mypy --strict · OpenTelemetry · Grafana

**Every dependency is free and open source, and nothing on the enforcement path calls out.**
One exception, stated rather than hidden: the natural-language policy compiler uses hosted
inference (Gemini or Groq, free tier) when an API key is configured, because that is what made
it score 27/30 (ADR-040). Without a key it runs on a local open-weight model through Ollama,
and `AGENTIAM_LLM_BACKEND=ollama` forces that. The compiler only drafts policy; nothing it
produces is enforced until it passes the test corpus and an operator activates it.

---

## Design commitments

- **The hot path never blocks on the network.** Verification, policy evaluation, revocation
  checks, and budget reservation are all local. Leases and revocation lists propagate
  asynchronously.
- **Fail closed.** No lease, stale policy bundle, unreachable control plane → deny. There is no
  fail-open mode for authorization; only the advisory drift score fails open when its model
  is unreachable.
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
  expiry. Bounded, measured, and reclaimed by the control plane's reaper.
- **Slow-drift evasion.** An adversary who drifts intent gradually enough can stay under the
  detection threshold. A genuine open problem, named rather than hidden.
- **Agent-reported amounts.** Where the enforcement point cannot independently determine the
  actual spend, it relies on the agent's report. The discrepancy is flagged and audited; the
  trust boundary is real.

---

## Licence

Apache-2.0.
