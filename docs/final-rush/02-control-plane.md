# 02 — The control plane an operator actually uses

**The problem in one line.** Most console pages are read-only views over data a script
produced. The pages that do act — `/run` and the policy editor — run on in-memory stand-ins:
`TaskRegistry` is process-local and reuses a hard-coded `DEMO_MANDATE_ID`
(`agentiam_controlplane/tasks.py`), because a PEP serves exactly one mandate (gap 25,
`LeasePool.__init__(…, mandate_id)` in `agentiam_pep/pool.py`). Mandates are a Pydantic model
with no table (gap 7). Roles are a static JSON file read at boot (gap 28). An approved
escalation's token goes to the approver, not the agent. Nothing in the console can revoke.

After this workstream, every operator action an IAM product needs exists, is persisted, is
authorized, and is audited.

**Ordering.** T-081 and T-083 are on the Jakarta-critical path and have no dependency on the
rest. T-074 → T-075 → T-076 → T-077 is the money-path chain and goes first in W3.

---

### T-074 — *Spec*: mandate issuance and persistence

**Size** S · **Lane** control plane · **GPU** — · —

**Why.** `PLAN.md` §8 lists an Issuance API (`POST /v1/tasks`, `/approve`, `GET`, `/revoke`)
that was never built; mandates exist only inside tokens. Spec-first, because this touches the
token's authority block, the ledger's `budgets` rows and the custody chain at once.

**Read.** `docs/PLAN.md` §6.1, §7, §8 Issuance; `docs/specs/01-token-format.md`;
`docs/specs/04-lease-protocol.md` §3; `agentiam_core/models.py` `Mandate`, `Budget`;
`agentiam_controlplane/tasks.py`; `scripts/seed_demo.py` `_mandate`; ADR-014; gap 7.

**Probe first.** List every place a mandate is created today (`grep -rn "Mandate(" packages
scripts`) and every place a `budgets` row is inserted. Record whether the two are ever created
in one transaction. They must be after this spec.

**Deliverables.** `docs/specs/11-issuance.md` (spec 12 is reserved for the MCP gateway, T-089) specifying:
1. Task lifecycle: `draft` → `approved` → (`expired` | `revoked`); who may move each edge.
2. `mandates` table: every `Mandate` field, `status`, `approved_by` (OIDC `sub`),
   `approved_at`, `root_block_id`, `revoked_at`.
3. Approval in one transaction: insert the mandate, insert its `budgets` rows (one per
   dimension), mint the root token, record the root block id, append an audit record.
4. The root token is returned once and never stored (store its block id only).
5. FK `budgets.mandate_id → mandates.mandate_id` (closes gap 7).
6. Separation of duties: whether the requester may approve their own task (decide; the
   escalation path already refuses self-approval).
7. Expiry: what happens to leases and budgets when a mandate expires (reaper interaction).
8. API shapes, status codes, reason codes.

**Acceptance criteria.**
- [ ] Every API in `PLAN.md` §8 Issuance specified or explicitly replaced.
- [ ] ADR for any departure from `PLAN.md` §8.

---

### T-075 — Mandates table and issuance API

**Size** L · **Lane** control plane · **GPU** — · —

**Why.** Implement T-074. After this, a mandate is a durable, approvable, revocable object, and
the demo stops depending on a fixed id.

**Read.** `docs/specs/11-issuance.md`; `db/models.py`; `db/ledger.py`; `auth.py` (session
handling from T-043).

**Depends on.** T-074.

**Probe first.** Apply the FK migration against a copy of a seeded demo database and record
whether existing `budgets` rows violate it. If they do, the migration needs a backfill step.

**Deliverables.**
- Migration `0009_mandates.py` with the FK and any backfill.
- `db/mandates.py`, `issuance_api.py` with the four endpoints.
- `scripts/seed_demo.py` and `bootstrap_demo_secrets.py` issue through the same code path,
  not around it.
- `tasks.py` stops minting under `DEMO_MANDATE_ID` once T-076 lands (leave a guarded TODO
  naming T-076 until then).

**Acceptance criteria.**
- [ ] Approve is atomic: kill the transaction after the mandate insert and before the budget
      insert (fault injection) — neither row exists afterward.
- [ ] Approve without a session → 401; approve by a principal the spec forbids → 403.
- [ ] Revoke publishes a revocation for the root block and every PEP refuses the subtree within
      NFR-4's 2 s.
