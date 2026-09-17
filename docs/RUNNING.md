# Running AgentIAM

How to bring the stack up, drive it, and check every surface by hand — with the output each
step actually produces. Everything below was captured from a live run on 2026-09-08, not
reconstructed from the code.

`DEMO.md` is the 10-minute presentation script. This is the operator's guide underneath it:
what to type, what you should see, and what to do when you see something else.

---

## 1. Prerequisites

Docker with Compose v2. Nothing else — the images build from the repo's own `Dockerfile`, and
no credential is baked in: `bootstrap` generates the root keypair and signs the policy bundle
on first run (ADR-056).

`uv` and a local `.venv` are needed only for the test suite and the CLI tools run outside a
container.

---

## 2. Bring the stack up

```bash
make demo-up
# or, without make:
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --wait --build
```

One command, and NFR-8 says it reaches healthy in under 90 s (CI's `demo-stack` job measures
exactly this). Expect, in order: `postgres`, `redis` and `keycloak` healthy, then the
`bootstrap`, `migrate` and `seed` one-shots exiting successfully, then `tools`,
`controlplane` and `pep` healthy.

```
Container agentiam-bootstrap  Exited
Container agentiam-migrate    Exited
Container agentiam-seed       Exited
Container agentiam-keycloak   Healthy
Container agentiam-postgres   Healthy
Container agentiam-redis      Healthy
Container agentiam-controlplane  Healthy
Container agentiam-tools      Healthy
Container agentiam-pep        Healthy
```

Where things listen on the host:

| Service | URL | What it is |
|---|---|---|
| Control plane + console | http://localhost:8000 | Every screen, and the `/v1` API |
| PEP | http://localhost:8082 | The enforcement proxy — `/proxy/...` |
| Keycloak | http://localhost:8085 | OIDC. Runs, but the console is **not** wired to it — see §10 |
| Postgres | localhost:5433 | `agentiam` / `agentiam` |
| Redis | localhost:6379 | Revocation push channel |

The order in the compose file is not cosmetic: the PEP binds one mandate at boot and primes
its lease pool there, so `seed` has to have run first or every budgeted request is refused
until a top-up. `tests/unit/test_demo_compose.py` pins that ordering.

### Confirm it is really enforcing

```bash
curl -s http://localhost:8082/readyz
```

```json
{"status": "ready", "enforcing": true,
 "checks": {"upstream_client": true, "upstream_base_url": "http://tools:8081"}}
```

`enforcing` is **derived from the wiring**, not declared — an app built without a pipeline
reports `false`. That is the single most useful field on the stack.

---

## 3. Re-seed before you present

**Do this every time, even on a stack that is already up.** The demo mandate expires **8 hours**
after it is seeded, and past that every call returns `401 TOKEN_EXPIRED` — the token layer
working exactly as designed, and the most likely way to find a dead demo (TODO item 33).

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm --no-deps seed python scripts/seed_demo.py --out /secrets
```

```
budget pool 500000.0000 for mandate d0d0d0d0-0000-4000-8000-000000000001: already present
minted 6 tokens -> /secrets/demo-tokens.json
task id: d0d0d0d0-0000-4000-8000-000000000002
```

Idempotent for the mandate and the budget row; it re-mints the six tokens. The 30-second
canary that tells you the demo is alive:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml run --rm --no-deps -T seed \
  python -c "import json,httpx; t=json.load(open('/secrets/demo-tokens.json'))['tokens']; \
print(httpx.get('http://pep:8080/proxy/invoices/inv_001', \
headers={'Authorization':'Bearer '+t['root']}).status_code)"
```

`200` and you are ready. `401` and you have not re-seeded.

---

## 4. Drive the whole scenario

```bash
make demo-seed
# or:
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm --no-deps seed python scripts/seed_demo.py --drive \
  --tokens /secrets/demo-tokens.json --pep-url http://pep:8080
```

Thirteen calls. **This is the expected output, exactly** — if a line differs, §8 says what it
means:

