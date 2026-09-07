# Manual end-to-end test report

A hand-driven pass over the running system: the control plane, a real PEP, and the stub
tools, exercised over HTTP the way an agent and an operator would use them. Every case
below records **what was sent**, **what should have come back** (per `PLAN.md`, the specs,
and `DEMO.md`), and **what actually came back**.

**Date:** 2026-09-07 · **Commit under test:** `003a06a` plus the console rework
**Result:** 42 cases across 6 areas. **3 defects found, 1 fixed.** One of them makes the
system's headline guarantee — holder-side attenuation — unenforced in the deployed PEP.

---

## 1. What was running

Nothing was mocked. Real biscuits, real Ed25519 keys, real Postgres, real Cedar.

| Component | How it was started | Port |
|---|---|---|
| Postgres 16 / Redis 7 / Keycloak | `docker compose up -d --wait` | 5433 / 6379 / 8085 |
| Control plane | `uvicorn agentiam_controlplane.app:create_app_from_env --factory` | 8000 |
| Stub tools | `python scripts/serve_tools.py` | 8081 |
| **PEP** | **`python scripts/pep_service.py`** — the real deployment entry point, not a test harness | 8082 |

Credentials came from `scripts/bootstrap_demo_secrets.py`, so the signed policy bundle is
`agentiam_core.corpus.CORPUS_SOURCE` and the route table is `scripts.serve_pep.ROUTES` —
the same artefacts `make demo-up` mounts.

### The scenario

One mandate, **BDT 500,000**, delegated two levels deep — the shape `DEMO.md` beat 2
describes ("root spawns 3 sub-agents"):

```
agt-root          depth 0   scopes: invoice:read, vendor:read, payment:initiate   (mandate)
├── agt-doc-reader   depth 1   ScopeSubset{invoice:read, vendor:read}   BudgetCeiling 0
├── agt-negotiator   depth 1   ScopeSubset{vendor:read}                 BudgetCeiling 50,000
└── agt-payer        depth 1   ScopeSubset{payment:initiate}            BudgetCeiling 200,000
    └── agt-settlement  depth 2  ScopeSubset{payment:initiate}          BudgetCeiling 25,000
```

The signed Cedar bundle permits `invoice:read` and `vendor:read` unconditionally, and
`payment:initiate` when `amount <= 500,000 && principal.depth <= 2`.

---

## 2. Findings at a glance

| # | Severity | Finding | Status |
|---|---|---|---|
| **1** | **Critical** | The deployed PEP never evaluates token caveats. `ScopeSubset` and `BudgetCeiling` are ignored — a read-only agent successfully moved money. | **Fixed** — see §5 |
| **2** | **Critical** | The deployed PEP never primed its lease pool, so *every* budgeted request was refused and `budgets.committed` never moved. This is the "nothing changes" symptom. | **Fixed** in this pass |
| **3** | **High** | The identity tree renders **blank** whenever two agents share a depth — `d3.stratify` throws `ambiguous: agt-depth-1` and the error is silently swallowed. | **Fixed** — plus two further render bugs behind it, see §5 |
| 4 | Medium | Max single payment is hard-capped at the 5,000 lease size, with no environment override. A 500,000 mandate cannot authorize a 8,000 payment. | Open |
| 5 | Low | Authentication and routing failures are **not** written to the audit chain, but still hand the caller a `decision_id` that resolves to nothing. | Open |
| 6 | Low | Unmapped routes and revoked tokens return `401`, where `403` is the accurate status. | Open |

Everything else behaved as specified. Section 4 lists what works.

---

## 3. Case-by-case results

Legend: **PASS** = matched expectation · **FAIL** = defect · **N/A** = my request was
malformed, not the app's fault.

