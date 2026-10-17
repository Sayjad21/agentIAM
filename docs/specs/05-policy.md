# Spec 05 — Org Policy and the `PolicyEngine`

**Status:** accepted · **Ticket:** T-024 · **Implements:** `PLAN.md` §6.5, §9 T-024
**Depends on:** [`09-decision-record.md`](09-decision-record.md), [`02-caveat-language.md`](02-caveat-language.md)
**Consumed by:** T-024, T-025, T-026, T-027, T-030

> `PLAN.md` §3.2: **two-layer authorization.** The token's Datalog answers *what did this
> chain of delegation permit?* Cedar answers *what does the organization permit at all,
> regardless of token?* Both must pass, and neither can grant what the other denies.
>
> This document is the second layer's contract. The parts worth checking are §4 — where a
> third decision value that is neither allow nor deny has to be mapped by hand — and §6,
> where the measured cost changes what the project can claim about NFR-1.

---

## 1. Why two layers

A token is portable and offline-verifiable, which is exactly what makes it a poor place to
put organizational rules. A token minted this morning cannot know that at noon the company
stopped allowing external payments over ৳100,000. Revoking and re-minting every token in
flight to express that is not a policy mechanism; it is an outage.

So: caveats are **per-delegation and travel with the token**; Cedar is **org-level, centrally
managed, and evaluated fresh on every request** against a cached bundle.

They compose by intersection. Step 4 (caveats) runs before step 5 (policy) and the first
failing step wins (spec 09 §3), so a token that never granted the scope is reported as
`SCOPE_NOT_GRANTED` rather than as a policy denial — the operator's fix differs.

**Neither layer can widen the other.** Cedar cannot grant a scope the token does not carry,
because step 4 already denied. A caveat cannot override a `forbid`, because step 5 still runs.

---

## 2. The entity model

From `PLAN.md` §6.5, unchanged:

```
Principal:  AgentIAM::Agent   attrs: role, depth, task_id, principal_id
Action:     AgentIAM::Action  "invoice:read", "payment:initiate", …
Resource:   AgentIAM::Tool    attrs: tool_id, server, sensitivity, is_external
Context:    { time, amount, arg_digest, drift_score, elevated, environment }
```

`depth`, `task_id` and `role` change per request, so the principal entity is rebuilt on every
call and the entity set cannot be parsed once and reused.

**Measured**, because the alternative was tempting: moving those facts into `context` and
pre-parsing a static entity set costs **78.5 µs** against **83.0 µs** for rebuilding the
principal each time — under 5%. Cedar's own model treats entities as the durable graph and
context as the request, so the plan's placement is also the idiomatic one, and it is kept. A
5% saving is not a reason to make `principal.depth` unavailable to a policy author who
reasonably expects it.

The **action** namespace is the same scope vocabulary the token uses (`PLAN.md` §2), so a
policy and a caveat name the same operation the same way. That is deliberate: two vocabularies
for one concept is where the two layers would start to disagree.

---

## 3. What the engine is given, and what it returns

```python
class PolicyEngine(Protocol):
    def evaluate(self, context: RequestContext) -> PolicyVerdict: ...
```

The protocol already exists in `agentiam_core.decision` (T-019). This document fixes what an
implementation must do with it.

| Returned | Meaning |
|---|---|
| `allowed=True` | Some `permit` matched and no `forbid` did |
| `allowed=False` | Denied — `statement` names the determining policy |
| `statement` | The policy id Cedar reports as the reason, or `None` when nothing matched |
| `version` | The bundle version, recorded on every decision record |
| `stale` | The cached bundle is older than `max_staleness` (T-025) |

`statement` is not decoration. `PLAN.md` §3.2 principle 4 requires every deny to name its
cause, and spec 09 §4 puts that string in `reason_detail`. **Measured:** `cedarpy` reports
`diagnostics.reasons` as a list of policy ids (`['policy0']`), so this is satisfiable — an
engine that could only answer allow/deny would fail this spec.

---

## 4. Three decision values, and the one that is a trap

