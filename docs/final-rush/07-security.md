# 07 — Security hardening and enterprise readiness

**The problem in one line.** The cryptographic core is strong, but the deployment around it is
not: no TLS between any services (while `PLAN.md` T-044's deferral says "mTLS in place"), bearer
tokens with no proof of possession (TM-01, accepted risk), no root-key rotation story in
operation, `RateLimit` and per-scope fail-open specified but never built, a red-team table with
gaps, and a single-organization data model.

Tickets here are W3 work; none is on the Jakarta path.

---

### T-109 — TLS and mTLS between all services

**Size** M · **Lane** infra · **GPU** — · —

**Why.** Every hop is plaintext today: agent → PEP, PEP → control plane, PEP → Postgres, PEP →
Redis, PEP → upstream tools. Tokens are bearer credentials, so plaintext hops leak authority.
`PLAN.md` T-044 was deferred on the claim that mTLS was in place; it was not.

**Read.** `docker-compose.yml`, `docker-compose.demo.yml`, `deploy/k3s/`; `scripts/pep_service.py`;
`agentiam_controlplane/app.py` `create_app_from_env`; the SELinux `:z` and named-volume ownership
hazards (certificates are mounted files).

**Probe first.** Confirm with `tcpdump` (or `ss` plus a capture in a container) that a token
crosses the PEP → upstream hop and the agent → PEP hop in plaintext on the demo stack. Keep the
capture command for the acceptance test.

**Deliverables.**
- `scripts/dev_ca.py`: creates a development CA and per-service certificates (control plane, each
  PEP, tools, Postgres, Redis) with SANs matching compose and k3s service names.
- TLS on the agent → PEP listener; mTLS PEP ↔ control plane (the control plane authenticates the
  PEP by certificate, which T-107 then uses); TLS to Postgres (`sslmode=verify-full`) and Redis.
- Compose and k3s manifests mount certificates with the right SELinux labels and file ownership.
- A `TLS=off` development switch that is impossible to enable in the release image.
- `docs/deployment.md` section on certificate rotation.

**Acceptance criteria.**
- [ ] The probe's capture shows no plaintext token on any hop.
- [ ] A PEP presenting a certificate from a different CA is refused by the control plane.
- [ ] `make demo-up` still reaches healthy within NFR-8's 90 s.
- [ ] `PLAN.md` T-044 rationale corrected.

---

### T-044 — SPIRE workload identity on k3s

**Size** L · **Lane** infra · **GPU** — · —

**Why.** Deferred since M9. SPIFFE identities replace hand-issued certificates with attested,
rotating workload identities in Kubernetes. `research/related-work.md` notes SPIFFE is
complementary: it could also supply the attestation that agent roles currently lack (N9).

**Read.** `docs/PLAN.md` T-044; `deploy/k3s/`; `docs/deployment.md`; T-109; the kind and
`imagePullPolicy` hazards.

**Depends on.** T-109.

**Deliverables.**
- SPIRE server and agent manifests for k3s; registration entries for the control plane and PEPs.
- PEP ↔ control plane mTLS using SVIDs (via the SPIFFE workload API or a helper sidecar that
  writes rotating certificates).
- Verified on a real `kind` cluster; `--dry-run=server` validation in CI where a cluster is
  available.
- An ADR on whether agent roles can be bound to SPIFFE identities (a design note, not an
  implementation).

**Acceptance criteria.**
- [ ] Certificate rotation happens without restarting the PEP (observe two rotations).
- [ ] A workload without a registration entry cannot reach the control plane.

---

### T-110 — Proof-of-possession tokens (TM-01)

**Size** L · **Lane** policy / core · **GPU** — · —

**Why.** A-06 succeeds by design: a stolen token works until expiry or revocation. Binding a
token to a key the agent holds turns theft of the token alone into nothing. This is the largest
remaining accepted risk in `docs/threat-model.md`.

**Read.** `docs/threat-model.md` TM-01; `docs/specs/01-token-format.md`; biscuit's support for
third-party blocks and request signing (probe the installed version); RFC 9449 (DPoP) and RFC 9421
(HTTP Message Signatures) as reference designs.

**Probe first.** Check whether the installed `biscuit-python` can embed a public key in a block
and whether an authorizer can check a request signature against it. If not, the binding check
moves into the PEP, outside the biscuit authorizer; record which and why.

**Deliverables.**
- Spec 01 addendum (spec first): each attenuated block may bind a holder public key; the PEP
  requires a per-request signature over method, path, body digest and a timestamp, verified
  against the most recent bound key; replay window and clock tolerance stated.
- `attenuate()` takes an optional holder key; the SDK generates a per-agent keypair and signs
  requests.
- PEP verification with a named reason code for a missing or invalid proof.
- A-06 re-run: replaying a stolen bound token from another holder now fails.
- NFR-1 impact measured and published.

**Acceptance criteria.**
- [ ] A-06 flips from "accepted risk" to "mitigated for bound tokens"; unbound tokens behave as
      before (opt-in per mandate).
- [ ] Replay of a captured signed request outside the replay window fails.
- [ ] Each check proven load-bearing.

---

### T-111 — `RateLimit` caveat

**Size** M · **Lane** policy / core · **GPU** — · —

**Why.** `PLAN.md` §21 item 7; spec 02 §5. The one caveat needing verifier-side state, which is
why it was deferred: it threatens the stateless hot path that NFR-1 depends on.

**Read.** `docs/specs/02-caveat-language.md` §4, §5; `docs/PLAN.md` §6.2; `agentiam_core/caveats.py`;
T-066's agreement property.

**Probe first.** Measure a PEP-local sliding-window counter (in-process, per token id) against
NFR-1's benchmark. Record the cost. A cross-PEP limit (Redis) is a different design; measure it
too and state which the spec chooses.

**Deliverables.**
- Spec 02 §5 rewritten from "deferred" to the chosen design. Recommended: per-PEP local limit,
  stated honestly as "N per window per PEP", with the cross-PEP limit expressed as a ledger
  dimension (`tool_calls` already exists) rather than a new mechanism.
- `RateLimit` clause in `caveats.py` and its Datalog form; T-066's property extended to it.
- Attenuation narrowing rule: a child's limit ≤ its parent's.

**Acceptance criteria.**
- [ ] A burst above the limit is refused with a named reason; below it is allowed.
- [ ] Narrowing property holds (property test).
- [ ] NFR-1 p99 still < 1 ms.

---

### T-112 — Per-scope opt-in fail-open (NFR-7)

**Size** S · **Lane** policy · **GPU** — · —

**Why.** README's design commitments and `ENGINEERING-RULES` rule 6 say fail-open is per-scope,
opt-in and audited. `research/gap-analysis.md` found no configuration surface for it.

**Read.** `docs/PLAN.md` NFR-7; `agentiam_core/decision.py` (the fail-closed steps); spec 09.

**Probe first.** Grep the codebase for any existing fail-open switch. The only one found on
2026-10-16 is drift's advisory fail-open in `agentiam_pep/drift.py`, which is a different thing.

**Deliverables.**
- A bundle-level (signed) list of scopes allowed to fail open on control-plane unavailability,
  per failure kind (ledger unreachable, policy stale). Never for revocation.
- Every fail-open decision recorded with a distinct outcome and reason, and counted in metrics.
- Spec 05 or 09 updated.

**Acceptance criteria.**
- [ ] With the ledger down, a listed scope is allowed and audited as fail-open; an unlisted scope
      is denied.
- [ ] A revoked token is denied regardless of fail-open configuration (test).

---

### T-113 — Root key rotation and key custody

**Size** M · **Lane** core / control plane · **GPU** — · —

**Why.** The control plane holds the root private key in configuration. `RootKeySet` exists in
`agentiam_core/tokens.py`, but there is no operational rotation, no key id discipline in the
console, and no custody story beyond an environment variable.

**Read.** `docs/specs/01-token-format.md` §8 (rotation); `agentiam_core/tokens.py` `RootKeySet`;
`scripts/bootstrap_demo_secrets.py`; `docs/deployment.md`.

**Probe first.** Mint a token with key A, rotate to key B with A still in the verification set,
and confirm the PEP verifies both; then retire A and confirm A-tokens fail with a named reason.
Record what breaks.

**Deliverables.**
- Rotation procedure: add the new key (verify-only) → switch signing → retire after max token
  lifetime. Each step is a command (`scripts/rotate_root_key.py`) and an operator action (audited,
  T-082).
- The same procedure for the policy signing key (T-070) and the audit checkpoint key (T-108).
- A key-provider interface with two implementations: file (current behaviour) and an open-source
  secrets manager (for example OpenBao's transit engine, which signs without exposing the key).
  Choose in an ADR; no paid service.
- `docs/deployment.md` key ceremony section.

**Acceptance criteria.**
- [ ] Rotation under load drops no valid request (integration test).
- [ ] A retired key's tokens fail with a named reason.

---

### T-114 — Remaining red-team attacks A-14…A-33

**Size** L · **Lane** security tests · **GPU** — · —

**Why.** `PLAN.md` §12 lists 33 attacks; T-051 implemented 22. A grep of `tests/` on 2026-10-16
finds ids A-15, A-20 and A-21 nowhere, and A-14, A-16, A-19, A-22, A-27, A-31, A-32 and A-33 may
be mentioned without a dedicated test. `PLAN.md`'s reporting rule requires every attack to
record `mitigated | partially mitigated | accepted risk`.

**Read.** `docs/PLAN.md` §12; `tests/security/test_redteam_suite.py`,
`tests/integration/test_redteam_suite.py`; ADR-048; `docs/threat-model.md`.

**Probe first.** For each id A-01…A-33, find the test that exercises it (not a comment that
mentions it). Produce the table of which have real tests before writing any.

**Deliverables.**
- Tests for every attack lacking one. In particular: **A-15** self-approval of an escalation (the
  route refuses it; make the test name the id); **A-20** rapid top-up loop as a ledger DoS (rate
  limit per PEP; this may need code); **A-21** negative or NaN amounts rejected at every entry
  point (SDK, PEP, ledger, API).
- `docs/evidence/redteam.md` generated from test metadata: all 33 attacks, outcome
  classification, rationale, test id. Picked up by the evidence pack.

**Acceptance criteria.**
- [ ] 33 of 33 attacks have a named test or a written reason why they cannot be tested.
- [ ] The table honestly contains accepted risks (A-06 until T-110; A-27 until T-095).

---

### T-115 — API contract fuzzing

**Size** S · **Lane** security tests · **GPU** — · —

**Why.** `PLAN.md` §21 item 4. Schemathesis generates requests from the OpenAPI schema and finds
500s, schema violations and crashes that hand-written tests miss.

**Depends on.** T-123 (committed OpenAPI).

**Deliverables.**
- Schemathesis (dev dependency, ADR) run against the control plane and PEP in an optional CI job.
- Every 500 found is fixed or ticketed.

**Acceptance criteria.**
- [ ] Zero 500 responses across the run, with the example count recorded.

---

### T-116 — OPA as a second policy backend

**Size** M · **Lane** policy · **GPU** — · —

**Why.** `PLAN.md` §21 item 14. The `PolicyEngine` protocol exists; organizations standardized on
Rego will ask. Low priority: do it only after everything else in this file.

**Read.** Spec 05 §7 (two engines behind one protocol); `agentiam_pep/policy.py`.

**Depends on.** T-072.

**Deliverables.**
- `OpaEngine` implementing `PolicyEngine`, embedded (WASM or a sidecar; choose by measured
  latency).
- The 51-case corpus passes on both engines, with a Rego translation of the corpus.
- NFR-1 measured with OPA.

**Acceptance criteria.**
- [ ] Corpus 51/51 on OPA; latency published beside Cedar's.

---

### T-117 — *Spec*: multi-tenancy

**Size** S · **Lane** control plane · **GPU** — · —

**Why.** One deployment serves one organization today. A product sold to several organizations,
or a hosted control plane (the open-core model in `docs/DEMO.md` §3.4), needs tenant isolation
from the database up.

**Read.** `docs/PLAN.md` §7; every table in `db/models.py`; T-075, T-079, T-082.

**Depends on.** T-075, T-079.

**Deliverables.** `docs/specs/13-multi-tenancy.md`: a `tenant_id` on every table; Postgres
row-level security enforcing it; per-tenant root, policy and audit keys; per-tenant Keycloak realm
or organization; PEP-to-tenant binding; what is shared (nothing in the data path) and what is not;
migration from single-tenant.

**Acceptance criteria.**
- [ ] Every table and every Redis channel accounted for.

---

### T-118 — Multi-tenancy implementation

**Size** L · **Lane** control plane · **GPU** — · —

**Why.** Implement T-117. Split further if it exceeds three sittings, recording the split in an ADR.

**Depends on.** T-117.

**Deliverables.** Per the spec, plus a two-tenant e2e test.

**Acceptance criteria.**
- [ ] Tenant A cannot read, list or affect any object of tenant B through any API, even with a
      valid token for A and a guessed id for B (parameterized test over the route table).
- [ ] Row-level security proven: a query without the tenant setting returns nothing.