```
  allow 200  root reads an invoice                        OK
  allow 200  root reads another                           OK
  allow 200  doc-reader reads an invoice                  OK
  allow 200  doc-reader reads a third                     OK
  deny  403  doc-reader attempts a payment                SCOPE_ATTENUATED_AWAY
  allow 200  negotiator looks up a vendor                 OK
  deny  403  negotiator attempts to read an invoice       SCOPE_ATTENUATED_AWAY
  deny  403  payer attempts to read an invoice            SCOPE_ATTENUATED_AWAY
  allow 200  payer settles a small invoice                OK
  allow 200  settlement agent pays within its slice       OK
  deny  429  settlement agent exceeds its ceiling         BUDGET_EXHAUSTED_CAVEAT
  deny  429  root attempts more than the mandate grants   BUDGET_EXHAUSTED_MANDATE
  deny  403  sub-contractor is too deep for the policy    POLICY_DENIED

outcomes: BUDGET_EXHAUSTED_CAVEAT=1, BUDGET_EXHAUSTED_MANDATE=1, OK=7,
          POLICY_DENIED=1, SCOPE_ATTENUATED_AWAY=3
```

**Five distinct refusals, and the distinction is the product.** Each is a different layer
saying no, and a judge who understands the difference understands the architecture:

| Reason code | Which layer refused | Why it fires here |
|---|---|---|
| `SCOPE_ATTENUATED_AWAY` | The token's own Datalog | The agent's own chain narrowed that scope away — nothing to do with org policy |
| `BUDGET_EXHAUSTED_CAVEAT` | A caveat the *parent* attached | The agent asked for more than its own slice, though the mandate had room |
| `BUDGET_EXHAUSTED_MANDATE` | The mandate's grant | Over the ceiling the human approved. A different fix from the line above, which is why item 17 split them |
| `POLICY_DENIED` | Cedar, the organization's policy | `principal.depth <= 2`, and the sub-contractor is at 3. Token perfectly valid |
| `LEASE_UNAVAILABLE` | The PEP's local budget lease | Should **not** appear — see §8 |

---

## 5. Interact by hand

The tokens live in the `demo-secrets` volume, not on the host, so the easy way to call the PEP
is from inside a container that mounts it. Grab one token into a shell variable:

```bash
TOKEN=$(docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm --no-deps -T seed python -c \
  "import json;print(json.load(open('/secrets/demo-tokens.json'))['tokens']['agt-payer'])")
```

Then call the PEP from the host on port 8082.

### A read that is allowed

```bash
curl -s -H "Authorization: Bearer $TOKEN_DOC_READER" \
  http://localhost:8082/proxy/invoices/inv_001
```

```json
{"id": "inv_001", "vendor_id": "ven_01", "total": "12500.0000", "status": "open"}
```

The body is the upstream tool's, passed through untouched. An allow is invisible by design.

### A payment that is allowed

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN_PAYER" \
  -H 'Content-Type: application/json' \
  -d '{"amount": "1250.0000", "recipient": {"account_id": "acct_9001"}}' \
  http://localhost:8082/proxy/payments
```

`200`, and the pool on `/budgets` moves. This is the call that proves the whole item-29 chain:
`payment_api` is `sensitivity: critical` in the signed catalogue, the bundle forbids critical
resources to non-seniors, and `agt-payer` is `senior` in the organization's role map. Remove
any one of those three and this becomes `403 POLICY_DENIED`.

### A refusal, and what a refusal looks like

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN_SUBCONTRACTOR" \
  -H 'Content-Type: application/json' \
  -d '{"amount": "100.0000", "recipient": {"account_id": "acct_9004"}}' \
  http://localhost:8082/proxy/payments
```

```json
{
  "reason_code": "POLICY_DENIED",
  "detail": "denied by policy statement unnamed",
  "trace_id": "6f59d622-e56c-42fb-b5bf-6d804930a55e",
  "decision_id": "6f59d622-e56c-42fb-b5bf-6d804930a55e"
}
```

`decision_id` is the handle for everything else: it appears on `/decisions`, it is what an
escalation is opened against, and it is in the audit chain.