### 3.1 Authentication

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| A1 | `GET /proxy/invoices/inv_001`, no `Authorization` header | 401, fail closed | `401 MALFORMED_REQUEST` — "token is absent or empty" | PASS |
| A2 | Same, `Bearer not-a-real-token` | 401, signature cannot verify | `401 TOKEN_INVALID_SIGNATURE` — "does not verify against any of the 1 accepted root key(s)" | PASS |
| A3 | Same, root token with its last 8 chars replaced | 401, tampering breaks the chain | `401 TOKEN_INVALID_SIGNATURE` | PASS |

Fail-closed authentication is solid, and the reason codes are specific.

### 3.2 Root authority

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| B1 | `GET /proxy/invoices/inv_001` as root | 200, `invoice:read` unconditionally permitted | `200` `{"id":"inv_001","total":"12500.0000"}` | PASS |
| B2 | `GET /proxy/invoices/inv_999` as root (no such invoice) | PEP allows, upstream 404 | `404` `{"detail":"no invoice inv_999"}` | PASS |
| B3 | `POST /proxy/payments {amount: 1000}` as root | 200, under every ceiling | **before fix:** `429 LEASE_UNAVAILABLE`<br>**after fix:** `200` `{"payment_id":"pay_9d728d065d40","status":"accepted"}` | **FAIL → fixed** |

### 3.3 Attenuation — the core guarantee

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| C1 | `GET /proxy/invoices/inv_001` as **agt-doc-reader** (`ScopeSubset{invoice:read, vendor:read}`) | 200, inside its subset | `200` | PASS |
| C2 | `POST /proxy/payments {amount: 100}` as **agt-doc-reader** — **no `payment:initiate` scope, `BudgetCeiling` 0** | `403 SCOPE_NOT_GRANTED`. This is `DEMO.md` beat 3, the demo's centrepiece. | **`200` `{"payment_id":"pay_1627b670cc75","amount":"100","status":"accepted"}`** | **FAIL** |
| C3 | `GET /proxy/invoices/inv_001` as **agt-negotiator** (`ScopeSubset{vendor:read}` only) | 403, `invoice:read` was dropped | **`200` — invoice returned in full** | **FAIL** |
| C4 | `GET /proxy/invoices/inv_001` as **agt-payer** (`ScopeSubset{payment:initiate}` only) | 403, holds no read scope | **`200` — invoice returned in full** | **FAIL** |

> **C2 is the headline failure.** A sub-agent explicitly restricted to reading documents,
> with a spend ceiling of exactly zero, initiated a payment and the tool accepted it.
> The README's first promise is *"a parent mints a strictly narrower child token"*. The
> narrowing is minted correctly — it is simply never checked.

### 3.4 Quantitative caps

All post-fix.

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| D1 | `POST /proxy/payments {amount: 1500}` as agt-payer (ceiling 200,000) | 200 | `200` accepted | PASS |
| D2 | `POST /proxy/payments {amount: 250000}` as agt-payer (**ceiling 200,000**) | `403 BUDGET_EXCEEDED` — the child's own caveat binds | `429 LEASE_UNAVAILABLE` — refused, but by the **wrong constraint**. The ceiling was never consulted (see finding 1); the 5,000 lease stopped it. | **FAIL** (right outcome, wrong reason) |
| D3 | `POST /proxy/payments {amount: 600000}` as root (policy limit 500,000) | 403, refused by the signed bundle | `403 POLICY_DENIED` — "denied by policy statement unnamed" | PASS |
| D4 | `POST /proxy/payments {amount: 900}` as agt-settlement (depth 2, ceiling 25,000) | 200, inside ceiling and `depth <= 2` | `200` accepted | PASS |
| D5 | `POST /proxy/payments {amount: 30000}` as agt-settlement (**ceiling 25,000**) | `403 BUDGET_EXCEEDED` | `429 LEASE_UNAVAILABLE` — same masking as D2 | **FAIL** (right outcome, wrong reason) |
| D6 | `POST /proxy/payments {amount: 8000}` as root — a single request larger than the 5,000 lease | 200; the pool tops up to cover it | `429 LEASE_UNAVAILABLE`, **on all 3 retries 3s apart** | **FAIL** (finding 4) |

