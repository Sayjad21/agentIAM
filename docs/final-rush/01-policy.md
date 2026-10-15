# 01 — Policy that actually reaches the PEP

**The problem in one line.** `POST /policy/activate` assigns
`DummyBundleStore.current_source` inside the control-plane process
(`agentiam_controlplane/app.py`, `class DummyBundleStore`). The deployed PEP reads a signed
bundle from disk once at boot (`scripts/pep_service.py`, "Policy is a signed bundle read from
disk"), bypassing `PolicyCache` entirely. So activation changes nothing that enforces, and
`POLICY_BUNDLE_STALE` is unreachable in production (gap 26, `research/threat-model.md` N7).

**What already exists and should be reused rather than rebuilt:**
`agentiam_core.bundles` (`PolicyBundle`, `sign_bundle`, `verify_bundle`, canonical-JSON
payload), `agentiam_pep.policy_cache.PolicyCache` (staleness, rollback refusal, hot reload,
rejection counting), `agentiam_pep.activation.activate_bundle` (parse + corpus gate + signature
+ serial), and the revocation distribution pattern — Redis pub/sub push with a pull backstop
(`RedisRevocationPublisher`, `RedisRevocationSet`), which is already proved by A-29 and
`test_revocation_nfr4.py`.

---

### T-070 — *Spec*: policy bundle publication and distribution

**Size** S · **Lane** policy · **GPU** — · **J**

**Why.** Spec-first. Spec 05 §5 defines the bundle and the PEP-side cache, but not who
publishes a bundle, where it is stored, how a PEP learns of it, or who holds the signing key.

**Read.** `docs/specs/05-policy.md` (all); `docs/specs/07-revocation.md` (the push/pull model to
copy); ADR-039; `docs/PLAN.md` §6.5 and §8 Policy; `scripts/pep_service.py` policy section.

**Probe first.** Confirm both halves of the problem against the running stack: activate a
policy in the console that forbids `invoice:read`, then send an `invoice:read` through the PEP
and record that it is still allowed. That transcript goes in the spec's motivation section.

**Deliverables.** A new section in `docs/specs/05-policy.md` (or `05a-policy-distribution.md`)
specifying:
1. **Storage**: a `policy_bundles` table — `serial` (monotonic, assigned by the database),
   `version` label, `cedar_source`, `entity_schema`, `tools`, `created_at`, `created_by`
   (OIDC `sub`), `provenance` (`manual` | `compiled`, plus digest of the NL sentence when
   compiled — never the sentence itself), `test_summary`, `signature`, `state`
   (`draft` | `active` | `superseded` | `refused`).
2. **Signing key**: a policy signing key distinct from the token root key, loaded by the control
   plane from configuration; its public half is what PEPs pin
   (`AGENTIAM_PEP_POLICY_PUBLIC_KEY_HEX`, already used).
3. **Activation**: the gate order — parse, corpus, auto-tests, sign, insert as `active`, mark
   the previous `superseded` — in one transaction. A refused activation leaves the active
   bundle unchanged.
4. **Rollback**: a rollback is a *new* bundle with a higher serial and an older source.
   Serials never go backwards (spec 05 §5.2 stays true).
5. **Distribution**: publish `{serial}` on Redis channel `agentiam:policy`; PEPs fetch
   `GET /v1/policy/bundle/current` on push and on a pull interval `< max_staleness / 3`.
6. **PEP behaviour**: boot-time load from the API with the on-disk bundle as an optional
   first-boot fallback; every subsequent load goes through `PolicyCache.load()`;
   `/readyz` reports serial, age and rejection count.
7. **Failure table**: control plane unreachable, Redis down, bad signature, lower serial,
   unparseable source — each mapped to a behaviour and a reason code.
8. **Open questions**, if any remain.

**Acceptance criteria.**
- [ ] The spec answers every row of the failure table.
- [ ] Reviewed against spec 09's reason codes; no new deny path without a code.
- [ ] ADR recording the separate policy signing key and the push/pull choice.

**Out of scope.** Code.

---

### T-071 — Persisted, signed bundle store and policy API

**Size** L · **Lane** policy · **GPU** — · **J**

**Why.** Replace `DummyBundleStore` with the store T-070 specifies, so the console and the API
operate on durable, signed bundles.

**Read.** The T-070 spec section; `agentiam_core/bundles.py`; `agentiam_pep/activation.py`;
`docs/PLAN.md` §8 Policy; `agentiam_controlplane/app.py` policy routes.

**Depends on.** T-070.

**Probe first.** Sign a bundle with `sign_bundle` and verify it with `verify_bundle` after a
round-trip through Postgres `JSONB`, and after a round-trip through `TEXT`. Record which column
type preserves the canonical payload byte-for-byte; JSONB reorders keys and normalizes numbers,
which may break signatures. Choose the column type from the measurement.

**Deliverables.**
- Alembic migration `0008_policy_bundles.py`.
- `agentiam_controlplane/db/policy_bundles.py`: create draft, run gate, activate (one
  transaction, `SELECT … FOR UPDATE` on the active row), list history, get current.
- `agentiam_controlplane/policy_api.py` implementing `PLAN.md` §8:
  `GET /v1/policy/bundle/current`, `POST /v1/policy/bundles`,
  `POST /v1/policy/bundles/{id}/verify`, `POST /v1/policy/bundles/{id}/activate` (200 | 409),
  `GET /v1/policy/bundles` (history).
- Activation requires an authenticated session; `created_by` is the OIDC `sub`.
- Activation publishes `{serial}` on `agentiam:policy`.
- The console's `/policy/test`, `/policy/compile` and `/policy/activate` handlers rebuilt on
  the store. `DummyBundleStore` deleted.
- Bootstrap: on first start with an empty table, the control plane imports the shipped corpus
  bundle as serial 1.

**Acceptance criteria.**
- [ ] Two concurrent activations produce two distinct serials and exactly one `active` row
      (integration test with real Postgres).
- [ ] An activation whose corpus fails returns 409, names the failing cases, and leaves the
      previous bundle active.
- [ ] `GET /v1/policy/bundle/current` returns a payload whose signature verifies with the
      published public key.
- [ ] A bundle row altered directly in the database fails verification on read (tamper test).
- [ ] `grep -r DummyBundleStore packages scripts` returns nothing.

**Out of scope.** PEP-side consumption (T-072), console history UI (T-073).

---

### T-072 — The PEP syncs bundles; `POLICY_BUNDLE_STALE` becomes reachable

**Size** M · **Lane** policy · **GPU** — · **J**

**Why.** Close gap 26. A deployed PEP must hot-reload policy, refuse rollbacks and forged
bundles, and fail closed when policy is too old — all of which `PolicyCache` already does, but
nothing feeds it.

**Read.** T-070 spec; `agentiam_pep/policy_cache.py`; `agentiam_pep/revocation.py` (the
push/pull loop to copy); `scripts/pep_service.py`; spec 05 §5.3–5.4.

**Depends on.** T-071.

**Probe first.** Start a PEP with `PolicyCache` and `max_staleness = 10 s`, never feed it, and
confirm a request after 10 s is denied with `POLICY_BUNDLE_STALE`. If it is not, that is a bug
to fix in this ticket, before the sync loop.

**Deliverables.**
- `agentiam_pep/policy_sync.py`: subscribes to `agentiam:policy`, fetches the current bundle
  on push and on a pull interval, calls `PolicyCache.load()`. Same lifecycle shape as
  `RedisRevocationSet`.
- `scripts/pep_service.py` wires `PolicyCache` + `PolicySync`; the disk bundle becomes an
  optional first-boot seed.
- `/readyz` and `/metrics` expose bundle serial, age and rejection count.
- `scripts/serve_pep.py` (the benchmark harness) composed the same way, with
  `TestTheHarnessMatchesTheDeployedComposition` extended to cover policy.
- `docs/STATUS.md` gap 26 closed; `research/threat-model.md` N7 updated.

**Acceptance criteria.**
- [ ] End-to-end: activate a policy forbidding `invoice:read`; the next `invoice:read` through
      the PEP is denied within the push latency (target < 1 s), with `POLICY_DENIED`.
- [ ] Redis down: the change still arrives within the pull interval.
- [ ] Control plane down beyond `max_staleness`: requests denied with `POLICY_BUNDLE_STALE`;
      recovery is automatic when it returns.
- [ ] A bundle with a lower serial, or a bad signature, is rejected; the previous one keeps
      serving; the rejection count increases (A-28 on the deployed path).
- [ ] A-16: a request in flight during a reload completes against the bundle it started with.
- [ ] Each guard proven load-bearing by a test that disables it.
- [ ] `make test-integration`, `make test-e2e` green.

**Out of scope.** OPA (T-116).

---

### T-073 — Console policy page on real bundles: history, diff, rollback, provenance

**Size** M · **Lane** control plane / console · **GPU** — · **J**

**Why.** The policy page should show what is actually enforced: which serial is active, when,
by whom, from which source, and what each change did.

**Read.** `console/templates/authoring.html`, `authoring_results.html`, `nl_results.html`;
T-071 API.

**Depends on.** T-071 (T-072 for the "acknowledged by N PEPs" column).

**Deliverables.**
- Policy page: active serial and age; history table (serial, version, author, provenance,
  test summary, state); diff between any two serials; "roll back to this" creating a new
  serial.
- Per-PEP acknowledgement: which serial each PEP reports (from PEP `/readyz` via the control
  plane, or from a heartbeat the PEP posts). Shows the operator the change actually landed.
- Every activation and rollback written to the operator-action audit (T-082 adds roles; this
  ticket writes the records).

**Acceptance criteria.**
- [ ] Rollback creates serial n+1 with serial k's source; the PEP enforces it (e2e test).
- [ ] The page renders with zero bundles, one, and fifty (pagination).
- [ ] No NL sentence text is stored or rendered from the database; only its digest.