### The organization's policy refusing a perfectly valid token

Worth showing, because it is the one refusal that has nothing to do with the token's own
authority. The **root** token — the widest one in the demo, with the whole mandate behind it —
cannot pay at all:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN_ROOT" \
  -H 'Content-Type: application/json' \
  -d '{"amount": "400000.0000", "recipient": {"account_id": "acct_9001"}}' \
  http://localhost:8082/proxy/payments
```

```json
{"reason_code": "POLICY_DENIED", "detail": "denied by policy statement policy5", ...}
```

৳400,000 is inside the mandate and inside Cedar's own `amount <= 500000` permit, and the root
is at depth 0, so no ceiling and no depth rule is doing this. `policy5` is
`forbid(...) when { resource.sensitivity == "critical" && principal.role != "senior" }`:
`payment_api` is `critical` in the signed catalogue, and a root token carries no attenuation
block, so it asserts no role at all. **Having a valid token is not the same as the
organization currently allowing the action** — which is the whole argument for the second
layer, in one call.

Push the same token to ৳900,000 and the refusal changes to `429 BUDGET_EXHAUSTED_MANDATE`,
naming *block 0 of the token*: the mandate's own ceiling is evaluated before policy is ever
consulted, so the two layers are visibly distinct rather than interchangeable.

### Revoking, and the credential it needs

Revocation is an *operator* action on the control plane, not an agent action on the PEP, and
it authenticates differently from everything above: no biscuit, no `revoked_by` field. Present
either a console session or the operator token.

```bash
OPERATOR_TOKEN=dev-change-me-operator-token   # AGENTIAM_CONTROLPLANE_OPERATOR_TOKENS
TASK=d0d0d0d0-0000-4000-8000-000000000002
ROOT_BLOCK=$(curl -s http://localhost:8000/v1/tree/$TASK \
  | python -c "import json,sys; print(json.load(sys.stdin)['nodes'][0]['block_ids'][0])")

curl -s -X POST -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"block_id\": \"$ROOT_BLOCK\", \"scope\": \"token\", \"reason\": \"demo\",
       \"expires_at\": \"2030-01-01T00:00:00Z\"}" \
  http://localhost:8000/v1/revocations
```

`201`, and within NFR-4's two seconds every agent in the subtree answers `401
ANCESTOR_REVOKED`. Drop the header and it is `401` — the same call that used to be `201`
purely because the body said so. Recovery is a re-seed; fresh block ids are named by no
revocation.

The default above is a known dev value, exactly like the session key beside it. Set a real
secret for anything reachable beyond localhost.

### Every route the PEP maps

| Method | Path | Scope | Tool | Notes |
|---|---|---|---|---|
| GET | `/proxy/invoices/{id}` | `invoice:read` | `invoice_api` | `low` sensitivity |
| GET | `/proxy/vendors/{id}` | `vendor:read` | `vendor_api` | `low` |
| POST | `/proxy/payments` | `payment:initiate` | `payment_api` | **`critical`**, external |
| POST | `/proxy/email/send` | `email:send` | `email_internal` | not external |
| POST | `/proxy/email/send-external` | `email:send` | `email_external` | external — two routes because the policy turns on `is_external` and a route picks its tool statically (ADR-067) |

Anything else is `401 MALFORMED_REQUEST`, deliberately: an unmapped path is not a 404, because
the PEP refuses to forward what it cannot describe.

The demo mandate does not grant `email:send`, so both email routes answer
`403 SCOPE_NOT_GRANTED`. That is the route working — reaching a scope refusal means the path
resolved.

### The auth header

```
Authorization: Bearer <token>     -> 200
Authorization: bearer <token>     -> 200   (RFC 6750 makes the scheme case-insensitive)
Authorization: <token>            -> 401 MALFORMED_REQUEST
Authorization: Bearer             -> 401 MALFORMED_REQUEST
(absent)                          -> 401 MALFORMED_REQUEST
```

The third line accepted the request until TODO item 32.

### Intent binding

The SDK asserts what task it believes it is doing, and the PEP checks it against the hash the
mandate was minted for:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "AgentIAM-Task-Intent: Procure 500 units of packaging stock, budget BDT 500,000." \
  http://localhost:8082/proxy/invoices/inv_001        # 200

curl -s -H "Authorization: Bearer $TOKEN" \
  -H "AgentIAM-Task-Intent: Buy something else entirely" \
  http://localhost:8082/proxy/invoices/inv_001        # 403 INTENT_MISMATCH
```