**Measured:** `cedarpy.Decision` has **three** members — `Allow`, `Deny`, and `NoDecision`.
`NoDecision` is returned when the policy set fails to parse, with the parse errors in
`diagnostics.errors`.

```python
r = cedarpy.is_authorized(request, "this is not cedar", entities)
r.decision            # Decision.NoDecision
r.decision is Decision.Deny   # False
r.diagnostics.errors  # ['policy parse errors:\nunexpected token `is`']
```

So the obvious implementation is wrong:

```python
if response.decision == Decision.Deny:   # WRONG — NoDecision falls through as "not denied"
    return PolicyVerdict(allowed=False)
return PolicyVerdict(allowed=True)
```

**An implementation MUST treat anything that is not `Allow` as a denial.** Written as
`allowed = response.decision is Decision.Allow`, so a fourth member added by a future Cedar
release also fails closed rather than silently permitting. This is the whole reason this
section exists: the failure mode of the natural spelling is a policy layer that stops
enforcing the moment its bundle is corrupt.

An empty policy set — a syntactically valid bundle with no statements — returns `Deny` with
no reasons, which is correct: **deny by default**, confirmed by measurement rather than
assumed.

---

## 5. The bundle

```
{serial, version, cedar_source, entity_schema, created_at, tools, signature}
```

T-024 consumed `version`, `cedar_source` and optionally `entity_schema`. Signature
verification, staleness, rollback and hot reload are **T-025** and are specified in
§5.1–§5.4 below. `tools` is §5.6.

**A bundle is parsed once, at load, not per request** — see §6. A bundle whose source does not
parse MUST be rejected at load rather than at the first request that touches it. `cedarpy`
supports this deliberately: `format_policies()` raises `ValueError: cannot parse input
policies` on bad source, and `PolicySet.from_str()` is the parse step itself.

Rejecting at load is what keeps §4's `NoDecision` an impossible state in production rather
than merely a handled one.

### 5.1 What is signed, and how

**Ed25519, detached, over the bundle's canonical JSON** — `hashing.canonical_json` from T-005,
the same canonicalization the audit chain uses. That reuse is the point: two serializations of
one bundle must produce one signature, or re-encoding a bundle in transit breaks it.

The signed payload is every field **except** the signature itself:

```
{serial, version, cedar_source, entity_schema, created_at, tools}
```

`tools` is inside it for the reason §5.6 gives: it carries authorization inputs, not metadata.
An absent catalogue (`null`) and an empty one (`{}`) sign differently and must, because they
are different assertions.

**Measured**, against `cryptography` 50:

| | |
|---|---|
| Signature | 64 bytes |
| Public key, raw | 32 bytes — 64 hex characters, which is what an operator pastes into config |
| Determinism | Ed25519 is deterministic: the same bundle and key always produce the same signature |
| A bad signature | **raises `InvalidSignature`**; it does not return `False` |

That last row shapes the API. A library returning a boolean invites `if verify(...)` being
written where `if not verify(...)` was meant, and the failure of that typo is *accepting every
bundle*. Verification here raises, and the caller cannot ignore it by accident.

Four tamper shapes were checked and all four raise: a flipped signature bit, an altered
payload, an empty signature, and a signature from a different key.

### 5.2 Rollback: the serial, not the label

`version` is a **label** — `"2026-08-15.3"`, whatever an operator finds readable. It is what
lands in `DecisionRecord.policy_version`, and it is *not* what rollback protection compares.

`serial` is a monotonically increasing integer, and the cache **refuses any bundle whose serial
is not greater than the one it currently holds** — even when the signature is perfectly valid.
That is the rollback attack: an attacker who captures an old, legitimately-signed bundle replays
it to restore a permission that has since been removed. Signature verification alone cannot see
anything wrong with it.

Two fields for what looks like one concept, deliberately. String labels do not order —
`"bundle-10" < "bundle-9"` lexicographically — and a rollback defence that depends on how an
operator names things is not a defence.

### 5.3 Staleness, and what happens on a bad bundle

