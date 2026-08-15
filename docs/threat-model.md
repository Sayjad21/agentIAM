# Threat Model

**Status:** accepted · **Ticket:** T-006 · **Method:** STRIDE
**Depends on:** [`specs/01-token-format.md`](specs/01-token-format.md), [`specs/02-caveat-language.md`](specs/02-caveat-language.md), [`specs/03-attenuation.md`](specs/03-attenuation.md), [`specs/04-lease-protocol.md`](specs/04-lease-protocol.md)

> Every threat below carries a status and the test id that covers it. Statuses are
> **mitigated**, **partially mitigated**, or **accepted risk**, and the accepted ones are
> stated with their bound rather than buried.
>
> A table claiming everything is mitigated is not a threat model, it is marketing. Seven of
> the twenty-five entries here are not fully mitigated, and each says why.

---

## 1. What is being protected

| Asset | Why an attacker wants it | Worst case |
|---|---|---|
| Delegation tokens | They are bearer credentials carrying spend authority | Unauthorized spend within the token's caveats |
| The budget ledger | It is the only authority on money | Mandate ceiling exceeded — the one thing the system exists to prevent |
| The audit ledger | It answers "who authorized this payment?" | Loss of chain of custody; a disputed transaction becomes unresolvable |
| The root signing key | It mints authority from nothing | Total compromise |
| Policy bundles | They gate every decision | Org-wide authorization bypass |
| Principal identities | They are the humans on the hook | Actions attributed to the wrong person |

---

## 2. Trust boundaries

```
   UNTRUSTED                    SEMI-TRUSTED                  TRUSTED
┌──────────────┐            ┌──────────────────┐        ┌─────────────────┐
│ Tool outputs │            │ Agent process    │        │ Control plane   │
│ Task text    │──inject──▶ │ holds a token    │──HTTP─▶│ ledger · policy │
│ Third-party  │            │ may be subverted │        │ audit · issuance│
│ content      │            │ by injection     │        │                 │
└──────────────┘            └──────────────────┘        └─────────────────┘
                                     │                           ▲
                                     │  every tool call          │
                                     ▼                           │
                            ┌──────────────────┐                 │
                            │ PEP              │─────────────────┘
                            │ TRUSTED code,    │  leases, records
                            │ MOST-EXPOSED     │
                            │ position         │
                            └──────────────────┘
```

**B1 — Agent → PEP.** The critical boundary. The agent is assumed potentially subverted:
prompt injection is a *normal input*, not an incident. Everything the agent says about itself
is untrusted.

**B2 — PEP → control plane.** The PEP is our code and is trusted to be correct, but it is the
component most exposed to a compromised agent. Spec 04 G2 (clamping a commit to the lease's
outstanding) exists precisely so that safety does not depend on PEP correctness.

**B3 — Tool output → agent.** Tool results are attacker-controlled content. This is where
injection enters.

**B4 — Human → console.** Authenticated via OIDC. Approval actions are high-value.

---

## 3. Threat register

STRIDE: **S**poofing · **T**ampering · **R**epudiation · **I**nformation disclosure ·
**D**enial of service · **E**levation of privilege.