- [ ] `GET /v1/tasks/{id}` returns live budget state per dimension.
- [ ] Gap 7 closed in `docs/STATUS.md`.

---

### T-076 — Multi-mandate PEP: lease-pool registry

**Size** L · **Lane** ledger and leases · **GPU** — · —

**Why.** Gap 25 / `research/lease-plan.md` GAP-G: a PEP enforces budget for exactly one
mandate, bound at boot. A real deployment serves many tasks at once. This is the money hot
path; review it with care.

**Read.** `docs/specs/04-lease-protocol.md` (all, especially §3, §4, §9); `agentiam_pep/pool.py`;
`agentiam_pep/pipeline.py` `reserve()`; `scripts/pep_service.py`; ADR-049, ADR-053; T-062's
measured recreate time.

**Depends on.** T-075.

**Probe first.** Instantiate two `LeasePool`s for two mandates against one real Postgres in a
scratchpad and drive them concurrently. Record whether anything shared (the settlement sink,
the `before_release` hook, metrics labels) assumes a single mandate.

**Deliverables.**
- Spec 04 addendum: per-mandate pools inside one PEP; lazy creation on the first request for a
  mandate; idle eviction (release the lease after `idle_ttl`); a cap on pools per PEP; what
  happens at the cap (deny with a named reason code, never evict a pool with in-flight
  reservations).
- `agentiam_pep/pool_registry.py`: maps `mandate_id` (read from the verified token's authority
  block, never from a header) to a `LeasePool`.
- `Pipeline.reserve()` resolves the pool through the registry.
- `AGENTIAM_PEP_MANDATE_ID` becomes optional (pre-warm hint), not required.

**Acceptance criteria.**
- [ ] Two mandates through one PEP, concurrently: each mandate's ledger invariant holds; neither
      can spend the other's budget (integration test).
- [ ] A token whose mandate has no `budgets` row is refused with a named reason, not a 500.
- [ ] Pool cap reached → named deny; existing pools unaffected.
- [ ] The Hypothesis stateful machine (`test_ledger_properties.py`) extended to two mandates.
- [ ] `make chaos` CH-1, CH-3, CH-4, CH-10 re-run with two mandates; invariant holds in all.
- [ ] `docs/STATUS.md` gap 25 and `research/threat-model.md` N8 closed.

**Out of scope.** Settlement and shutdown per mandate (T-077).

---

### T-077 — Multi-mandate PEP: settlement, release, shutdown, metrics per mandate

**Size** M · **Lane** ledger and leases · **GPU** — · —

**Why.** Completes T-076. The settlement batcher, the `before_release` hook (ADR-049's fix for
releasing a lease with settlements queued) and `aclose()` were written for one pool.

**Read.** `agentiam_pep/settlement.py`; `db/settlement_sink.py`; ADR-049, ADR-053; T-076
addendum.

**Depends on.** T-076.

**Deliverables.**
- Settlement batches may mix mandates; the clamp stays cumulative *per mandate* (ADR-053's
  finding, applied per key).
- Idle eviction drains that mandate's queued settlements before release.
- `aclose()` drains and releases every pool.
- Metrics labelled by mandate, with label cardinality bounded (hash bucket or top-N).
- `scripts/demo_concurrency.py` no longer recreates PEPs per run.

**Acceptance criteria.**
- [ ] A batch with two mandates where one is exhausted: the other commits; nothing rolls back.
- [ ] Evicting a pool with queued settlements loses none (count before and after).
- [ ] `make demo-conc` runs twice in a row without recreating any container.

---

### T-078 — Console mandate issuance and task detail page

**Size** M · **Lane** control plane / console · **GPU** — · —

**Why.** Turn `/run` from a demo driver into the real issuance flow: an operator drafts a task,
an approver approves it, and the root token is handed to the agent runtime.

**Read.** `console/templates/run.html`; `tasks.py`; T-075 API.

**Depends on.** T-075, T-076.

**Deliverables.**
- "New task" form: description, scopes (from the bundle's tool catalogue, not a hard-coded
  list), per-dimension budget, max depth, expiry.
- Approval view for a second principal.
- Task detail page: mandate, live budget gauge per dimension, identity tree link, decision feed
  filtered to the task, revoke button (T-083).
- Token hand-off: one-time download or one-time copy, never rendered again; the agent runtime
  (T-086) can also fetch it with a one-time code.