### 3.5 Routing

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| E1 | `GET /proxy/vendors/ven_01` as root (no route entry) | Refused — `routes.json` default is deny | `401 MALFORMED_REQUEST` — "no route mapping for GET /vendors/ven_01; an unmapped route is an unreviewed route" | PASS (status should be 403/404, finding 6) |
| E2 | `GET /proxy/invoices` (collection, only `/invoices/{id}` is mapped) | Refused | `401 MALFORMED_REQUEST`, same message | PASS (same caveat) |

The default-deny routing works and the message is genuinely good.

### 3.6 Escalations

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| G1 | `POST /v1/escalations` for agt-payer requesting 75,000 | 201, queued for a human | `201` with the escalation id | PASS |
| G2 | `GET /v1/escalations` | The pending item is listed | `200`, 1 item, correct fields | PASS |
| G3 | `GET /escalations` (console page) | Shows the agent, a scope checkbox, an amount capped at the request | `200` — `shows_agent_id: true`, `has_scope_checkbox: true`, `has_amount_max: true` (`max="75000.0000"`) | PASS |
| G4 | `POST .../approve` narrowing 75,000 → 50,000, **no session cookie** | 401 — approving needs an authenticated approver (T-043) | `401 {"detail":"login required"}` | PASS |
| G5 | `POST .../approve` asking for **90,000 and an extra scope** — a widening | Refused (EC-A09) | `401 login required` — **the auth gate fires first, so I could not reach the narrowing check** | **NOT TESTED** |
| G6 | `POST .../deny` | 401 without a session | `401 login required` | PASS |

> **Honest limitation.** G5 is the invariant `DEMO.md` leans on hardest — "approve narrows,
> never widens" — and this pass did **not** verify it end to end. The 401 is correct
> behaviour, but it means the widening check was never reached. Verifying it needs a signed
> session cookie, i.e. the Keycloak login path wired up. It *is* covered by
> `tests/integration/test_escalations_api.py`, but not by a live request.

### 3.7 Revocation

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| H1 | `POST /v1/revocations` for the root `block_id`, `scope: subtree` | 201, recorded and published | `201` with `seq: 1` | PASS |
| H2 | `GET /v1/revocations` | Listed for PEPs to pull | `200`, entry present | PASS |
| H3 | `GET /proxy/invoices/inv_001` as **root**, after revocation | Refused — the revoked root stops working | `401 TOKEN_REVOKED` | PASS |
| H4 | `GET /proxy/invoices/inv_001` as **agt-doc-reader** (a child of the revoked block) | Refused — children die with their root | `401 ANCESTOR_REVOKED` | PASS |
| H5 | `GET /v1/tree/{task}` after revocation | Nodes show `revoked: true` with the reason | All 5 nodes `revoked: true`, reason `"drill: revoke the root block"` | PASS |

**Revocation is the strongest part of the system.** Subtree propagation reached the PEP in
under 6 seconds, the distinction between `TOKEN_REVOKED` and `ANCESTOR_REVOKED` is exactly
right, and the tree reflected it immediately. (Statuses are 401 where 403 fits better —
finding 6.)

Two `4xx`s in this section were **my** error, not the app's: `POST /v1/revocations` needs
`block_id` + `scope ∈ {token, subtree, mandate}` + a `revoked_by` on the approver
allowlist, and `POST /v1/escalations` needs `decision_id` and `intent_hash`. Both APIs
rejected my malformed bodies with accurate messages.

### 3.8 Console and audit