| ID | STRIDE | Threat | Mitigation | Status | Tests |
|---|---|---|---|---|---|
| TM-01 | S | **Token theft and replay by another agent** | Bearer semantics: a stolen token works until it expires or is revoked. Short TTL (15 min default), fast revocation (NFR-4, < 2 s p99). PoP binding is documented future work | **accepted risk** (§5.1) | A-06, EC-T20 |
| TM-02 | S/E | **Token forgery** — forge a block, strip a block, reorder, splice two chains | Per-block Ed25519 signatures over the preceding chain. Verified: wrong key, single bit flip, and truncation all rejected | mitigated | A-01…A-04, EC-T02, EC-T03, INV-4 |
| TM-03 | E | **Replay of an expired token** | Validity window checked every call; boundary exclusive; verifier supplies `time` as mandatory context | mitigated | A-05, EC-T06, EC-T07, P-08 |
| TM-04 | E | **Privilege escalation via attenuation** — child claims a scope, ceiling, or expiry beyond its parent | Biscuit is append-only and every clause in every block must pass, so authority is an intersection. `narrows()` additionally rejects at mint. Verified: a block injecting `operation`/`scope`/`requested`/`current_depth` facts could not re-grant anything | mitigated | A-10, A-11, A-13, EC-T17…EC-T19, P-01, P-02 |
| TM-05 | E | **Confused deputy** — a low-privilege sub-agent induces a higher-privileged agent to act for it | The higher agent's own caveats still apply, so the action is bounded by *its* authority, not the requester's. The audit chain records who actually acted. **Residual risk: the high-privilege agent may hold authority the requester should not reach** | **partially mitigated** (§5.4) | A-12 |
| TM-06 | T | **Sibling budget race** — three children each holding the full parent ceiling spend concurrently | Static token inspection cannot bound this (INV-5). The ledger does: `SELECT … FOR UPDATE` serializes acquires; shared-pool is the default. Measured: three children requesting 100 against a pool of 150 receive 100 / 50 / 0 | mitigated | A-17, P-10, INV-5, T-017 |
| TM-07 | D | **Lease stranding** — a killed PEP holds budget nobody can spend | Bounded by `max_fraction × available` per PEP, for at most `ttl + S + ttl/4` (80 s with defaults). Graceful-shutdown `RELEASE`; TTL reaper | **partially mitigated** (§5.3) | A-18, CH-3, P-20 |
| TM-08 | T | **Revocation lag** — a revoked token keeps working | Redis pub/sub fast path plus periodic full-set pull as a correctness backstop. Any ancestor block id in the revoked set kills the chain. Target NFR-4: < 2 s p99 across 3 PEPs | mitigated | A-29, CH-2, P-21, T-040 |
| TM-09 | T | **Policy bundle rollback** — replay an older, correctly-signed bundle | Signature verification plus version monotonicity: a bundle with a lower version is rejected even with a valid signature. Staleness guard denies beyond max age | mitigated | A-28, A-16, T-025 |
| TM-10 | E | **Prompt-injection-driven privilege escalation** — injected instructions tell the agent to exfiltrate, spawn a wider sub-agent, or switch tasks | Authority is cryptographic, not linguistic. An injected instruction cannot widen a token: out-of-scope actions are denied by the token, and attenuation makes spawning a wider child impossible. Task redirection triggers drift escalation | mitigated (except TM-11) | A-23, A-24, A-25, A-26 |
| TM-11 | E | **Drift-detector evasion** — 20 small steps, each plausible, cumulatively off-task | A per-action detector will likely miss this. Trajectory-level scoring is future work | **accepted risk** (§5.2) | A-27 |
| TM-12 | T/R | **Audit tampering** — alter, delete, or reorder records | Hash chain: each record binds the previous hash inside the hashed structure, so altering any record changes every hash after it. `verify_audit_chain.py` reports the first inconsistent seq | mitigated | A-30, EC-A01…EC-A04, P-16, NFR-6 |
| TM-13 | I | **PII leakage into logs and decision records** | Decision records carry `arg_digest`, never arguments — enforced by a validator that rejects anything but a 64-char hex digest. Deny reasons cite caveats, never argument values | mitigated | A-33, EC-A12, NFR-5 |
| TM-14 | D | **Control-plane denial of service** — oversized tokens, depth-100 chains, top-up floods, connection-pool exhaustion | Size guard rejects before parsing cost; depth check rejects deep chains; per-PEP top-up rate limiting; graceful 503 that **fails closed**. The hot path holds local leases, so a slow control plane degrades top-ups, not decisions | mitigated | A-08, A-09, A-20, CH-1, CH-11, NFR-7 |
| TM-15 | T | **Currency and numeric confusion** — paisa vs taka, negative or NaN amounts | One canonical unit enforced at the type level: `Decimal` in Python, `NUMERIC(20,4)` in Postgres, scaled integers in token facts. `float` is rejected rather than coerced; negatives rejected by constraint | mitigated | A-21, A-22, T-005 |
| TM-16 | I | **Timing side channel on deny reasons** | Response shape is uniform; a small timing difference between deny paths is measured and reported rather than claimed absent | **partially mitigated** | A-32 |
| TM-17 | T | **Log injection via crafted role names or task text** | Structured logging with escaped fields; role and scope values are validated against a strict pattern at construction | mitigated | A-31, EC-T16 |
| TM-18 | S | **Self-approval and escalation abuse** | An approver may not approve their own escalation; approvals expire; the approved token is verified ⊆ the request; approver identity is audited | mitigated | A-14, A-15, EC-A07…EC-A11, P-22 |

### 3.1 Threats found while building, not present in the original catalogue