- `TaskRegistry` (process-local) deleted; everything reads from the database.

**Acceptance criteria.**
- [ ] A task created in the console is enforced by the PEP with its own ceiling and its own
      ledger rows (e2e).
- [ ] Restarting the control plane loses no task.
- [ ] The root token never appears in any HTML response after first display (test greps
      responses).

---

### T-079 — Agent registry and role management

**Size** L · **Lane** control plane / console · **GPU** — · —

**Why.** Gap 28 / `research/threat-model.md` N9: Cedar roles come from a static JSON map of
delegation path → role (`AGENTIAM_PEP_ROLE_ASSIGNMENTS_PATH`, ADR-069), read at boot. Adding an
agent requires a file edit and a restart; roles never expire.

**Read.** ADR-069; `docs/specs/05-policy.md` §2 (entity model); `bootstrap_demo_secrets.py`
`ROLE_ASSIGNMENTS`; `scripts/pep_service.py` role source.

**Depends on.** T-075.

**Probe first.** Confirm the token cannot choose its own Cedar role
(`test_neither_lets_the_token_choose_the_cedar_role` exists). Keep that property.

**Deliverables.**
- `agents` and `role_assignments` tables (agent id or delegation-path pattern, role, granted
  by, granted at, expires at).
- API and console page to grant, list and revoke role assignments.
- Distribution to PEPs with the same push/pull pattern as revocations and policy; the PEP's
  role source reads the synced map.
- Every change audited.

**Acceptance criteria.**
- [ ] Granting `senior` to an agent takes effect at the PEP within 2 s without a restart; the
      `forbid_critical_tool_payment_worker` policy flips for that agent (e2e).
- [ ] An expired assignment stops applying.
- [ ] A token claiming a role in its own facts still cannot obtain it.
- [ ] `docs/TODO.md` item 30 (no demo beat shows role discrimination) closed with a seeded
      non-senior payer.

---

### T-080 — Audited budget administration

**Size** M · **Lane** ledger and leases · **GPU** — · —

**Why.** Operators need to top up or reduce a mandate's budget. Today `total` is just a
column, and mutating it silently is also exactly the attack `research/lease-plan.md` GAP-A
describes. Changes must be append-only, attributable and invariant-preserving.

**Read.** `db/ledger.py`; `db/models.py` `ck_budgets_invariant`; `research/lease-plan.md` GAP-A;
`console/templates/budgets.html`.

**Depends on.** T-075.

**Deliverables.**
- `budget_adjustments` table (mandate, dimension, delta, reason, actor, at).
- `adjust_total()` in the ledger: applies the delta under `FOR UPDATE`, refuses a reduction
  below `committed + leased + allocated`, writes the adjustment row in the same transaction.
- `total` becomes derivable: a check that `total = initial + Σ adjustments`, run by the
  invariant checker.
- Console controls on the budgets page; API endpoint; requires the `operator` role (T-082).

**Acceptance criteria.**
- [ ] A reduction below current use is refused with a named reason.
- [ ] A direct SQL edit of `total` (no adjustment row) makes the invariant checker report a
      violation (proves the derivation check bites).
- [ ] Concurrent adjust + acquire: invariant holds (integration).

---

### T-081 — Escalation round-trip to the requesting agent

**Size** L · **Lane** control plane / agents · **GPU** — · **J**

**Why.** `escalations_api.approve_escalation` mints an elevated token with `mint_root` and
returns it in the approver's HTTP response. Nothing delivers it to the agent that was refused,
the SDK has no `await_escalation`, and the elevated token's link to the original task is not
recorded in custody. On stage, "a human approves and the agent continues" is the single
best beat; today it cannot happen.

**Read.** `escalations_api.py`; `agentiam_core/escalation.py`; ADR-041, ADR-046;
`docs/PLAN.md` §8 Escalation and SDK surface (`ApprovalRequired`, `await_escalation`);
`agentiam_pep/escalation_sink.py`.

**Depends on.** T-085 (for the SDK half; the server half can start first).

**Probe first.** Drive one escalation end to end on the demo stack and record exactly what the
agent sees at each step (status code, body, headers) and what the approver sees. Check whether
the elevated token's authority block carries anything tying it to the original task and
decision; if not, the custody chain breaks at elevation.

**Deliverables.**
- The elevated token is stored encrypted at rest (or held for a short window) and released only
  to the requesting agent, authenticated by its current token; the approver's response no
  longer contains it.