| # | Input | Expected | Actual | |
|---|---|---|---|---|
| F1 | `GET /v1/decisions?limit=100` | Every decision, with outcome and reason code | `200`, 18 for this task: **11 allow / 6 LEASE_UNAVAILABLE / 1 POLICY_DENIED** | PASS |
| F2 | `GET /v1/budgets/dashboard` | Pool with `committed > 0`, invariants hold | `200` — `committed 3,600`, `leased 1,400`, `spend_fraction 0.0072`, `lease_utilization 0.72`, `invariants_ok: true` | PASS |
| F3 | `GET /v1/tree/{task}` | 5 nodes, one per real agent | `200`, 5 nodes — but ids are `["agt-depth-0","agt-depth-1","agt-depth-1","agt-depth-1","agt-depth-2"]` and every `role` is `"unknown"` | **FAIL** (finding 3) |
| F4 | `GET /v1/audit/search?limit=5` | Hash-chained records, newest first | `200`, `total: 41`, correctly ordered | PASS |
| F5 | `GET /v1/audit/custody/{task}` | Full principal → agent → caveat narrative | `200`, 13 entries for the task | PASS |
| F6 | `POST /v1/audit/verify` | `ok: true` | `{"ok":true,"checked":41,"first_bad_seq":null}` | PASS |

The **Budgets & leases** page renders all of this correctly — stacked committed/leased/
allocated bars, per-pool utilisation, and a green invariant lamp.

---

## 4. What works well

Worth stating plainly, because the defects below are concentrated in two files:

- **Authentication and token verification.** Signature, validity window, and depth are all
  enforced, with specific reason codes.
- **Revocation**, including subtree propagation to a live PEP in under 6 seconds.
- **The Cedar policy layer.** D3 refused a 600,000 payment against the signed bundle. The
  bundle's signature is verified at boot and an unsigned one refuses to start.
- **The ledger.** Once primed, leases, settlement, and commit all move correctly, and the
  invariant sweep stayed green across 41 decisions.
- **The audit chain.** 41 records, hash-verified intact, with a working custody query.
- **The escalation queue**, including the console's narrowing controls.
- **Route default-deny**, with an unusually clear refusal message.

---

## 5. The defects in detail

### Finding 1 — Token caveats are never evaluated (Critical, open)

**Evidence:** cases C2, C3, C4. A token carrying `ScopeSubset{invoice:read, vendor:read}`
and `BudgetCeiling(spend_bdt, 0)` successfully initiated a payment.

**Root cause.** `Pipeline.__init__` takes a `caveats_for` callback and defaults it to a
function returning an empty tuple:

```python
# packages/agentiam-pep/src/agentiam_pep/pipeline.py:193
self._caveats_for = caveats_for or (lambda _token: ())
```

which is then handed to the decision function:

```python
# packages/agentiam-pep/src/agentiam_pep/pipeline.py:268
decision = decide(token, context, caveats=self._caveats_for(token), ...)
```

**`scripts/pep_service.py` never passes `caveats_for`.** Neither does `scripts/serve_pep.py`.
So the deployed PEP calls `decide(..., caveats=())` on every request and enforces no
caveat of any kind. The biscuit is verified; its contents are ignored.

This is the visible half of `STATUS.md` gap 2 — "`agent_id` and `role` are **not** on
`VerifiedToken`: they live in attenuation block facts, and there is no Datalog-to-caveat
parser to recover them". The gap is documented as a *reporting* limitation. It is
also an *enforcement* one, and that is not currently stated anywhere.

**Fixed, and it needed no parser.** The original assessment here said this was blocked on
the Datalog→caveat parser of `STATUS.md` gap 2. That was wrong, and the investigation for
`TODO.md` item 1 is what showed it: the caveats compile to `check if` / `reject if`
statements that already live inside the token, and biscuit evaluates them natively once the
request facts are supplied. `verify()` reads facts back out and deliberately never calls
`authorize()`, so nothing ever evaluated them.

`agentiam_core.tokens.authorize_request()` now does, and `decide()` calls it on every
request. Re-verified against a live PEP after the change:

| | before | after |
|---|---|---|
| C2 — doc-reader pays 100 | `200 accepted` | `403 SCOPE_ATTENUATED_AWAY` — *"refused by a check in block 1 of the token: check if operation($op), ["invoice:read", "vendor:read"].contains($op)"* |
| C3 — negotiator reads an invoice | `200` | `403 SCOPE_ATTENUATED_AWAY`, block 1 |
| C4 — payer reads an invoice | `200` | `403 SCOPE_ATTENUATED_AWAY`, block 1 |
| D2 — payer pays over its 200,000 ceiling | `429 LEASE_UNAVAILABLE` (wrong reason) | `BUDGET_EXHAUSTED_CAVEAT`, block 1 |
| D5 — settlement pays over its 25,000 ceiling | `429 LEASE_UNAVAILABLE` (wrong reason) | `BUDGET_EXHAUSTED_CAVEAT`, **block 2** — the depth-2 grandchild's own caveat |

Cost on the hot path: **102.1 µs median / 192.4 µs p99** at depth 1, measured as its own
step in `docs/benchmarks/performance.md`. `decide()` p99 is now 395.1 µs against NFR-1's
1,000 µs budget.

One thing this did **not** fix: `BUDGET_EXHAUSTED_CAVEAT` maps to HTTP 429, which tells a
client to retry a ceiling that is permanent. Folded into finding 6.

### Finding 2 — The lease pool was never primed (Critical, **fixed**)

**Evidence, before the fix:** every budgeted request returned `429 LEASE_UNAVAILABLE`,
including a 100 BDT payment against a 500,000 pool. The database was unambiguous:

```
leases:  0 rows
budgets: total 500000.0000 | committed 0.0000 | leased 0.0000
```

15 decisions recorded, no money moved. **This is the symptom you hit** — the console shows
nothing changing because nothing was changing.

**Root cause.** `LeasePool` acquires its first lease in `prime()`. The e2e slice calls it
by hand (`tests/e2e/test_thin_slice.py:231`); `scripts/pep_service.py`'s `lifespan` started
the emitter, settlement queue, and revocation set — but never primed the pool. The pool's
own docstring describes the resulting dead end:

> *"A dimension holding no lease at all is not covered: there is no `_Held` to top up from…
> That case is a PEP that never primed the dimension, not one that ran dry."*

Because top-ups are only scheduled from an existing lease, a PEP that never primes can
never recover.

**Fix applied** — `scripts/pep_service.py`, in `lifespan`:

```python
if not await pool.prime(BudgetDimension.SPEND_BDT):
    logger.warning("could not acquire an initial %s lease; budgeted requests will be "
                   "refused until a top-up succeeds", BudgetDimension.SPEND_BDT.value)
```

Non-fatal by design: an unreachable ledger at boot is already the per-request fail-closed
case, and crash-looping would take the read-only paths down with it.

**Verified after the fix:** `leases` shows `granted 5000.0000, settled 3600.0000`;
`budgets` shows `committed 3600.0000, leased 1400.0000`; payments B3/D1/D4 return 200.

**No test caught this.** `tests/unit/test_pep_service.py` passes identically before and
after — 22 passed both ways. The deployed service's lease priming has no coverage.

### Finding 3 — The identity tree renders blank with sibling agents (High, open)

**Evidence.** `/v1/tree/{task}` returns five nodes with ids
`["agt-depth-0","agt-depth-1","agt-depth-1","agt-depth-1","agt-depth-2"]`. Replaying
`identity_tree.html`'s own `updateTree()` against that exact payload:

```
inferred ids:       ["agt-depth-0","agt-depth-1","agt-depth-1","agt-depth-1","agt-depth-2"]
inferred parentIds: [null,"agt-depth-0","agt-depth-0","agt-depth-0","agt-depth-1"]

*** d3.stratify THREW: ambiguous: agt-depth-1 ***
```

The template catches it and returns:

```js
try { root = stratify(data); }
catch(e) { console.error("Stratify error", e); return; }
```