These came out of the empirical work in T-002 through T-005 and T-011. They are implementation
hazards rather than adversary capabilities, but each produces a security failure. TM-19…TM-20
still need a test; TM-21 is covered as of T-014; TM-22's reaper-side half is covered as of
T-013; TM-24 has one.

| ID | STRIDE | Threat | Mitigation | Status | Tests |
|---|---|---|---|---|---|
| TM-19 | E | **A caveat that appears to enforce and does not.** Biscuit checks are existential: written against the token's own grant facts, `check if scope($s), [...].contains($s)` passes if *any* granted scope matches, not the one being requested. Measured: narrowing to `invoice:read` still authorized `vendor:read` | Checks are written against verifier-supplied request context only (ADR-005). Spec 01 §2 states the rule normatively | mitigated | **new test needed** (§6) |
| TM-20 | E | **Incomplete request context.** A check whose fact is absent *fails*, so an omitted `requested(dimension)` denies — but a caveat form chosen wrongly can instead fail **open**: `reject if time($t), $t > EXPIRY` is vacuous when the verifier omits `time()`, and the token never expires | `check if` for facts supplied on every request, `reject if` only for legitimately optional facts (ADR-007). `RequestContext` validates at construction that every budget dimension is present | mitigated | **new test needed** (§6) |
| TM-21 | T | **Late commit against a reclaimed lease.** A commit arriving after `RELEASE`/`REAP`/`REVOKE` decrements `leased` a second time for budget already returned. Measured: `leased` went negative in 55 of 400 random interleavings | Commits against a non-active lease are rejected and recorded as reconciliation anomalies (ADR-009). The pool invariant is preserved; the divergence is surfaced | mitigated (§5.5) | `test_ledger_commit.py::test_ledger_commit_rejects_a_released_lease_and_records_an_anomaly`, `::test_ledger_commit_rejects_a_reaped_lease_TM21`, P-10's `LedgerCommit` rule (T-014) |
| TM-22 | T | **Clock skew beyond the configured allowance.** If the reaper reclaims while a lagging PEP still spends, the same budget is issued twice. Measured with no skew margin: the lease was reaped and re-issued while still in use | PEPs expire early at `expires_at − S`; the reaper reclaims late at `expires_at + S`; `ttl > 2S`. **Safety depends on actual skew staying within `S`** | **partially mitigated** (§5.6) | `test_ledger.py` (reaper side, T-013); CH-7, EC-T08 (PEP side) |
| TM-23 | T | **Agent under-reports the actual amount** to hide spend, where the PEP cannot independently determine it | Over-reporting is clamped to the lease's outstanding, so it cannot break the budget invariant. Under-reporting is flagged and audited wherever the PEP can cross-check, but is not prevented | **accepted risk** (§5.7) | A-19 |
| TM-24 | S | **A free-text identifier that reshapes rendered Datalog.** `quote_string()` escapes correctly, so a crafted `role`, `agent_id` or `principal_id` cannot forge a fact inside a signed token. But `block_source()` renders the string back **unescaped**: measured, a role of `x"); admin(true); //` renders as block text that re-parses into a genuine second fact. Every planned consumer of block source is a display or parsing path — the console's caveat chain (T-045), the audit explorer (T-048), the Datalog-to-caveat parser both need. A bidi override additionally reorders a rendered role without changing a byte | `models.validate_label` refuses quotes, backslashes, C0/C1 controls and bidi controls in the three fields that become Datalog string facts, at the only places they enter a token: `Mandate.principal_id` and `attenuate()`'s `agent_id` and `role`. Non-ASCII is otherwise unrestricted, so a Bengali role renders as itself | mitigated | `tests/security/test_datalog_labels.py` (66 cases) |
| TM-25 | D | **A library timeout shorter than the operation it guards.** `biscuit-python` 0.4.0's authorizer defaults to `max_time = 1 millisecond`, and it is **wall clock, not work**. A query taking microseconds raises `AuthorizationError: Reached Datalog execution limits` whenever the process loses the CPU for a millisecond, so a *legitimate* request is refused for want of scheduling — under exactly the load NFR-1's 1 ms budget exists to describe. Measured: a depth-8 chain costs 290 us to authorize quiet and 478 us under 24-way contention, under 2x headroom; and 2 of 42,014 authorize calls during a loaded test run hit the limit, both inside `verify()`. Fault injection confirmed the consequence: 10 of 10 injected timeouts on a parent check produced a false INV-1 violation report (ADR-021) | All limits set explicitly on every authorizer: `max_time=250 ms`, `max_facts=10 000`, `max_iterations=1 000`. Still bounded, so TM-14's ceiling survives; token size (8 192 b64) and depth (8) bound the input the engine can be given. The property harness re-raises the error rather than reading it as a denial | mitigated | `test_tokens.py::TestDatalogExecutionLimits` — pins the library defaults, and drives `MAX_DATALOG_TIME` to zero to prove `verify()` actually applies it |