Omitting the header is allowed — the token's own intent is used. Claiming a *different* one is
the refusal.

---

## 6. The console

Open http://localhost:8000. Every page renders client-side from the `/v1` API, so if a screen
looks empty, check the API under it before suspecting the page.

| Page | What it shows | The API under it |
|---|---|---|
| `/` | Overview and current posture | — |
| `/decisions` | Every call, newest first, with its reason code | `GET /v1/decisions` |
| `/budgets` | The pool: total, committed, leased | `GET /v1/budgets/dashboard` |
| `/identity-tree?task_id=<task>` | The delegation tree, one node per agent, current minting only | `GET /v1/tree/{task_id}` |
| `/audit` | The hash-chained ledger | `POST /v1/audit/verify`, `GET /v1/audit/search` |
| `/escalations` | Pending requests. The approve/deny buttons need a login this stack does not wire — §10 | `GET /v1/escalations` |
| `/policy` | Cedar authoring, compile and activate | `POST /policy/compile`, `/test`, `/activate` |

The task id is fixed for the demo and printed by the seed script:
`d0d0d0d0-0000-4000-8000-000000000002`.

What the APIs return on a driven stack:

```bash
curl -s http://localhost:8000/v1/tree/d0d0d0d0-0000-4000-8000-000000000002 | head -c 300
```

Five named agents with their own roles and depths — `agt-doc-reader` (reader, 1),
`agt-negotiator` (worker, 1), `agt-payer` (payer, 1), `agt-settlement` (payer, 2),
`agt-subcontractor` (payer, 3) — plus the root as `agt-depth-0`, whose name is honest: a root
token carries no attenuation block, so it declares no agent and no role.

```bash
curl -s -X POST http://localhost:8000/v1/audit/verify
```

```json
{"ok": true, "checked": 91, "first_bad_seq": null, "detail": ""}
```

```bash
curl -s http://localhost:8000/v1/budgets/dashboard | head -c 300
```

```json
{"generated_at": "...", "gauges": [{"dimension": "spend_bdt", "total": "500000.0000",
 "committed": "10750.0000", "leased": "2850.0000", ...}]}
```

`leased` should equal the size of the one lease the PEP currently holds, and `committed`
should equal the sum of every settlement. The invariant checker in §7 asserts exactly that.

### The live stream

`GET /v1/decisions/stream` is Server-Sent Events, and it is what makes the decisions page
move while you drive traffic. Verified delivering events during a drive.

---

## 7. Verifying the claims

Two tools, both of which check the product rather than the tests.

**The audit chain really is a chain:**

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm --no-deps seed python scripts/verify_audit_chain.py
```

```
chain intact: 113 record(s) verified
```

It re-hashes every record and checks each against its predecessor. Tamper with one row in
Postgres and it names the first bad `seq`.

**The books balance:**

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm --no-deps seed python scripts/run_invariant_checker.py --once
```

```
OK  1 budgets, all invariants hold (45.1 ms)
```

Exit code 0 if every invariant holds, 1 otherwise. Without `--once` it sweeps on a loop, which
is what you want on a second screen during a demo. This is the check that would have caught
the double-spend in gap 23 — though note it could not, at the time, because the books were
consistent about a number that had stopped describing reality (ADR-049 is the story).

---