| Situation | Behaviour |
|---|---|
| Bundle not **confirmed current** within `max_staleness` (default 300 s) | `stale=True`, and spec 09 denies with `POLICY_BUNDLE_STALE` |
| A new bundle fails signature verification | **Rejected; the cache keeps serving the previous bundle** and the failure is counted |
| A new bundle has a serial ≤ the current one | Rejected the same way |
| No bundle has ever loaded | `OracleUnavailable` → `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED` |

The second row is `PLAN.md` §11's EC-P01, and it is the one that looks inconsistent. Everything
else here fails closed; a bad bundle does not. The reason is that the alternatives are worse: a
forged bundle is *evidence of an attack in progress*, and responding by discarding the last known
good policy would let an attacker disable the policy layer by sending garbage. Keeping the
previous bundle means the attacker achieves nothing, and the staleness clock keeps running
underneath — so if the real bundle never arrives, the PEP fails closed on age anyway.

**Rejection is never silent.** The cache counts rejections and `/readyz` exposes them; a bundle
being refused repeatedly is the signal EC-P01 asks to alert on.

**Age is measured from the last confirmation, not from the load** (amended by APICTA-01). The
question staleness answers is *"how long since this PEP last knew it holds the current
policy?"*, and a policy that has not changed is not out of date. Measured before the
amendment: a cache loaded at *t* reported `stale=True` at *t* + 301 s with the policy unchanged
and the control plane healthy, and re-loading the same bundle to refresh it was refused as a
rollback (`serial` must advance, §5.2). Wired as it stood, every PEP would have refused every
request five minutes after boot. A successful sync that finds the PEP's serial still current
therefore **confirms** it (§5.7.4), which restarts the clock without loading anything.

### 5.4 Hot reload

Loading a new bundle parses and verifies it **before** anything is swapped, then replaces one
reference. In-flight requests hold the engine they started with and finish against it; the next
request picks up the new one. There is no lock on the read path, because there is nothing to
lock — a Python attribute rebind is atomic under the GIL, and a request that began under bundle
*n* completing under bundle *n* is the correct outcome rather than a compromise.

A bundle that fails to parse or verify never becomes the current one, so a request can never
observe a half-loaded bundle.

### 5.5 The activation gate (T-026)

`PolicyCache.load` is the fast path for hot reload — the cache reads a signed bundle from
distribution and applies it. It runs three safety checks (signature, monotonic serial, parse)
before swapping the reference.

`activate_bundle` is the operator-initiated slow path: it runs the same three verification
gates, but adds a fourth: **a policy test corpus**. A candidate bundle must pass a corpus of
≥50 request/expectation pairs derived from the demo workflows. If any test fails, the bundle
is refused with an `ActivationFailed` error (mapped to HTTP 409), and the existing bundle
keeps serving. This provides a hard correctness floor: an operator cannot deploy a policy
that accidentally blocks standard workflow components.

An empty corpus passes vacuously — an operator deploying the initial system without tests
sees a warning, but is not blocked.


---

### 5.6 The resource catalogue travels inside the bundle

A Cedar policy reads attributes off the resource it is asked about — the shipped corpus has
two such rules:

```cedar
forbid(principal, action, resource)
when { resource.sensitivity == "critical" && principal.role != "senior" };

permit(principal, action == Action::"email:send", resource)
when { !resource.is_external };
```

Those attributes come from a **tool catalogue**, `{tool_id: {server, sensitivity,
is_external}}`, and a request naming a tool the catalogue does not hold resolves to the safe
end of every axis (`sensitivity: "low"`, `is_external: false`) so an unknown tool cannot
satisfy a policy written about a sensitive one.

**The catalogue is part of the bundle and inside its signature.** Two reasons, and the first
is a security property:

1. `sensitivity` is an **authorization input**. A catalogue held in its own unsigned file is an
   authorization layer anyone with disk access can rewrite: downgrade one tool from `critical`
   to `low` and the forbid above is disarmed *silently*, because every request still returns a
   decision. That is the same threat §5.1's signature closes for `cedar_source`, and it is not
   weaker for being about an attribute rather than a rule.
2. A policy and the attributes it reads cannot arrive out of step, because **one serial names
   both**. A bundle that references `resource.sensitivity` and a catalogue that defines it are
   one versioned artifact, not two that a deployment has to keep aligned.