---

## 4. Assumptions the security rests on

Stating these matters more than the mitigations, because a mitigation is only as good as the
assumption under it. Each is checkable, and each should be re-checked when its dependency
changes.

| # | Assumption | If it fails | How it is checked |
|---|---|---|---|
| A1 | **Biscuit scopes block facts** so a later block's facts are invisible to earlier blocks' checks | INV-1 collapses; any child could widen authority by adding a fact | Verified empirically (spec 01 §6.3). **Re-verify on every `biscuit-python` upgrade** — this is the single load-bearing library behaviour |
| A2 | Ed25519 and SHA-256 are sound | Total compromise | Out of scope; we write no crypto of our own |
| A3 | The root signing key is not exfiltrated | Attacker mints arbitrary authority | Vault in dev; key rotation with an accepted-key set (EC-T05) |
| A4 | Clock skew between PEP and ledger stays within `S` | Overspend within the excess window (TM-22) | CH-7; the ledger can refuse a lease to a badly-skewed PEP |
| A5 | The PEP process is not itself compromised | It holds leases and issues decisions | Partially defended: the ledger clamps commits to the lease's outstanding, so a buggy or malicious PEP cannot break the global budget invariant |
| A6 | Postgres serializes `SELECT … FOR UPDATE` correctly | Concurrent acquires overspend; measured 160 granted against a total of 100 | T-013, 50-concurrent-acquire test against real Postgres |

---

## 5. Accepted and partial risks, with bounds

### 5.1 Bearer token replay (TM-01) — accepted

A token is a bearer credential. Anyone holding it can use it until it expires or is revoked.

**Bound:** 15-minute default TTL; revocation propagates in under 2 s p99 (NFR-4).
**Why accepted:** proof-of-possession binding requires per-agent key management that does not
fit the M1–M7 scope. This is the same trust model as every OAuth deployment in production
today — say that plainly, because it is true and it reframes the question.

### 5.2 Slow-drift evasion (TM-11) — accepted

An adversary who redirects intent gradually enough stays under a per-action threshold.

**Bound:** per-action detection catches abrupt redirection (demo Beat 6); gradual redirection
is not caught. Every action still remains within the token's cryptographic authority, so drift
evasion cannot exceed the mandate — it can only misuse authority the agent legitimately holds.
**Why accepted:** trajectory-level scoring is a genuine open research problem, and naming it is
a better contribution than pretending otherwise.

### 5.3 Stranded lease window (TM-07) — partial

**Bound:** at most `max_fraction × available` per PEP (25% by default), for at most
`ttl + S + ttl/4` = 80 s with default configuration. Measured: a PEP holding 60 of a 100 pool
crashes, available drops to 40, and returns to 100 after the reap.

### 5.4 Confused deputy (TM-05) — partial

The higher-privileged agent's caveats bound the action, and the audit chain records who acted.
What is *not* prevented is a low-privilege agent persuading a high-privilege one to exercise
authority on its behalf, where that authority is legitimately held.

**Bound:** the action cannot exceed the acting agent's own authority, and it is always
attributable. Intent binding narrows the surface, since the induced action must still match the
approved task.

### 5.5 Late-commit divergence (TM-21) — mitigated

A spend that really happened is not recorded against the budget when its lease has already been
reclaimed.

**Bound:** the pool invariant is preserved by construction; each occurrence is recorded as a
reconciliation anomaly with lease id, amount, and terminal state, and surfaced on the budget
dashboard. **Anomaly count must be zero in a clean chaos run** — a non-zero count is a signal,
not noise. AgentIAM emits settlement instructions rather than moving money, so an anomaly is a
reconciliation item, not a lost payment.

Closed in T-014: `ledger_commit()` rejects a commit against any lease not in `active` state and
writes a `ReconciliationAnomalyRow` in the same transaction (`docs/DECISIONS.md` ADR-016). G3's
removal was guard-proofed — the rejection was temporarily disabled and the released-lease and
reaped-lease tests were rerun; both tripped `ck_budgets_invariant` (`leased` driven negative),
the same failure mode §13 of spec 04 measured by simulation, now reproduced against real Postgres
row locking.

