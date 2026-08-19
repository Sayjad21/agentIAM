# Deployment

Three ways to run AgentIAM, in increasing order of how much of the real system they show,
and the rollback procedure for the two that matter operationally.

| | Brings up | Use for |
|---|---|---|
| `make up` | Postgres, Redis, Keycloak | Local development against real infrastructure |
| `make demo-up` | The above + the built image, three ways (bootstrap, migrate, controlplane, pep, tools) | The one-command enforcement demo (NFR-8) |
| `kubectl apply -k deploy/k3s/` | The same deployable shape on a real orchestrator | Showing this runs outside Compose |

`deploy/k3s/README.md` covers bring-up, secret handling, and what is/is not proven for
the k3s manifests specifically. This document covers what neither of those does: image
provenance, and rollback.

---

## 1. Image provenance

`.github/workflows/release.yml` runs on `git push --tags` of a `v*` tag — never on an
ordinary push to `main` — and:

1. Builds the single image (`Dockerfile`, ADR-056 §5.1 — one image, three entrypoints
   selected by `command:`) and pushes it to `ghcr.io/sayjad21/agentiam` tagged both
   `vX.Y.Z` and `latest`. **Hardcoded, not `${{ github.repository }}`**: Docker/OCI image
   references must be lowercase, this repository's owner is `Sayjad21` (mixed case), and
   GitHub Actions' expression syntax has no `toLower()` — checked against GitHub's own
   documented function list rather than assumed, since guessing one wrong here would have
   shipped a release job that fails on every push with "invalid reference format."
2. Signs it **keylessly** with `cosign sign`: the job exchanges its short-lived GitHub
   OIDC token for a Fulcio certificate and logs the signature to the public Rekor
   transparency log. No private signing key is generated, stored, or rotated anywhere in
   this repository or its secrets — the identity that did the signing is
   `https://github.com/Sayjad21/agentIAM/.github/workflows/release.yml@<ref>`, which
   `cosign verify` checks against, not a key anyone has to protect. **Note the case
   difference from the image path above**: the certificate identity comes from the OIDC
   token's `repository` claim, which carries the repository's real stored casing
   (`Sayjad21/agentIAM`, matching `git remote -v`), not the lowercased image reference —
   they are two different strings checked by two different mechanisms, and the workflow
   itself builds this one from the live `${{ github.repository }}` context value rather
   than hardcoding it, so it is always correct regardless of this document.
3. Attests the SBOM: `cosign attest --predicate docs/evidence/sbom.json --type
   cyclonedx` against the same digest. The attested predicate is the same CycloneDX file
   T-054 already produces and a judge can already open — not a second SBOM invented for
   this workflow.
4. **Verifies its own output in the same job**, immediately: `cosign verify` and `cosign
   verify-attestation` against the digest just pushed, using the identity above. A broken
   keyless setup fails the release rather than shipping a signature nobody can actually
   check.

A consumer verifies the same way, after `cosign` is installed:

```bash
IMAGE="ghcr.io/sayjad21/agentiam@sha256:<digest>"

cosign verify \
  --certificate-identity-regexp "^https://github.com/Sayjad21/agentIAM/.github/workflows/release.yml@.*$" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  "$IMAGE"

cosign verify-attestation \
  --type cyclonedx \
  --certificate-identity-regexp "^https://github.com/Sayjad21/agentIAM/.github/workflows/release.yml@.*$" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  "$IMAGE" | jq -r '.payload' | base64 -d | jq '.predicate'
```