A PEP MUST take its catalogue from the bundle it verified unless a caller supplies one
explicitly, and MUST refuse a malformed entry at load rather than at request time — the
failure otherwise is a PEP that starts, looks like it is enforcing resource attributes, and is
not. That failure is not hypothetical: it is what the deployed PEP did until ADR-069, and
every conformance case passed throughout because each one builds its own catalogue by hand.

A misspelled attribute is refused rather than ignored. Splatting an entry into the facts type
would raise on some misspellings by luck and silently drop others, and a dropped `is_external`
is the difference between a permit that fires and one that does not.


### 5.7 Publication and distribution (APICTA-01)

Everything above describes a PEP that *receives* bundles. Until APICTA-01 nothing sent one:
`POST /policy/activate` ran the corpus gate and assigned the source to an in-memory stub in the
console process, and the PEP verified one bundle from disk at boot and never saw another
(`STATUS.md` gap 26). This section is the publishing half.

#### 5.7.1 Who may activate

**Activation requires a signed-in approver** — a session principal in `approvers`, the rule
`POST /v1/escalations/{id}/approve` and `POST /v1/revocations` already apply. Measured before
APICTA-01: `POST /policy/activate` with no session at all answered 200 and activated. That was
harmless while activation changed only a stub; once it reaches every PEP it is the most
powerful action in the system, and it must not be anonymous.

| Request | Answer |
|---|---|
| No session | 401, nothing published |
| A session not in `approvers` | 403, nothing published |
| An approver, a policy failing the corpus | 409 (§5.5), nothing published |
| An approver, on a control plane without a signing key | 503, nothing published |
| An approver, a policy passing | the bundle is published (§5.7.3) and the response names its serial |

The session cookie is `SameSite=Lax`, so a cross-site form cannot activate on an approver's
behalf. **Every refusal is shown.** HTMX 1.9 swaps only 2xx/3xx responses into the page, so
until APICTA-01 the console's 409 explanation of a failing corpus was received and discarded —
the button appeared to do nothing. The policy page now opts 400/401/403/404/409/503 back in.

#### 5.7.2 The signing key

**A dedicated Ed25519 bundle-signing key, separate from the root token key.** The control plane
holds the private half (`AGENTIAM_CONTROLPLANE_BUNDLE_SIGNING_KEY_PATH`, a 64-hex-character file,
mode `0600`); each PEP holds only the public half, as it already does
(`AGENTIAM_PEP_POLICY_PUBLIC_KEY`). `scripts/bootstrap_demo_secrets.py` generates the pair and
must persist the private half — until now it signed the initial bundle with a key it then
discarded, which is why no second bundle could ever be signed.

Separation bounds what a stolen key does. The root key mints tokens; the bundle key can only
change organization policy. And policy is the *second* of two layers (§1): a token's own scopes
and caveats are evaluated before Cedar and cannot be widened by any bundle. A forged bundle can
therefore at worst remove the organization's restrictions — it cannot grant an agent more than
its token already holds.

#### 5.7.3 The bundle store and serial allocation

```sql
CREATE TABLE policy_bundles (
  serial        BIGINT PRIMARY KEY,                  -- §5.2: monotonic, compared by every PEP
  version       TEXT NOT NULL,                       -- a label: "bundle-<serial>" by default
  cedar_source  TEXT NOT NULL,
  tools         JSONB,                               -- §5.6; null and {} sign differently
  created_at    TIMESTAMPTZ NULL,                    -- inside the signature; NULL only for an
                                                     -- imported bundle signed without one
  signature     BYTEA NOT NULL,                      -- 64 bytes, Ed25519, §5.1
  activated_by  TEXT NOT NULL,                       -- the session principal, never a body field
  activated_at  TIMESTAMPTZ NOT NULL,                -- when it was stored (provenance, not signed)
  source_serial BIGINT NULL,                         -- set on a rollback: the serial re-published
  nl_digest     TEXT NULL                            -- sha256[:16] of the English, never the text
);
CREATE SEQUENCE policy_bundle_serial;
```

