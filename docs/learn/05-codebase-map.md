# 05 — Map of the code

Where everything lives, and why the boundaries are drawn where they are. Read this once and
you will be able to find anything.

---

## 1. Five packages

The repository is a `uv` workspace with five installable packages under `packages/`.

```
agentiam-core          the rules, with no I/O at all
agentiam-pep           the enforcement point (the hot path)
agentiam-controlplane  the central service, the ledger, the console
agentiam-sdk           what an agent developer imports
agentiam-demo          the stub tools the demo calls
```

Plus `scripts/` for the runnable entry points, `tests/` for eight kinds of test, and `docs/`.

---

## 2. `agentiam-core` — the rules

**The most important boundary in the project.** This package contains every correctness claim
the system makes: the attenuation invariants, the budget arithmetic, the decision pipeline.

And it is **forbidden from doing any I/O whatsoever**. No network, no database, no filesystem,
no subprocess — and no reading the clock. The clock is passed in.

| Module | What it holds |
|---|---|
| `models.py` | Mandate, Budget, RequestContext, the validated types |
| `tokens.py` | Minting and verifying biscuits |
| `attenuation.py` | Creating a narrower child token |
| `caveats.py` | The caveat types and their compilation to Datalog |
| `decision.py` | `decide()` — steps 3 to 7, pure |
| `datalog.py` | Reading a token's rendered rules back |
| `hashing.py` | Canonical JSON, the one definition of an intent hash |
| `bundles.py` | Signing and verifying policy bundles |
| `escalation.py` | The elevation state machine |
| `drift_features.py` | The drift feature arithmetic |
| `corpus.py` | The 51-case policy test corpus |

### Why the rule is absolute

If `decide()` could reach a database, then "this function returns deny for these inputs" would
stop being a statement you can test, and start being a statement about the state of a database
at a moment in time. Every property test in the project depends on core being deterministic.

The rule is enforced **statically**, by a test that walks the AST of every source file rather
than importing the package — a runtime check would miss a lazily imported violation inside a
function body, which is exactly where one would realistically appear. There is a dedicated CI
job called `purity` that runs it.

To add a forbidden import you must write an architecture decision record. Not a quiet edit.

---

## 3. `agentiam-pep` — the enforcement point

Everything that happens on the hot path.

| Module | What it holds |
|---|---|
| `pipeline.py` | The ten steps wired together — the request path itself |
| `extractor.py` | Step 1: HTTP request → `RequestContext` |
| `policy.py` | The Cedar engine and the tool catalogue |
| `policy_cache.py` | Bundle loading, staleness, rollback protection |
| `pool.py` | The lease pool — acquiring, spending, renewing |
| `lease.py` | The local lease arithmetic |
| `settlement.py` | Telling the ledger what was actually spent |
| `emitter.py` | Buffering and writing decision records |
| `revocation.py` | The local revocation set, Redis push plus pull backstop |
| `drift.py` | The drift oracle client |
| `app.py` | The FastAPI application and proxy |

Note what is **not** here: the database. The PEP talks to the ledger through structural
`Protocol` types, so `agentiam-pep` never imports `agentiam-controlplane`. They are genuinely
independent deployables.

---

## 4. `agentiam-controlplane` — the centre

| Module | What it holds |
|---|---|
| `db/` | SQLAlchemy models, migrations, the ledger operations |
| `decisions_api.py` | `/v1/decisions` and the live stream |
| `audit_api.py` | `/v1/audit` — search, custody, chain verification |
| `budgets_api.py` | `/v1/budgets/dashboard` |
| `tree_api.py` | `/v1/tree/{task_id}` — the identity tree |
| `escalations_api.py` | Opening, approving and denying escalations |
| `revocations_api.py` | Issuing revocations and serving the pull feed |
| `auth.py` | OIDC login against Keycloak |
| `console/` | The HTML templates for every screen |
| `nl_compiler/` | Natural language → Cedar |

---

## 5. `agentiam-sdk` — what an agent imports

Small on purpose.

```python
from agentiam_sdk import AgentIAM

client = AgentIAM(token=my_token, root_keys=keys)

# spawn a narrower child — local, offline, microseconds
child = client.attenuate(scopes={"invoice:read"}, spend_bdt=Decimal("0"))

# the headers to send with a tool call
headers = client.headers(action_intent="reading invoice inv_001")
```

It also offers `@requires_scope(...)`, a decorator that fails fast in the agent's own process
rather than waiting for the PEP to refuse — a developer-experience convenience, never a
security control. The PEP is the security control.

---

## 6. `scripts/` — the runnable things

| Script | What it does |
|---|---|
| `pep_service.py` | **The deployed PEP.** The composition root — assembles everything from environment variables |
| `serve_pep.py` | The load-test harness's PEP. Must do the same per-request work as the real one |
| `serve_tools.py` | The stub tools the demo calls |
| `seed_demo.py` | Creates the demo mandate, mints the six tokens, drives the scenario |
| `bootstrap_demo_secrets.py` | Generates the root keypair and the signed policy bundle |
| `verify_audit_chain.py` | Re-hashes the audit chain and reports the first bad record |
| `run_invariant_checker.py` | Asserts the budget invariants against the live ledger |
| `generate_evidence_pack.py` | Builds the submission evidence bundle |

> **Why is the deployed PEP in `scripts/` and not in the package?** Because assembling it needs
> the ledger, the audit sink and the settlement sink — all of which live in the control plane.
> The `agentiam-pep` package deliberately never imports `agentiam-controlplane`. Declaring that
> dependency to move this file into the package would invert the architecture. A composition
> root is the one place allowed to know about both, so it sits at the repository layer.

---

## 7. How the layers stack

```
        agentiam-sdk          agentiam-demo
             │                      │
             ▼                      ▼
    ┌──────────────────┐    ┌──────────────┐
    │   agentiam-pep   │───▶│  the tools   │
    └──────────────────┘    └──────────────┘
             │
             │ Protocol types only — no import
             ▼
    ┌──────────────────────────┐
    │  agentiam-controlplane   │
    └──────────────────────────┘
             │
             ▼
        Postgres, Redis

    everything above depends on ▼
    ┌──────────────────┐
    │  agentiam-core   │   no I/O, no clock
    └──────────────────┘
```

---

## 8. Finding your way around

**Reading the code for the first time?** Start at `packages/agentiam-core/decision.py` and read
`decide()` top to bottom. It is the whole authorization model in one function, with the step
numbers from [file 04](04-request-lifecycle.md) as comments.

**Want to know why a line is the way it is?** The comments in this codebase are unusually long
and they explain *why*, often naming the measurement or the bug that produced them. They are
the single best source in the repository. When one references an ADR number, look it up in
[`docs/DECISIONS.md`](../DECISIONS.md).

**Docstrings carry the reasoning too.** They are not "returns a string" filler — most of them
name what would break if the function behaved differently.

---

## Next

Part 2 begins with [06 — Tokens and identity](06-tokens.md).
