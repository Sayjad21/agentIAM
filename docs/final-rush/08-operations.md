# 08 — Operations, performance and release

**The problem in one line.** NFR-2 (PEP overhead p99 < 8 ms at 500 RPS) has never been
demonstrated: at 100 RPS three runs gave enforcement p99 between 1.75 and 74.7 ms, and 500 RPS
could not be offered because the load generator, three uvicorn processes and Postgres share one
machine. `PLAN.md` §8 says the OpenAPI is committed; it is not. `release.yml` has never run
against a real tag. Two of four planned dashboards exist and nothing alerts.

---

### T-119 — NFR-2 measured off-box on two machines

**Size** M · **Lane** infra / performance · **GPU** — · —

**Why.** GAP-E, mandatory under RD-3, and `docs/TODO.md` item 21 (the harness changed and the
committed figures predate it). The team has two laptops, which is enough to separate the
generator from the system under test.

**Read.** `docs/benchmarks/performance.md`; `scripts/run_load_test.py`; `tests/perf/driver.py`
(coordinated-omission-aware); `docs/PLAN.md` §13.1; `docs/TODO.md` item 21; ADR-052.

**Probe first.** Measure the network between the two laptops: wired, or a dedicated router with
nothing else on it. Record round-trip p50/p99 with `ping` and an HTTP echo server under 500 RPS.
If the network's own p99 exceeds about 2 ms, the measurement cannot support an 8 ms claim; fix
the network first.

**Deliverables.**
- `run_load_test.py --remote <host>`: the driver runs on machine A; the PEP stack, stub
  upstream and Postgres run on machine B.
- Both profiles from `PLAN.md` §13.1, three repeats each, three tiers (direct upstream, PEP
  allow path, PEP with budget), in one sitting on fixed hardware.
- `pb2-breakdown.json` and `nfr2-load.json` regenerated in that one sitting (TODO item 21's
  requirement); `performance.md` regenerated; the "predate a change" notes removed.
- Hardware, OS, CPU governor, network and versions recorded in the results.

**Acceptance criteria.**
- [ ] NFR-2 stated as met or not met at 100 and 500 RPS, with the run-to-run variance.
- [ ] Reproducible from the documented commands.
- [ ] `research/gap-analysis.md` NFR-2 row updated.

---

### T-120 — 1,000 RPS profile and horizontal scaling

**Size** M · **Lane** infra / performance · **GPU** — · —

**Why.** `PLAN.md` §21 item 12. Once a single PEP's ceiling is known, the scaling story is
"add PEPs", and that needs measuring: throughput vs PEP count, and ledger contention as PEPs
share a pool.

**Depends on.** T-119, T-077.

**Deliverables.**
- 1,000 RPS offered across 1, 2 and 4 PEPs; throughput, enforcement p99, ledger `FOR UPDATE`
  wait time, and top-up refusal rate per configuration.
- `docs/benchmarks/scaling.md` generated with `--check`.

**Acceptance criteria.**
- [ ] The point where the ledger becomes the bottleneck is identified, or shown not to be reached.

---

### T-121 — Latency and drift dashboards, alert rules

**Size** S · **Lane** infra · **GPU** — · —

**Why.** `PLAN.md` §21 item 9: two dashboards (decisions, budgets) of four exist. Nothing alerts,
and `docs/TODO.md` item 22 showed red nightly jobs going unread for days.

**Read.** `deploy/grafana/dashboards/`; `deploy/prometheus/`; ADR-047.

**Deliverables.**
- Latency dashboard (decision p50/p99, proxy overhead, per-step breakdown) and drift dashboard
  (score distribution, escalations, oracle availability, classifier version).
- Prometheus alert rules: invariant violation, policy bundle stale, revocation pull lag, audit sink
  backlog, lease refusal rate, PEP skew refusals (T-102), certificate expiry (T-109).
- Alerts tested with `promtool test rules`.

**Acceptance criteria.**
- [ ] Each alert has a unit test that fires it.
- [ ] Dashboards load with no "no data" panels on the demo stack after `make demo-seed`.

---

### T-122 — Cut v1.0.0 and exercise the release pipeline

**Size** S · **Lane** infra · **GPU** — · —

**Why.** `release.yml` (GHCR push, keyless cosign signature, SBOM attestation, self-verification)
has never run against a real tag (ADR-056 addendum). A pipeline that has never run is a claim,
not a capability.

**Read.** `.github/workflows/release.yml`; ADR-056; `docs/deployment.md` rollback section; the
GitHub Actions lowercase-image and action-pinning hazards.

**Depends on.** T-063.

**Probe first.** Run the workflow on a throwaway tag in a fork, or with `act`, and fix what fails
before tagging the real repository.

**Deliverables.**
- `v1.0.0` tag; CHANGELOG generated from ticket commits.
- `cosign verify` and `cosign verify-attestation` commands in `docs/deployment.md`, run and their
  output recorded.
- The rollback procedure exercised once against the published image.

**Acceptance criteria.**
- [ ] The release workflow run is green; the signature and SBOM attestation verify from a clean
      machine.

---

### T-123 — Generate and commit OpenAPI

**Size** S · **Lane** docs/tests · **GPU** — · —

**Why.** `PLAN.md` §8 says "Full OpenAPI is generated and committed to `docs/openapi/`". The
directory does not exist. External developers, T-115's fuzzing and T-090's SDK need it.

**Deliverables.**
- `scripts/generate_openapi.py` writing `docs/openapi/controlplane.json` and `pep.json` from the
  FastAPI apps, deterministic, with `--check`.
- The check wired into the `evidence-pack` CI job (the job that never regenerates its own
  inputs, per `tests/unit/test_ci_workflow.py`'s rule).

**Acceptance criteria.**
- [ ] Changing a route without regenerating fails CI.

---

### T-124 — CI on Windows

**Size** S · **Lane** infra · **GPU** — · —

**Why.** `docs/STATUS.md` §4.6. Part of the team develops on Windows; two past defects were
platform-specific (mojibake, missing `make`). `make.ps1` mirrors the Makefile by hand (ADR-003),
and drift between them has happened (T-055).

**Deliverables.**
- A Windows job running `make.ps1 check` (unit tests, no Docker).
- A test asserting every Makefile target has a `make.ps1` counterpart.

**Acceptance criteria.**
- [ ] Windows job green; the target-parity test fails when a target is added to only one file.