- **The serial comes from a sequence, before signing.** The signature covers the serial, so it
  must be known first: `nextval`, build, sign, insert, in one transaction. Two concurrent
  activations get two serials and the later one wins at every PEP, which is the intended
  outcome; a failed activation leaves a gap, which §5.2 does not care about.
- **The catalogue is carried forward.** An activation supplies Cedar source; the new bundle's
  `tools` is the current bundle's, unchanged. A policy and its catalogue stay one artifact
  (§5.6) without the console having to edit both.
- **Rollback publishes the old source as a new serial.** Re-sending serial *n* would be refused
  by every PEP as a rollback attack (§5.2), correctly. "Roll back to *n*" means publish serial
  *m* > current with *n*'s source and catalogue, `source_serial = n`.
- **The initial bundle is imported.** At startup, if the table is empty, the control plane
  imports the deployment's signed bundle file (the one bootstrap wrote and every PEP boots
  from) as its first row, verifying its signature first, and advances the sequence past it.
- **The English is never stored.** `nl_digest` is the same `sha256[:16]` digest
  `nl_compiler/compiler.py` logs (T-054); `tests/security/test_secret_scanning.py` already
  forbids the text itself in logs.

**One format on disk and on the wire, one reader.** The boot bundle file is
`agentiam_core.bundles.bundle_file_payload` — the wire form without its signature, which stays
in its own `.sig` file — and both the PEP (`pep_service.load_policy`) and the control plane's
importer read it with the same strict `bundle_from_file`. Found while building this: the PEP had
rebuilt the bundle field by field and omitted `created_at`, so the moment bootstrap began
signing a timestamp the PEP would have verified a different bundle and refused to boot. A
volume bootstrapped before APICTA-01 lacks only the signing key; bootstrap replaces just the
policy half (new key, boot bundle re-signed, public half rewritten) and keeps the root key, so
the tokens already minted stay valid.

Two read routes serve PEPs and the console, unauthenticated like `GET /v1/revocations`:
`GET /v1/policy/bundles/current` and `GET /v1/policy/bundles/{serial}`, each returning
`bundle_to_wire`'s fields — `created_at` as ISO-8601 with its offset, the signature in base64 —
plus an unsigned `provenance` object (`activated_by`, `source_serial`, `nl_digest`) that a PEP
ignores. **Bundles are integrity-protected, not confidential**: anyone who can reach the control
plane can read the policy, as anyone can read the revocation list. Measured: the signature
survives a JSON round trip with a timezone-aware `created_at`, and a naive one fails
canonicalization (`CanonicalizationError`) rather than verifying, so a transport bug fails
closed.

#### 5.7.4 How a PEP stays current

The hot path only ever reads the cache (§5.4). Everything here is background work in the PEP's
lifespan, in the shape `RedisRevocationSet` already uses (spec 07 §5):

1. **Push.** After its transaction commits, the control plane publishes
   `{"event": "policy:reload", "serial": <n>}` on the Redis channel `agentiam:policy`. A PEP
   receiving it fetches `GET /v1/policy/bundles/current` — *current*, not *n*, so events that
   arrive out of order or are coalesced still converge on the newest — and calls
   `PolicyCache.load()`.
2. **Pull backstop.** Every `AGENTIAM_PEP_POLICY_PULL_INTERVAL_S` (default 30 s) the PEP
   fetches the current bundle regardless. Redis pub/sub is at-most-once: a PEP that was
   restarting or partitioned when the event fired would otherwise miss it for good.
3. **Confirmation.** A fetch whose serial equals the one held calls
   `PolicyCache.confirm(serial)`, which restarts the staleness clock (§5.3 amendment). A fetch
   with a lower serial is ignored; a fetch that fails changes nothing, and the clock runs on.
4. **Boot.** A PEP loads the bundle file it was given, as today, then attempts one pull before
   it reports ready. If the control plane is unreachable it serves the file's bundle, which goes
   stale after `max_staleness` unless a sync confirms or replaces it — fail-closed on age, as
   §5.3 intends.
5. **A bad bundle is counted and refused** (§5.3, EC-P01): the previous one keeps serving.