so **nothing is drawn, no error is shown, and the status indicator still reads
"Connected"**. The page looks like it is working and reporting an empty tree.

**Root cause chain:**

1. `scripts/pep_service.py:382` derives `agent_id=f"agt-depth-{token.depth}"` — a
   consequence of gap 2, and honestly documented there.
2. Sibling agents therefore collide on one id, and `role` is the configured default
   (surfacing as `"unknown"`).
3. `d3.stratify()` requires unique ids and throws on duplicates.
4. The catch swallows it.

**Fixed, and it took three changes rather than one.** The tree now keys on the terminal
`block_id` (unique per agent, already in the payload) and surfaces a stratify failure
instead of hiding it. That alone was not enough: driving the real page under a browser
found two further bugs that had kept it from ever rendering a node —
`d3.linkHorizontal()`'s accessors were dereferenced twice and threw before the node join,
and a cancelled transition left every node at `opacity: 0`. See `TODO.md` item 15; the
tree now draws root → three siblings → depth 2 → depth 3.

What remains is *labelling*: the nodes read `agt-depth-N` and their roles read `UNKNOWN`,
which still needs the gap 2 parser (`TODO.md` item 4).

### Finding 4 — Single payments are capped at the hardcoded lease size (Medium, open)

`DEFAULT_LEASE_SIZE = Decimal("5000.0000")` and `ServiceSettings.from_env()` reads no
override for it — every other setting has one. A request larger than the lease is refused
with `LEASE_UNAVAILABLE` and **retrying never helps** (verified: 3 attempts, 3s apart, all
429), because the top-up refills to the lease size rather than to cover the request.

So a deployed PEP cannot authorize a single payment over 5,000 BDT regardless of the
mandate. `DEMO.md` beat 4 — a judge setting a 50,000 ceiling and watching the agent spend
up to it — is not reachable on the shipped defaults.

### Finding 5 — Auth and routing failures are not audited (Low, open)

An unauthenticated request returns `decision_id: b67e507f-…`, but
`/v1/audit/search?decision_id=b67e507f-…` returns `{"results":[],"total":0}` and the chain
total stays at 41. Refusals before token verification take `_refuse` rather than
`_record_and_refuse`, so they never reach the ledger.

Two consequences: credential-probing leaves no audit trail, and the `decision_id` handed
to the caller cannot be looked up by the operator it would be quoted to.

### Finding 6 — Status codes (Low, open)

`401` is returned for an unmapped route (a routing decision, not an authentication one)
and for `TOKEN_REVOKED` / `ANCESTOR_REVOKED` (the token authenticated fine; it is no longer
authorized). `403` fits both. Cosmetic, but it misleads a client retrying on 401.

---

## 6. Reproducing this

```bash
docker compose up -d --wait
cd packages/agentiam-controlplane && DATABASE_URL=<dsn> uv run alembic upgrade head && cd -

uv run python scripts/bootstrap_demo_secrets.py --out /tmp/secrets
uv run uvicorn agentiam_controlplane.app:create_app_from_env --factory --port 8000 &
uv run python scripts/serve_tools.py --port 8081 &

# then create a mandate + budget row, mint a root token, attenuate it, and point
# AGENTIAM_PEP_MANDATE_ID at that mandate before starting scripts/pep_service.py
```

The scenario builder and the two driver scripts used for this pass live in the session
scratchpad (`setup_scenario.py`, `drive.py`, `drive2.py`). They are not committed —
building a proper seeded demo is **T-057**, which `STATUS.md` still lists as outstanding,
and it is what would make this reproducible with one command.

---

## 7. Bottom line

The **infrastructure** is in good shape: authentication, revocation, the Cedar layer, the
ledger, and the audit chain all did exactly what the specs say, and the budgets console
renders live data cleanly.

