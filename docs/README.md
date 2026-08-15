# Documentation Index

Every document in this repository, what it is for, and when to read it.

---

## Start here

| If you want to… | Read |
|---|---|
| Understand what AgentIAM is | [`../README.md`](../README.md) |
| See what is built and what is left | [`STATUS.md`](STATUS.md) |
| Follow how the project developed, and why | [`JOURNAL.md`](JOURNAL.md) |
| Build the next ticket | [`ROADMAP.md`](ROADMAP.md) → [`PLAN.md`](PLAN.md) → the relevant spec |

---

## The four authorities

These four documents govern the project. When they disagree, the order below decides.

| Document | Authority over | Size |
|---|---|---|
| [`PLAN.md`](PLAN.md) | **What** to build. Architecture, protocol specs, data model, API contracts, the 61 tickets, testing strategy, risk register, submission checklist. The root document. | 1,437 lines |
| [`specs/`](specs/) | The **precise** contract for anything `PLAN.md` §6 only sketches. A spec supersedes the plan on its own subject. | 4 files |
| [`ENGINEERING-RULES.md`](ENGINEERING-RULES.md) | **How** to build. Non-negotiable rules, the per-ticket loop, Definition of Done, review cadence. | 92 lines |
| [`ROADMAP.md`](ROADMAP.md) | **Order**. Milestone sequencing, exit gates, what is deferred and why. | 272 lines |

---

## Specifications

Written before the code that implements them. Each one was checked against a running
`biscuit-python` or a protocol model *before* being written down — §9 of spec 02 and §15 of
spec 04 list what was verified.

| Spec | Covers | Written by |
|---|---|---|
| [`specs/01-token-format.md`](specs/01-token-format.md) | Authority and attenuation block structure, all facts and checks, money encoding, size limits, a measured 3-level worked example | T-002 |
| [`specs/02-caveat-language.md`](specs/02-caveat-language.md) | The nine caveat types, their Datalog compilation, the `check if` vs `reject if` rule, reason codes | T-003 |
| [`specs/03-attenuation.md`](specs/03-attenuation.md) | The `narrows()` partial order, the mint-time check, INV-1…INV-10 stated formally, 14 counterexamples | T-003 |
| [`specs/04-lease-protocol.md`](specs/04-lease-protocol.md) | All seven ledger operations, the lease state machine, safety and liveness arguments, partition and clock-skew handling | T-004 |

Specs `05`–`09` (policy, drift, revocation, audit, decision record) are written in M2 and M5.

---

## Records

| Document | What it holds |
|---|---|
| [`DECISIONS.md`](DECISIONS.md) | Append-only ADR log. **17 entries.** Every non-obvious choice, with the context that forced it and what it costs. The receipts behind every "we deferred X because Y" claim in the submission. |
| [`JOURNAL.md`](JOURNAL.md) | The development narrative: what each ticket did, what was found while doing it, and what changed as a result. |
| [`STATUS.md`](STATUS.md) | What is done, what remains, and the improvements worth making with their impact. |
| [`threat-model.md`](threat-model.md) | 24 STRIDE threats, each with a mitigation, a status, and the test id covering it. Six came out of implementation rather than brainstorming. |

---

## Demo and submission

| Document | What it holds |
|---|---|
| [`DEMO.md`](DEMO.md) | The 8-beat demo runbook, 7 failure drills, judge archetypes, pre-presentation checklist |

`PLAN.md` §14 (evidence pack), §18 (research and IP), and §19 (submission checklist) cover the
rest of the submission material.

---

## Generated, not written

| Path | Filled by |
|---|---|
| `benchmarks/` | T-053 (load), T-052 (chaos), T-019 (decision latency). Committed — these go in the submission. |
| `openapi/` | Generated from the FastAPI apps, committed |

---

## Reading orders

**To evaluate the project (a judge, or a reviewer):**
`../README.md` → `STATUS.md` → `threat-model.md` → `specs/03-attenuation.md` §4 → `DEMO.md`

**To understand the engineering:**
`JOURNAL.md` → `DECISIONS.md` → the specs

**To build the next ticket:**
`ROADMAP.md` for the milestone → `PLAN.md` §9 for the ticket → its specs →
`ENGINEERING-RULES.md` for the loop