`max_staleness` (default 300 s) against a 30 s pull leaves roughly ten failed syncs before a PEP
refuses on age. `/readyz` reports `PolicyCache.status()`: serial, loads, rejections, staleness.

#### 5.7.5 What the cache must change to be wired

Measured against `PolicyCache` as T-025 left it, and each a precondition for §5.7.4. All three
are done (APICTA-01 Part C), each pinned by a test:

| # | Finding | Required change |
|---|---|---|
| 1 | `__init__` stores `dict(tools or {})` and passes it to `CedarEngine`; an explicit `{}` overrides the bundle's own catalogue (§5.6). Measured: 5 catalogue entries directly, **0** through the cache | Pass `tools` through as `None` when not given |
| 2 | No `.bundle`; `Pipeline` reads `policy.bundle.version` for every decision record | Expose the current bundle, or the unavailable placeholder |
| 3 | No way to mark an unchanged bundle as still current (§5.3 amendment) | `confirm(serial)` |

**A fourth, found wiring it:** the pipeline read `policy.bundle.version` when it wrote the
record, after awaiting an escalation or a drift score — so a bundle loaded during that await
was credited with a decision made under its predecessor. The engine and the label are now read
together, with no await between them, and a test loads a bundle mid-request to prove it.

#### 5.7.6 Latency

The target: a policy activated in the console is enforced by every connected PEP within **one
second** by push, within one pull interval without it. Measured:

| | |
|---|---|
| Published → PEP loaded, push, real Redis (integration test) | **2.4 ms** |
| Published → PEP loaded, pull-only at a 0.2 s interval | 196 ms |
| Console click → running PEP refuses, demo stack (`agt-payer` quarantined) | **42 ms** (31 ms of it the activation request, corpus gate included) |
| One load on the event loop (verify + parse, 51-case corpus bundle) | 0.34 ms median, 0.37 ms p99 |

A load costs the hot path one bundle parse, once per change. A confirmation costs one signature
check (~0.2 ms) per pull.


### 5.8 From an English sentence to an activatable policy (APICTA-01 Part D)

**A compiled sentence is an update of the current policy, not a policy of its own.** Measured
before: the compiler returned only the rule a sentence described, the console tested that rule
alone against the corpus, and every one scored 27/51 — no English sentence could ever be
activated. Added to the current policy, the same rules behave as intended: a quarantine or a new
permit scores 51/51, and a rule that would break an approved workflow is refused with the cases
it breaks (a ৳100,000 payment cap names `beat4_payment_at_ceiling`).

| Compiler | Given the current policy, it returns |
|---|---|
| The model | The **whole updated policy**: every statement it does not deliberately change, plus the new ones, and no blanket `permit(principal, action, resource)`. Without a current policy (the 30-case dataset) the prompt is byte-for-byte the one that scored 27/30 |
| The offline templates (`nl_compiler/templates.py`) | The current policy with the sentence's statements **appended** under a `// added by the offline compiler:` label |

Measured on Gemini (`gemini-3.5-flash-lite`), six sentences against the real corpus policy: all
six compiled; the four compatible ones kept all 7 existing statements and passed 51/51; the two
that conflict with approved workflows were refused naming the cases. Live on the demo stack:
model compile 2.4 s, offline compile 0.1 s, and the running PEP enforcing either ~16 ms after
activation.

**The offline compiler** answers when the model is unreachable, takes longer than the console's
bound (`AGENTIAM_CONTROLPLANE_COMPILE_TIMEOUT_S`, default 20 s — about 8× the measured 2.6 s
median), returns an answer not in the expected shape, or is switched off (the console's
*offline* box, or `AGENTIAM_CONTROLPLANE_COMPILER_OFFLINE`). It understands six shapes:
quarantine an agent, cap a payment, cap a payment unless approved, restrict an action to one
role, limit an action by delegation depth, block external email. Anything else gets that list,
never a guess. Its output is labelled, and the label is set by the compiler after parsing — it is
not in the schema the model sees, so the model cannot claim it.