### 5.6 Clock skew (TM-22) — partial

**Bound:** safety holds while actual skew ≤ `S` (5 s default on NTP-synced hosts). Beyond that,
overspend is possible within the excess. Checkable: the ledger can compare a PEP's reported
clock at acquire time and refuse the lease.

### 5.7 Agent-reported amounts — accepted

Where the PEP cannot independently determine the true spend, it relies on the agent's report.

**Bound:** over-reporting is clamped to the lease's outstanding (spec 04 G2), so it cannot break
the budget invariant. Under-reporting to hide spend (A-19) is not prevented; the discrepancy is
flagged and audited wherever the PEP can cross-check.

---

## 6. Coverage gaps

Two threats still have no test. Recorded here rather than left implicit, and each is a
concrete addition to T-051's red-team suite.

| Threat | Test to add | Ticket |
|---|---|---|
| TM-19 | A caveat compiled against the token's own grant facts must fail the conformance suite — assert the *wrong* encoding does not enforce | T-008 |
| TM-20 | Authorization with a deliberately incomplete `RequestContext` must deny, never allow. Cover every omitted fact individually | T-019, T-051 |

~~TM-21~~ — closed in T-014. `tests/integration/test_ledger_commit.py`'s
`test_ledger_commit_rejects_a_released_lease_and_records_an_anomaly` and
`test_ledger_commit_rejects_a_reaped_lease_TM21` prove the rejection is load-bearing against real
Postgres: G3 was temporarily removed and both tests were rerun and observed to fail (`leased`
driven negative, caught by `ck_budgets_invariant`), then restored. `test_ledger_properties.py`'s
`LedgerCommit` rule additionally reaches late commits as ordinary interleaving, not only as
hand-constructed cases.

~~TM-22~~ (reaper side) — closed in T-013. `tests/integration/test_ledger.py`'s
`test_reap_does_not_reclaim_within_the_skew_margin` and
`test_reap_reclaims_a_lease_past_the_skew_margin` prove the margin is load-bearing against real
Postgres: the skew-margin cutoff was temporarily removed, the first test was run and observed to
fail (a lease 3 s past `expires_at`, within the 5 s margin, was reclaimed anyway), then restored.
The other half of TM-22 — refusing a lease to a PEP whose reported clock is skewed beyond `S` at
`ACQUIRE` time (spec 04 §17 Q1) — is still open, as is CH-7 (a full chaos scenario).

~~TM-24~~ — closed in T-011. `tests/security/test_datalog_labels.py` covers the guard, the
rejected characters, the Bengali case that must still pass, and the rendering hazard itself, so
the guard is demonstrated to be load-bearing rather than asserted.

`PLAN.md` §12 numbers 33 attacks and the submission scope is 15–20 (`ROADMAP.md` Part 1).
TM-19 through TM-22 and TM-24 should be counted as additions to that set, not substitutions:
they were found by measurement rather than brainstorming, which is usually a sign the remaining
ones are worth looking for the same way. TM-24 in particular came from asking one question of a
running library — *what does `block_source()` do with a quote?* — rather than from reading.

---

## 7. STRIDE coverage

Two threats span categories (TM-02 is spoofing and elevation; TM-12 is tampering and
repudiation), so the rows below overlap rather than partition.

| Category | Threats | Fully mitigated |
|---|---|---|
| Spoofing | TM-01, TM-02, TM-18, TM-24 | 3 of 4 |
| Tampering | TM-06, TM-08, TM-09, TM-12, TM-15, TM-17, TM-21, TM-22, TM-23 | 7 of 9 |
| Repudiation | TM-12 | 1 of 1 |
| Information disclosure | TM-13, TM-16 | 1 of 2 |
| Denial of service | TM-07, TM-14, TM-25 | 2 of 3 |
| Elevation of privilege | TM-02, TM-03, TM-04, TM-05, TM-10, TM-11, TM-19, TM-20 | 6 of 8 |

**25 threats · 18 mitigated · 4 partially mitigated · 3 accepted risks.**

The three accepted risks — bearer replay (TM-01), slow-drift evasion (TM-11), and
agent-reported amounts (TM-23) — are the ones to raise voluntarily in the pitch. Each is real,
each has a bound, and stating them is what makes the other twenty-two believable.
