# AgentIAM on Kubernetes — T-056 Part 3

Plain Kubernetes manifests (no Helm), showing the same deployable shape as
`docker-compose.demo.yml` on a real orchestrator. **Structurally validated on every
`make check`; verified once, live, against a real `kind` cluster while writing this —
not re-verified by CI**, since no CI job here runs a live cluster. See "What is and is
not proven" below before trusting this for anything beyond a demo.

## Bring it up

```bash
# 1. Generate the root keypair + signed policy bundle + routes (T-056 Part 2's script,
#    reused as-is — nothing k8s-specific about what it produces).
uv run python scripts/bootstrap_demo_secrets.py --out /tmp/agentiam-secrets

# 2. Build and load the image. On a real cluster, push to a registry instead and set
#    the image tag via kustomization.yaml's override comment.
docker build -t agentiam:latest .
kind load docker-image agentiam:latest --name <your-kind-cluster>   # kind only
# k3s: `k3s ctr images import` after `docker save`, or point at a real registry.

# 3. Create the namespace and the Secret BEFORE the app manifests — see "Why the Secret
#    is not a committed file" below.
kubectl create namespace agentiam --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic agentiam-secrets \
  --from-file=/tmp/agentiam-secrets -n agentiam

# 4. Everything else.
kubectl apply -k deploy/k3s/

# 5. Wait for it.
kubectl wait --for=condition=complete job/migrate -n agentiam --timeout=120s
kubectl wait --for=condition=available deployment/controlplane deployment/pep \
  -n agentiam --timeout=120s
```

## Why the Secret is not a committed file

`create_app_from_env()` and `pep_service.ServiceSettings.from_env()` both refuse to
start without a real root keypair and a real signed policy bundle (ADR-056). A
Kubernetes `Secret` manifest with real key material committed to a public repository is
exactly the leak class `tests/security/test_secret_scanning.py` (T-054) exists to catch
— and a *template* Secret with placeholder values sitting next to real manifests is a
predictable source of copy-paste mistakes. `kubectl create secret generic --from-file`
against `bootstrap_demo_secrets.py`'s output directory is one command and produces a
Secret keyed by filename, matching every environment variable and file path the two
Deployments expect exactly (`controlplane.yaml`, `pep.yaml`).

## What is and is not proven

**Structurally validated in `tests/unit/test_k3s_manifests.py`, on every `make check`:**
every file parses as YAML, every `Deployment`/`Job` references `agentiam:latest` and no
other image, every environment variable name matches what `ServiceSettings.from_env()`
and `ControlPlaneSettings.from_env()` actually read (Part 1/2's own settings classes,
not retyped), every readiness/liveness probe path matches a real `/healthz`/`/readyz`
route, and the two Secret-consuming Deployments only ever reference keys
`bootstrap_demo_secrets.py` actually writes.

**Not proven by CI, because it would need a live cluster CI does not have:** that these
manifests are *schema-valid* Kubernetes objects, and that a real deployment actually
reaches `Ready`. Both were checked manually against a real cluster while writing this —
`kubectl apply --dry-run=client` was measured *not* to catch a deliberately-broken field
in this kubectl/server version (v1.36); only `kubectl apply --dry-run=server`, which
needs a reachable API server, does real validation. Reproduce:

```bash
kubectl apply --dry-run=server -k deploy/k3s/   # needs the agentiam-secrets Secret first
```

**Live-verified once, manually, against a real `kind` cluster (kubectl/server v1.36.1).**
`kubectl apply -k deploy/k3s/` → every pod reached `Running`/`Completed`
(`postgres`, `redis`, `migrate`, `controlplane`, `pep`, `tools`), the migrate Job
completed and `controlplane`'s `/readyz` reported `database: true` against the real
schema it created, and a request through the real proxy path with no bearer token
returned a genuine `401` (`MALFORMED_REQUEST`, `"token is absent or empty"`) — verify,
policy and lease all reachable end to end inside the cluster. This found one real bug,
now fixed: **every `agentiam:latest` container needs `imagePullPolicy: IfNotPresent`
set explicitly.** Kubernetes defaults a `:latest`-tagged image to `imagePullPolicy:
Always` regardless of local presence, so even a successful `kind load docker-image`
still produced `ImagePullBackOff` — there is no registry to pull `agentiam:latest`
*from*. `tests/unit/test_k3s_manifests.py`'s
`TestImages::test_every_agentiam_latest_container_sets_imagepullpolicy_ifnotpresent`
pins the fix. (Getting `postgres:16-alpine`/`redis:7-alpine` into this particular kind
node was separately blocked by an IPv6 egress timeout on `kind load` and a local
Docker image cache with an incomplete multi-arch manifest list — both host-specific,
neither an AgentIAM defect, worked around with
`docker exec <node> ctr --namespace=k8s.io images pull --platform linux/amd64 <image>`.)

## What is deliberately not here

- **Keycloak / OIDC.** `AGENTIAM_CONTROLPLANE_OIDC_*` is unset, so `/readyz` reports
  `auth: false` — a legitimate configuration (T-043, Part 1), not a broken one. Bringing
  Keycloak's realm-import setup to Kubernetes is real work for a login flow this demo
  does not require; add it if a cluster deployment needs human OIDC login.
- **Ollama.** Same reasoning as `docker-compose.demo.yml`'s opt-in profile: the LLM
  backend defaults to hosted inference (ADR-040), and a model pull is several GB.
- **A bundle-publishing service.** Same limitation as everywhere else in this project
  (ADR-039): the PEP loads its policy bundle once at boot. Updating it means restarting
  the `pep` Deployment with a new Secret, not a live push.
- **TLS / Ingress.** Out of scope for a demo cluster; `kubectl port-forward` or a
  cluster-specific `Ingress`/`Service type: LoadBalancer` is left to the deployer.
- **Multi-mandate PEP.** Unchanged from Parts 1–2 (gap 25): one `pep` Deployment
  enforces budget for exactly one mandate.