## 8. When something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| Everything is `401 TOKEN_EXPIRED` | The mandate expired — 8 h after seeding | Re-seed (§3). This is the common one |
| `401 MALFORMED_REQUEST` on a call you think is fine | Missing `Bearer ` prefix, or an unmapped path | Check the header and §5's route table |
| A payment is `429 LEASE_UNAVAILABLE` | The PEP holds no usable budget lease | Should not happen: the pool renews on a timer (ADR-070). If it does, the ledger was unreachable — check `postgres`. It self-heals on the next sweep, within `ttl/4` |
| Every agent is `401 ANCESTOR_REVOKED` | Something revoked the root authority block, so the whole subtree cascades | Re-seed. Fresh tokens carry fresh block ids that no revocation names |
| A revocation you set does not stop applying | Working as specified. `expires_at` is *the original token's* expiry, kept so the row can be pruned — not a lease on the revocation (spec 07 §8). Honouring it would silently un-revoke a token | Re-seed |
| A revocation seems to do nothing | You revoked a stale generation's block id — most likely one read off `?generations=all`, or copied from before a re-seed | Take the block id from the plain `/v1/tree/{task}`, which serves the current minting only |
| Revoking answers `401` | No credential. `revoked_by` in the body is not one — the acting identity comes from a session or an operator token | Send `Authorization: Bearer $OPERATOR_TOKEN` — §5 |
| The identity tree looks short of agents | It shows the **current** minting only. Re-seeding mints a fresh chain, and the tree follows it | Add `?generations=all` to see every superseded chain the audit record still holds |
| Beat 5's NL compiler shows an error | Ollama is not running. It is opt-in via a compose profile | Expected. The page degrades cleanly: `Ollama network error: All connection attempts failed`, no hang, no 500. `DEMO.md`'s F-2 drill scripts this |
| The stack froze and resumed | The host machine slept. Every container stops together | Nothing to do; the lease renews within `ttl/4` of resume. Re-seed if you were asleep more than 8 h |
| A page is blank but its API returns data | The console is client-rendered | Check the browser console; the API is fine |

Logs, when the table does not cover it:

```bash
docker logs agentiam-pep --tail 50
docker logs agentiam-controlplane --tail 50
```

A PEP that refuses to start says why and exits 2 — every configuration error names the
variable, because the first reader of it is a container log.

---

## 9. Teardown

```bash
make demo-down          # stop, keep the volumes (and so the mandate, tokens and audit chain)
```

For a genuinely clean slate — a fresh root keypair, a fresh bundle and an empty audit chain:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml down -v
```

That drops the `demo-secrets` volume too, so the next `demo-up` re-bootstraps from nothing.
Worth doing once before a real presentation.

---

## 10. What is deliberately not here

Stated so a judge asking finds an answer rather than a gap (`STATUS.md` §3 has the full list):

- **One PEP serves one mandate.** The lease pool binds a `mandate_id` at construction (gap 25).
- **No issuance or bundle-publishing service.** The bundle is signed at bootstrap and loaded
  once at boot, so hot reload and staleness are unreachable (gap 26); roles are static
  configuration (gap 28); `budgets.mandate_id` has no foreign key (gap 7).
- **Drift detection needs Ollama**, and is off unless you enable the profile. `None` is a
  legitimate configuration, not a degraded one (spec 06 §2.1).
- **OIDC login is not wired, so nobody can approve or deny an escalation on this stack.**
  Keycloak runs and is healthy, but the console has no `AGENTIAM_CONTROLPLANE_OIDC_*`
  configured, `/auth/login` is therefore not mounted (404), and `/readyz` reports
  `auth: false` — derived from the wiring, like the PEP's `enforcing`. Escalations can be
  opened, listed and left to expire, which is the honest demo; approve/deny answers
  `401 login required` until you set the three OIDC variables. **Do not script a live human
  approval against this stack.** Revocation is unaffected: it takes an operator token, for
  exactly the reason that incident response cannot depend on a browser login.
- **No demo beat shows the role forbid firing** *in the scripted thirteen*. It is enforced,
  corpus-tested, and reachable by hand — `root` paying ৳400,000 answers
  `403 POLICY_DENIED / policy5`, because `payment_api` is `critical` and the root token
  carries no role, let alone `senior` (TODO item 30). §5 has the call.