**What this is not.** The workflow's YAML was parsed and its action version pins
(`docker/login-action@v4`, `docker/setup-buildx-action@v4`, `docker/build-push-action@v7`,
`sigstore/cosign-installer@v4.1.2`) were checked against the real GitHub API before being
written down — the same discipline that caught the missing `v` prefix on `trivy-action` in
T-055 — rather than guessed. It has not been exercised end to end: doing that needs a real
`vX.Y.Z` tag pushed to the real repository, which publishes a public package under the
project's real identity and is exactly the class of visible, hard-to-reverse action this
project's own working rules hold back for explicit confirmation before taking. State that
plainly rather than claim a measurement that was not made. A local dry run of the `cosign`
CLI syntax was also attempted and could not complete — this sandbox's network egress could
not reach GitHub's release-asset host to install the binary — so the flag syntax
(`--predicate`, `--type cyclonedx`, `--certificate-identity`, `--certificate-oidc-issuer`)
rests on cosign's documented stable interface, not a local invocation.

Using kustomize to point at a published image, once one exists:

```bash
kubectl kustomize deploy/k3s/ | \
  sed 's|agentiam:latest|ghcr.io/sayjad21/agentiam:vX.Y.Z|' | \
  kubectl apply -f -
```

---

## 2. Rollback

Two things can need rolling back independently: the **image** (application code) and the
**schema** (Alembic revision). They are not always the same operation.

### 2.1 Image rollback

```bash
# Compose — docker-compose.demo.yml always `build:`s from the checked-out source
# (verified: no service in it has an `image:` line), so a compose rollback is a source
# rollback, not an image-tag swap.
git checkout v1.2.2 -- Dockerfile packages/ scripts/
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build

# k3s — this is the one that actually swaps a published, signed image by tag.
kubectl set image deployment/controlplane controlplane=ghcr.io/sayjad21/agentiam:v1.2.2 -n agentiam
kubectl set image deployment/pep pep=ghcr.io/sayjad21/agentiam:v1.2.2 -n agentiam
kubectl rollout status deployment/controlplane deployment/pep -n agentiam
```

**Is it safe to roll the image back without touching the schema?** Only when nothing
between the two versions changed what a query means, not just what columns exist. Every
migration under `packages/agentiam-controlplane/.../migrations/versions/` adds columns or
tables — additive at the DDL level — but `0004_budget_split.py` is the counter-example
worth naming explicitly: it does not just add the `allocated` column, it **changes the
pool invariant** old code enforces (`committed + leased <= total` becomes `committed +
leased + allocated <= total`). An image rolled back to before 0004, run against a database
still at 0004 or later, would compute the older, narrower invariant and could under-count
committed capacity if any allocation rows exist by then. **Check what the target
migration's own docstring says before assuming an image-only rollback is safe** — this
project's migrations document exactly this when it applies (0004's docstring is explicit),
rather than leaving it for the next reader to work out from the diff.

### 2.2 Schema rollback

```bash
kubectl run migrate-rollback -n agentiam --rm -i --restart=Never \
  --image=ghcr.io/sayjad21/agentiam:v1.2.2 \
  --command -- sh -c \
  "cd /app/packages/agentiam-controlplane && alembic downgrade <target-revision>"
```

`downgrade()` is implemented for every migration (0001 through 0007) — this is not a gap.
What it is not, is safe by default: `0004`'s `downgrade()` drops the `allocated`,
`parent_budget_id` and split-tracking columns outright, and its own docstring states the
consequence plainly — **"this destroys data, and there is no version of it that does
not"** — any allocation row, its leases, its settled reservations, and any reconciliation
anomaly referencing it is gone, not archived. Back up (`pg_dump`) before downgrading past
any revision whose docstring says the same.

### 2.3 The part that is not obvious: a rollback's own shutdown can hang

Both rollback paths above stop old pods (`kubectl set image` triggers a rolling update;
`docker compose up -d` recreates changed services) and every stop sends the PEP a
`SIGTERM`. **T-052's chaos suite (CH-4, ADR-050) measured that this can go badly under a
Postgres partition specifically, and the finding governs how this rollback is executed,
not just how it is described:**