**Testing a rule about one agent.** `PolicyTestCase.agent_id` is now the principal the console's
evaluator asks Cedar about. It used to be a hardcoded `agent-1`, which made a rule naming an
agent untestable by the gate; no corpus case names an agent, so the corpus is unchanged.

---

## 6. Cost, and what it does to NFR-1's headroom

**Measured**, per authorize, on the development host:

| Arrangement | Cost |
|---|---|
| Policy source re-parsed on every call | 167.7 µs |
| `PolicySet.from_str` once, entities per request | **80.1 µs** |
| Both pre-parsed (not reachable — §2) | 61.7 µs |

**Pre-parsing the policy set is mandatory**, not an optimisation: it is a 2× difference, and
the naive spelling puts 17% of NFR-1's entire budget into one step.

The honest consequence, which supersedes a claim made in T-019:

> T-019 measured `decide()` at **~5.2 µs** and recorded *"about 200× inside the 1 ms budget…
> `PLAN.md` §17 R-2 can be considered closed rather than pending."*

That measurement excluded step 5, because no policy engine existed. With Cedar the decision
costs roughly **85 µs**, so the headroom is about **12×**, not 200×. R-2 (*p99 over 2 ms by M8
triggers a port to Rust*) is therefore **not closed** — it is comfortable, on one machine,
with a small bundle. It should be re-measured under T-053's load profile with a realistic
bundle, because the one variable this spec cannot bound is how large a policy set an operator
writes.

The engine is still in-process and makes no network call, which is the property `PLAN.md` §3.2
actually requires of the hot path.

---

## 7. Two engines behind one protocol

`CedarEngine` is the implementation. `OpaEngine` exists as a stub raising
`NotImplementedError`.

The stub is not padding. `PLAN.md` §6.5 wants the abstraction demonstrated rather than
asserted, and a protocol with exactly one implementation is a protocol nobody has tested the
shape of. The stub costs a few lines and proves the seam is real — an OPA sidecar is an
out-of-process call, so an implementation that could not express that would be an abstraction
over nothing.

Full OPA is deferred (`PLAN.md` §21).

---

## 8. Known limitations

| # | Limitation | Bound |
|---|---|---|
| 1 | No schema validation of the bundle by default | Cedar validates policies against an `entity_schema` when one is supplied; T-026's activation gate is where that becomes mandatory. Without it, a typo in an entity type is a policy that silently never matches — and never matching means never permitting, so it fails closed |
| 2 | Cost is measured with a small bundle | §6. A large policy set costs more; T-053 measures a realistic one |
| 3 | `arg_digest` is in context, the arguments are not | A policy cannot say *deny payments to account X* unless the extractor maps that argument (spec 10 §4). Deliberate: NFR-5 keeps argument values out of everything that is recorded |
| 4 | One bundle per PEP, no per-tenant bundles | Single-tenant is the scope (`PLAN.md` §1.4) |
| 5 | Bundles are readable by anyone who can reach the control plane | §5.7.3: integrity-protected, not confidential. A policy that must stay secret needs authenticated reads |
| 6 | One approver activates; there is no second-person rule for policy | §5.7.1. Separation of duties for activation would mirror escalation's; not built |

---

## 9. Open questions

| # | Question | Owner |
|---|---|---|
| ~~1~~ | ~~Whether the bundle should be validated against `entity_schema` at load, or only at activation~~ — **resolved in T-026: at activation.** `load()` is the hot path for the PEP cache, where tests have already run; `activate_bundle` is the operator-initiated gate. Forcing tests on the hot path creates a failure mode that breaks the PEP pulling updates, rather than breaking the operator pushing them. | done |
| ~~2~~ | ~~Whether `stale` should deny immediately or serve a grace window with a flag~~ — **resolved in T-025: deny immediately.** A grace window is a second staleness limit with a friendlier name, and it makes the failure mode *policy silently out of date* rather than *policy refused*. The operator's fix is the same either way — refresh the bundle — and `max_staleness` is already the knob for how much staleness is tolerable. Setting it to 600 s is the grace window, stated once | done |
| 3 | Whether policy evaluation should be cached by `(scope, tool, principal, rounded amount)` — worth it only if T-053 shows step 5 dominating | T-053 |