- `GET /v1/escalations/{id}/await?timeout=…` long-poll (or SSE) for the agent.
- The elevated token's authority block carries `parent_task_id`, `escalation_id` and
  `decision_id`; `custody()` follows the link.
- PEP denial response for an escalatable deny carries the `escalation_id`.
- SDK: `ApprovalRequired(escalation_id)` raised on that response; `await_escalation()` returns
  a client bound to the elevated token.
- A denied escalation raises a typed error in the SDK.

**Acceptance criteria.**
- [ ] e2e: agent refused → escalation pending → approver narrows and approves → agent receives
      the narrowed token within 1 s → retried call succeeds within the narrowed amount → a
      call above it is refused.
- [ ] The approver's response body contains no token (test).
- [ ] Custody for the final payment walks back through the escalation to the original human.
- [ ] Timeouts and denials are typed SDK errors.

---

### T-082 — Console RBAC, CSRF protection, operator-action audit

**Size** M · **Lane** control plane / console · **GPU** — · —

**Why.** Every logged-in user can currently do everything the console offers. Mutating console
forms post without CSRF tokens. Operator actions (activate policy, approve, revoke, adjust
budget, grant role) need their own audit stream, separate from agent decisions.

**Read.** `auth.py`, `demo_auth.py`; `deploy/keycloak/` realm; `console/templates/*.html` forms.

**Deliverables.**
- Roles from Keycloak groups: `viewer`, `operator` (tasks, budgets, revoke), `approver`
  (escalations, task approval), `policy-admin`; enforced server-side on every route.
- CSRF tokens on every state-changing form and `fetch`; `SameSite=Lax` session cookie
  confirmed.
- `operator_actions` table (actor, role, action, target, before/after digest, at), hash-chained
  the same way as the audit chain.
- Console page listing operator actions.
- Realm export updated (`<realm>-realm.json` naming, see the Keycloak hazard).

**Acceptance criteria.**
- [ ] A `viewer` gets 403 on every mutating route (parameterized test over the route table, so
      a new route without a guard fails the test).
- [ ] A POST without a valid CSRF token is refused.
- [ ] Every mutating route writes exactly one operator-action record (route-table test).

---

### T-083 — Revoke from the console

**Size** S · **Lane** control plane / console · **GPU** — · **J**

**Why.** Revocation is fully real in the backend (`revocations_api.py`, Redis push, pull
backstop, < 2 s across 3 PEPs), but the identity tree can only display it. The demo's
revocation beat currently needs a terminal.

**Read.** `console/templates/identity_tree.html`; `revocations_api.py`; spec 07; `docs/TODO.md`
item 34 (revoking a stale generation appears to do nothing).

**Deliverables.**
- A "Revoke" action on each node (token scope) and "Revoke subtree" on non-leaf nodes, with
  a confirmation step.
- After revoking, the page shows the measured propagation time: the time from the request until
  every PEP reports the revocation (PEP `/readyz` or a revocation-ack metric).
- Revoking a stale generation's block warns that it affects only that generation.
- Requires the `operator` role once T-082 lands.

**Acceptance criteria.**
- [ ] e2e: revoke a subtree from the page; every agent below it is refused with
      `ANCESTOR_REVOKED` or `TOKEN_REVOKED` on its next call.
- [ ] The propagation time shown matches the server-side measurement within 100 ms.

---

### T-084 — Console UX and accessibility pass

**Size** M · **Lane** console · **GPU** — · —

**Why.** Once pages act rather than display, error, empty and loading states matter, and an
international panel will see the console on an unknown projector.

**Read.** All `console/templates/*.html`; `static/style.css`; `docs/DEMO.md` F-6.

**Depends on.** T-078, T-083.

**Deliverables.**
- Every page has explicit empty, loading and error states; no blank canvases.
- Keyboard navigation and visible focus on every control; form labels; colour contrast meets
  WCAG 2.1 AA.
- Layout verified at 1024×768, 1366×768 and 1920×1080.
- Playwright smoke test per page (dev dependency, ADR) that loads it against a seeded stack,
  checks for console errors, and takes a screenshot committed under `docs/evidence/console/`.

**Acceptance criteria.**
- [ ] Zero browser console errors on every page in the smoke test.
- [ ] Automated accessibility check (axe-core via Playwright) reports no serious or critical
      violations.