`LeasePool.aclose()` drains in-flight top-ups and releases every held lease, and both need
the ledger. Under a partition, the `asyncio.wait_for(..., timeout=5)` meant to bound that
does not: the cancellation lands inside SQLAlchemy's greenlet bridge while `asyncpg` is
blocked writing to a dead socket, and the driver's own rollback-then-close cleanup needs
that same socket. `tests/chaos/test_ch04_partition.py::test_a_partitioned_pep_cannot_shut_down_gracefully`
measured this stuck for five minutes against a five-second bound, and only stops if the
partition heals.

**Consequence for this runbook, not just for CH-4's test file:** do not run a rollback
while the target's Postgres is known to be unreachable, and do not assume `kubectl
rollout status` completing on schedule means every old pod exited cleanly. Set
`terminationGracePeriodSeconds` on `controlplane`/`pep` short enough that Kubernetes
`SIGKILL`s a pod stuck in this state rather than blocking the rollout indefinitely — the
default (30 s) is already short of the measured five-minute hang, so the manifests'
default is correct as shipped; do not raise it "to be safe" without re-reading this
section, since a longer grace period only makes a stuck rollout take longer to resolve
without protecting anything.

**A `SIGKILL` mid-rollback does not lose money — but does not automatically get it back
either, and this project's own record of that fact turned out to be wrong.** CH-4's own
docstring reads *"the money is safe either way ... what is lost is availability on
restart"*, resting on *"the lease expires and `REAP` reclaims it."* Checking that claim
while writing this runbook found it is true only inside the chaos harness, which advances
an injected clock and calls `agentiam_controlplane.db.ledger.reap()` directly to observe
reclamation (`tests/chaos/pepstack.py`'s own docstring says as much). **Grepping every
non-test call site of `reap()` found none** — not in `scripts/pep_service.py`, not in
`scripts/serve_pep.py`, not in either compose file, not in `deploy/k3s/`. Spec 04's own
pseudocode prescribes `REAP() # background, every TTL/4`; nothing in this codebase runs
it on a schedule. Recorded as new gap 27 (§3 of `STATUS.md`) — found here, not fixed here,
since a scheduled reaper is a money-hot-path addition needing its own design (where it
runs, how concurrent instances avoid duplicate work) and deserves its own ticket, the same
posture T-056 Part 1 took with gap 25.

**What this means for a rollback today:** a lease stranded by a `SIGKILL`ed pod stays
`leased` in the ledger — reachable by nobody, but not lost either, since `committed` (what
was actually spent, per ADR-049) is unaffected — until someone runs `reap()`. Do it by
hand after any rollback that force-killed a pod:

```bash
uv run python -c "
import asyncio
from datetime import UTC, datetime
from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.db.ledger import reap

async def main() -> None:
    engine = make_engine('postgresql+asyncpg://agentiam:agentiam@<host>:5432/agentiam')
    session_factory = make_session_factory(engine)
    async with session_factory() as session:
        reclaimed = await reap(session, now=datetime.now(UTC))
        print(f'reclaimed {len(reclaimed)} lease(s)')
    await engine.dispose()

asyncio.run(main())
"
```

Confirm it worked the way T-052's own invariant sidecar does: the budget dashboard
(T-047, `console/budgets.html`) should show the pre-rollback total once this runs, not
before.

### 2.4 Rollback checklist

1. Confirm Postgres is reachable from the target namespace/host *before* starting — a
   rollback attempted during the exact outage §2.3 describes is the one case where
   `kubectl rollout undo` can itself hang.
2. Read the docstring of every migration between the current revision and the rollback
   target. If any of them describes a semantic change (like 0004's) rather than a pure
   addition, roll back the schema too (§2.2) and expect the data loss it documents, or
   stay on the current schema and roll the image forward again once fixed instead.
3. Roll back the image (§2.1). Do not raise `terminationGracePeriodSeconds` past the
   shipped default while doing this (§2.3).
4. Watch `kubectl rollout status` / `docker compose ps` to completion.
5. If any pod was `SIGKILL`ed rather than exiting cleanly, run `reap()` by hand (§2.3,
   gap 27) — nothing does this automatically yet — then check the budget dashboard shows
   the pre-incident total.
