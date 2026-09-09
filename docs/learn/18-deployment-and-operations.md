# 18 — Deployment and operations

*Feature: running it for real, and the choices that make it safe to run.*

---

## 1. What gets deployed

Six containers, one command.

| Service | Port | What it is |
|---|---|---|
| `controlplane` | 8000 | Ledger, audit, console, all `/v1` APIs |
| `pep` | 8082 | The enforcement proxy |
| `tools` | — | Stub tools, internal only |
| `postgres` | 5433 | Ledger and audit chain |
| `redis` | 6379 | Revocation push channel |
| `keycloak` | 8085 | OIDC, for approver login |

Plus three one-shot jobs that must run in order before the app starts: `bootstrap` (generate
keys, sign the policy bundle), `migrate` (schema), `seed` (create the demo mandate and mint
tokens).

```bash
make demo-up      # build, start, wait for healthy
make demo-seed    # drive the scenario
make demo-down
```

NFR-8 requires that whole sequence to reach healthy in **under 90 seconds**, and a CI job
measures it on every push. A cold-start requirement that nobody measures is an aspiration.

There is also a k3s manifest set under `deploy/k3s/` for a real cluster.

---

## 2. Configuration that refuses to be wrong

The PEP reads everything from the environment, and its settings module has one rule:

> Nothing here has a fallback that would let the service start in a state where it **looks like
> it is enforcing and is not**.

Concretely:

- A missing or malformed root public key is a **boot failure**, not a first-request failure.
  A bad key means no token can ever verify, and finding that out on the first request is
  finding out too late.
- A policy bundle whose signature does not verify is a boot failure. Not "load it anyway and
  warn" — an unverified bundle is an authorization layer anyone with disk access can rewrite.
- Every error message **names the variable**, because the first reader of it is a container log.
- A misconfigured PEP exits with code 2 and says why.

### `/readyz` reports enforcement, derived not declared

```json
{ "status": "ready", "enforcing": true,
  "checks": { "upstream_client": true, "upstream_base_url": "http://tools:8081" } }
```

`enforcing` is **computed from the wiring**. An app built without a pipeline reports `false`
rather than claiming to be safe. That field exists because "the PEP enforces nothing" was once
the single most misleading state in the repository, and a health check that says "ready" while
enforcing nothing is worse than no health check.

---

## 3. Startup and shutdown order, which is not arbitrary

**On the way up**, the PEP draws its first lease immediately. Without it the pool holds nothing,
and the refusal path cannot recover — top-ups are scheduled only from an existing lease. This is
not fatal if it fails: an unreachable ledger at boot is the fail-closed case the PEP already
handles per request, and crash-looping would take out the read-only paths too.

**On the way down**, settlement drains **before** the lease pool releases. A lease retired while
it still owes the ledger is one of the two routes to a double-spend. The order is enforced by a
hook, not by a comment.

---

## 4. Supply chain

The release pipeline builds and pushes an image, signs it with keyless `cosign`, attaches an
SBOM attestation, and verifies its own signature afterwards.

Security scanning runs in CI on every push: static analysis, dependency audit, container scan,
secret scanning, an SBOM check, and a log secret-scanner.

**Two findings from building that** worth carrying elsewhere:

- **The SBOM was platform-dependent.** Generated on Windows it differed from the Linux one, so
  the "SBOM matches its sources" check could never pass in CI. The generator now produces a
  platform-independent document.
- **Waive a scanner finding by value only when the value cannot be spelled better.** Nine
  false-positive secret detections were suppressed — but the rule is that you first try to
  rewrite the code so the scanner is right, and only waive when you cannot (ADR-064).

---

## 5. Observability

Metrics go straight to Prometheus; traces go through an OTEL collector. Grafana dashboards ship
with the repo, because the plan treats observability as **a feature, not instrumentation** — the
dashboards are part of the demo.

One finding here is a good example of a whole class of bug: `decision_span`, the trace span for
an authorization decision, **had never been called from production code** in two milestones. It
existed, it was tested, and nothing in the request path invoked it.

Grepping for non-test callers of a function you believe is wired is a five-second check that has
now found three separate defects in this project.

---

## 6. What went wrong

### A migration switched off the application's logging

`fileConfig` disables every existing logger by default, and an Alembic migration was calling it.
So running migrations turned off the application's own logging (ADR-060).

It surfaced as two log-assertion tests going vacuous when the whole suite ran in one process —
they passed by finding nothing. The test symptom was real, but the cause was a **product** bug.

### A drift check that could never pass

A CI job regenerated the benchmark JSON and then ran a check asserting the committed document
matched it. The check could never pass, because the job had just rewritten its own input.

The fix separates them: the check renders from the **committed** JSON, which is deterministic;
re-running the benchmark is the noisy part and is gated separately (ADR-063).

**A check must not run in a job that regenerates what it checks.**

### The load harness stopped measuring the thing we deploy

The performance numbers are a claim about the **deployed** PEP, but they are measured through a
separate load-test harness. That harness had drifted — it was doing less per-request work than
the real service, so the published figures described something nobody runs.

Now a test asserts the two compose the same way (ADR-062). Any divergence in what they do per
request turns it red.

---

## 7. Operating it day to day

The symptom table lives in [`docs/RUNNING.md`](../RUNNING.md). The three you will actually hit:

| Symptom | Cause | Fix |
|---|---|---|
| Everything is `401 TOKEN_EXPIRED` | The demo mandate expired — 8 hours after seeding | Re-seed |
| Every agent is `401 ANCESTOR_REVOKED` | Something revoked the root block; the subtree cascades | Re-seed |
| A page is blank but its API returns data | The console renders client-side | Check the browser console |

Two commands prove the system is behaving, and both should be on a second screen during a demo:

```bash
python scripts/verify_audit_chain.py           # chain intact: 113 record(s) verified
python scripts/run_invariant_checker.py --once # OK  1 budgets, all invariants hold
```

---

## 8. Known operational gaps

Stated plainly, because a reviewer will ask:

- **One PEP serves one mandate.** The lease pool binds a mandate at construction. Fine for a
  demo; a real multi-tenant deployment needs one process per mandate or a multi-mandate pool.
- **No bundle publisher.** The policy bundle is signed at bootstrap and loaded once at boot, so
  hot reload and the staleness guard are unreachable in the deployment. A bundle changes only by
  restarting.
- **A partitioned PEP cannot shut down gracefully**, and a timeout does not bound it — the
  cancellation lands inside the database driver's greenlet bridge while the socket is dead.
  Measured stuck for five minutes against a five-second bound. Availability, not correctness.

---

## Next

Part 3 begins with [19 — Testing](19-testing.md).
