# Documentation Index

Every document in this repository, what it is for, and when to read it.

---

## Start here

| If you want to… | Read |
|---|---|
| Get the whole picture on one page — architecture, status, deferrals, benchmarks, the demo | [`MASTER.md`](MASTER.md) |
| See what is built, what is left, and what is known to be wrong | [`STATUS.md`](STATUS.md) |
| Bring the stack up and drive it | [`RUNNING.md`](RUNNING.md) |
| Learn the project from scratch | [`learn/00-start-here.md`](learn/00-start-here.md) |
| Pick up the next piece of work | [`STATUS.md`](STATUS.md) §2 and §5 → [`PLAN.md`](PLAN.md) §9 for the ticket → its spec → [`ENGINEERING-RULES.md`](ENGINEERING-RULES.md) for the loop |

---

## The four authorities

These govern the project. When they disagree, the order below decides.

| Document | Authority over |
|---|---|
| [`PLAN.md`](PLAN.md) | **What** to build. Architecture, protocols, data model, API contracts, the 61 tickets, testing strategy, risk register, submission checklist. The root document |
| [`specs/`](specs/) | The **precise** contract for anything `PLAN.md` §6 only sketches. A spec supersedes the plan on its own subject |
| [`ENGINEERING-RULES.md`](ENGINEERING-RULES.md) | **How** to build. Non-negotiable rules, the per-ticket loop, Definition of Done |
| [`ROADMAP.md`](ROADMAP.md) | **Order.** Milestone sequencing, exit gates, what is deferred and why |

---

## Specifications

Written before the code that implements them, and checked against a running `biscuit-python`
or a protocol model before being written down.

| Spec | Covers |
|---|---|
| [`01-token-format`](specs/01-token-format.md) | Authority and attenuation block structure, facts and checks, money encoding, size limits |
| [`02-caveat-language`](specs/02-caveat-language.md) | The caveat types, their Datalog compilation, `check if` vs `reject if`, reason codes, reading Datalog back |
| [`03-attenuation`](specs/03-attenuation.md) | The `narrows()` partial order, the mint-time check, INV-1…INV-10, counterexamples |
| [`04-lease-protocol`](specs/04-lease-protocol.md) | The seven ledger operations, the lease state machine, safety and liveness, partition and clock skew |
| [`05-policy`](specs/05-policy.md) | Cedar in the hot path, the entity model, the test corpus |
| [`06-drift-detection`](specs/06-drift-detection.md) | Intent binding, the drift oracle and its modes |
| [`07-revocation`](specs/07-revocation.md) | Persist-then-publish, push and pull propagation, subtree semantics |
| [`08-audit-chain`](specs/08-audit-chain.md) | The hash chain, custody, tamper detection |
| [`09-decision-record`](specs/09-decision-record.md) | The ten-step decision, precedence, the record every decision writes |
| [`10-scope-extraction`](specs/10-scope-extraction.md) | Route mapping, argument extraction, ambiguity refusal |

---

## Records

| Document | What it holds |
|---|---|
| [`DECISIONS.md`](DECISIONS.md) | Append-only ADR log, 70 entries. Every non-obvious choice, the context that forced it, and what it costs |
| [`JOURNAL.md`](JOURNAL.md) | The development narrative: what each ticket did and found, the fix queue from the manual passes (items 1–34, cited in commits as "TODO item N"), and every closed gap |
| [`threat-model.md`](threat-model.md) | 27 STRIDE threats, each with a mitigation, a status and the test covering it |
| [`manual-test-report.md`](manual-test-report.md) | A hand-driven pass over the running system — the evidence behind the fix queue |

---

## Demo, operations and submission

| Document | What it holds |
|---|---|
| [`DEMO.md`](DEMO.md) | The intended 8-beat demo, failure drills, judge archetypes, pre-presentation checklist. `MASTER.md` §7.8 says which beats can be shown today |
| [`RUNNING.md`](RUNNING.md) | The operator's guide: bring-up, every surface with the output it produces, what to do when it differs |
| [`deployment.md`](deployment.md) | Image provenance, signing, rollback |
| [`../deploy/k3s/README.md`](../deploy/k3s/README.md) | The Kubernetes manifests |

`PLAN.md` §14 (evidence pack), §18 (research and IP) and §19 (submission checklist) cover the
rest of the submission material.

---

## Generated, not written

| Path | Produced by | Checked in CI |
|---|---|---|
| `benchmarks/performance.md` | `make benchmarks` (from the PB-2 and NFR-2 JSON) | yes |
| `benchmarks/chaos-results.md` | `make chaos` | yes |
| `evidence/sbom.json` | `make sbom` — regenerate on Linux x86_64 only (see `scripts/generate_sbom.py`) | yes |
| `evidence/evidence-pack.html` | `make evidence` | yes |

`evidence/security-scan.md` is written by hand and documents every scanner waiver.

---

## Research

[`../research/`](../research/README.md) is the research track: the gap analysis of claims
against code, related work, a property-oriented threat model, and the plan for the lease
contribution. Its README gives the reading order.

---

## Reading orders

**To evaluate the project (a judge or a reviewer):**
`../README.md` → `MASTER.md` → `threat-model.md` → `specs/03-attenuation.md` §4 → `DEMO.md`

**To understand the engineering:**
`JOURNAL.md` → `DECISIONS.md` → the specs

**To build the next ticket:**
`STATUS.md` → `PLAN.md` §9 for the ticket → its specs → `ENGINEERING-RULES.md` for the loop