The **enforcement of attenuation** — the thing the project is named for — does not
currently happen in the deployed PEP. `agentiam-core` implements it correctly and proves it
under property tests; `scripts/pep_service.py` never wires it in. Finding 1 and finding 3
are the same root gap seen from two directions, and both are visible in the demo path.

Findings 1 and 2 are fixed. Findings 3, 4, 5 and 6 are open.

One new defect came out of fixing finding 1 and has since been fixed too: the mandate's own
budget check in the authority block was existentially quantified, so a satisfied dimension
rescued a violated one, and an omitted dimension was allowed rather than denied. It was
masked in practice by the ledger, which enforces the mandate ceiling when it issues a lease
— but not for the offline-verification claim, which is the one that rests on the token
alone. Spec 01 §2.3 now records it; `mint_root` emits one check per dimension.

---

# Second pass — 2026-09-07

A fresh hand-driven pass over the **demo stack as shipped** (`make demo-up`), run after
TODO items 1–20 and 22 closed. Same standard as the first pass: nothing mocked, everything
over HTTP, and every case records what was sent, what the specs say should come back, and
what did.

**Commit under test:** `46b9939` · **Result:** 58 cases across 9 areas.
**5 anomalies found**, filed as TODO items 24–28. One is critical.

## 8.1 What was running

| Component | Started by | Port |
|---|---|---|
| Postgres 16 / Redis 7 / Keycloak | `docker compose … up -d --wait --build` | 5433 / 6379 / 8085 |
| Control plane | the demo compose | 8000 |
| **PEP** (`scripts/pep_service.py`) | the demo compose | 8082 |
| Stub tools | the demo compose | internal |

Stack healthy in 53 s (NFR-8 budget: 90 s). Tokens read out of the `demo-secrets` volume, so
the chain under test is the one `make demo-up` produces.

## 8.2 Findings at a glance

| # | Severity | Finding | TODO |
|---|---|---|---|
| **1** | **Critical** | The PEP refuses every budgeted request 60 s after boot, permanently. The boot lease expires *unspent*, and the top-up guard tests the amount remaining rather than the expiry, so no ACQUIRE is ever issued. | 24 |
| 2 | High | The SDK's `AgentIAM-Task-Intent` header can never match a demo-minted mandate: the seed hashes the intent with plain `sha256`, the spec and the PEP use canonical-JSON + SHA-256. | 25 |
| 3 | Medium | `email:send` is served by the stub, governed by the corpus policy and described in the tool catalogue — and mapped by no route, so a whole policy branch has no end-to-end path. | 26 |
| 4 | Low | Chain-of-custody on an unknown task returns `200` with an empty list, where EC-A05 specifies `404`. | 27 |
| 5 | Low (doc) | `DEMO.md`'s F-2 drill scripts a narration for the T-031 template fallback, which is deferred and absent. | 28 |

## 8.3 Case-by-case

### Authentication and request shape — 4/4 as specified

| Sent | Expected | Observed |
|---|---|---|
| no `Authorization` header | 401 `MALFORMED_REQUEST` | ✅ same |
| `Bearer not-a-biscuit` | 401, refused before parsing cost | 401 `TOKEN_INVALID_SIGNATURE` — *more precise than expected* |
| a token truncated mid-chain | 401 | 401 `TOKEN_INVALID_SIGNATURE` |
| `GET /proxy/nope/x` (unmapped) | 401 `MALFORMED_REQUEST` (spec 09 §11, item 11) | ✅ same |

### Root authority — 4/4

`invoice:read` and `vendor:read` allowed; a payment inside every ceiling allowed; an
`x-agentiam-intent` the token is not bound to refused `INTENT_MISMATCH`.

### Attenuation, the core guarantee — 6/6

