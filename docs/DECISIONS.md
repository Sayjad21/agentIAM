# Architecture Decision Log

Append-only. Newest entries at the bottom. One entry per non-obvious choice.

Format:

```
## ADR-NNN — <title>
**Date:** YYYY-MM-DD
**Status:** accepted | superseded by ADR-NNN | reversed
**Context:** what forced the decision
**Decision:** what was chosen
**Consequences:** what this costs, and what it rules out
```

---

## ADR-001 — Infrastructure is introduced per-ticket, not front-loaded in M1

**Date:** 2026-08-14
**Status:** accepted

**Context:** The original milestone plan placed the full service stack — Postgres, Redis,
Keycloak, Ollama with a ~5 GB model, OTEL Collector, Prometheus, Tempo, Loki, Grafana — in
Milestone 1, so that an infrastructure owner could work in parallel with protocol development.
This project has a single developer, so there is no parallel track to fill. Front-loading eight
services means one to two weeks of environment debugging before any protocol code exists, and
it de-risks nothing: the real risk in this project lives in the attenuation algebra (§6.3) and
the lease protocol (§6.4), neither of which needs more than a database.

**Decision:** `docker-compose.yml` grows one service at a time, each added by the first ticket
that requires it.

| Milestone | Services present |
|---|---|
| M1 | Postgres, Redis |
| M3 | + Alembic against real Postgres; testcontainers in the test suite |
| M4 | unchanged — the end-to-end slice needs neither Keycloak nor Grafana |
| M5 | + Ollama/Qwen2.5-7B (T-028/T-029), Keycloak (T-043) |
| M6 | + OTEL Collector, Prometheus, Tempo, Loki, Grafana (T-049) |

NFR-8 (cold start of the full stack under 90 s) is measured in M6/M7 once the stack is
complete, rather than chased against a partial stack in M1.

**Consequences:** The correctness core (M1–M4) is reachable in days rather than weeks, and the
first demoable state (T-023) arrives without any dependency on OIDC or observability wiring.
The cost is that infrastructure risk is discovered later — Keycloak realm configuration and the
Grafana datasource wiring are both known multi-hour tasks, and they now land in M5/M6 rather
than M1. This is accepted: those tasks are tedious but low-risk, whereas a lease-protocol
correctness bug found late is project-ending (risk R-3).

---

## ADR-002 — Single-developer execution; role-partitioned planning removed

**Date:** 2026-08-14
**Status:** accepted

**Context:** `docs/ROADMAP.md` was originally written for a three-person team, partitioning
work into Infrastructure, Core Logic, and Demo/Console tracks with a parallelism map across
milestones. This project is executed by one developer.

**Decision:** Scope is unchanged — all seven milestones and all eight demo beats remain in
scope, including the NL→Cedar compiler and drift detection. Only sequencing changes: the three
parallel tracks collapse into one dependency-ordered track, per-ticket ownership columns are
removed, and the "Build / Verify" split is retained because the distinction between writing
code and proving it against real infrastructure remains real for one person.

**Consequences:** Calendar time is longer than the three-person estimate; there is no fixed
submission deadline, so this is acceptable. Risk R-8 in `PLAN.md` §17 ("team member leaves") is
restated as a single-point-of-failure risk. The mitigation is unchanged and already in place:
every contract is specified in writing before implementation, and the property tests on
attenuation plus the invariant checker serve as the standing second pair of eyes on the two
components where a silent bug would be fatal.