| Agent | Action | Expected | Observed |
|---|---|---|---|
| `agt-doc-reader` | read invoice (scope kept) | 200 | ✅ |
| `agt-doc-reader` | pay (scope given up) | 403 `SCOPE_ATTENUATED_AWAY` | ✅ |
| `agt-negotiator` | read vendor (kept) | 200 | ✅ |
| `agt-negotiator` | read invoice (given up) | 403 `SCOPE_ATTENUATED_AWAY` | ✅ |
| `agt-payer` | read invoice (given up) | 403 `SCOPE_ATTENUATED_AWAY` | ✅ |
| `agt-settlement` (depth 2) | pay (kept) | 200 | ✅ |

### Quantitative caps — 3/3

`agt-settlement` over its 25,000 caveat ceiling → `BUDGET_EXHAUSTED_CAVEAT`; root over the
mandate's 500,000 → `BUDGET_EXHAUSTED_MANDATE`. The two are distinguishable at last (item 17).

### Policy — 1/1, plus the activation gate 4/4

`agt-subcontractor` at depth 3 holds a valid token and is refused `POLICY_DENIED` by
`principal.depth <= 2` — the layer separation the demo exists to show.

| `POST /policy/activate` | Expected | Observed |
|---|---|---|
| unparseable Cedar | 409 | ✅ |
| parses, fails the corpus | 409 | ✅ |
| the real corpus policy | 200 | ✅ |
| empty source | 409 | ✅ |

### Revocation — 9/9, including the subtree property

Revoking `agt-doc-reader`'s terminal block took effect on the **next request** (< 1 s via the
Redis push path). Revoking `agt-payer`'s block:

```
agt-payer         -> 401 TOKEN_REVOKED
agt-settlement    -> 401 ANCESTOR_REVOKED     (child)
agt-subcontractor -> 401 ANCESTOR_REVOKED     (grandchild)
agt-negotiator    -> unaffected               (unrelated sibling)
```

INV-10 exactly. Re-revoking the same block id is idempotent and returns the same row
(spec 07 §9); an invalid scope is 400; a non-approver is 403.

### Escalations — 18/18

Open 201; **duplicate for the same decision 409 naming the existing escalation** (item 16);
list 200; invalid state 400; approve with no session 401; widening the amount 400; adding an
unrequested scope 400; unknown id 404; non-approver 403; **approving your own agent's
escalation 403** (separation of duties); narrowing 200 with a verifiable elevated token at the
narrowed amount; approving twice 409 (EC-A10); deny 200.

### Audit and custody — 4/5

Chain verifies (`ok: true`) across every record; search 200; custody for the real task returns
55 ordered entries. Only the unknown-task case deviates — see finding 4.

### Console, under a real Chrome — 7/7 pages, no errors

Every page renders with no error banner and no uncaught exception. Identity tree: 6 nodes, the
root labelled by its principal and subtitled `principal` (item 18), the revoked subtree
correctly marked `REVOKED` and the untouched sibling keeping its role. Budgets reconciles to
the paisa — `committed 1,600` = 100 + 1,000 + 500, invariants holding. Both SSE streams emit
their opening `snapshot`. `/metrics` on both services; 46 `agentiam_*` series on the PEP.

> **The budgets page shows `ACTIVE LEASES 0` while the PEP is running.** That is finding 1
> visible on screen, and it is the single most useful symptom to recognise.

## 8.4 What this pass confirms works

Everything the first pass found broken is fixed and stayed fixed: caveats are enforced,
the pool primes, the tree renders with real names. Added since and confirmed here: the
mandate's own ceiling is distinguishable from a caveat's, `failing_caveat` is populated,
duplicate escalations are 409, the root node reads as the principal, and separation of duties
holds. The four refusal layers — scope, caveat ceiling, mandate ceiling, policy — are each
reachable and each name themselves correctly.

## 8.5 Reproducing this

```
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --wait --build
docker compose … run --rm --no-deps -T seed cat /secrets/demo-tokens.json > tokens.json
# then drive the PEP on :8082 and the control plane on :8000
```

For finding 1, the important part is **not** to run `make demo-seed` first: wait 60 seconds
after the stack reports healthy, then send a payment.
