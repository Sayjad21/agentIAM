# Learn AgentIAM

A guided tour of the project, written to be read in order by someone who has never seen it
before. Plain language first, precision second — but never precision sacrificed.

This folder is **separate from the reference docs on purpose**. `docs/specs/` tells you what
the system MUST do, `docs/DECISIONS.md` records why each choice was made, `docs/STATUS.md`
tracks what is built. Those are written for someone who already knows the project. These files
are written for someone learning it.

---

## How to read this

**Part 1 builds the ground floor.** Read these four in order. Nothing later makes sense
without them, and together they take about half an hour.

| File | What you get |
|---|---|
| [01 — What AgentIAM is](01-what-is-agentiam.md) | The problem, what the system does, its features, who it is for |
| [02 — Why normal auth breaks](02-why-existing-auth-fails.md) | Why OAuth and JWTs cannot do this, and what has to be different |
| [03 — Vocabulary](03-vocabulary.md) | Every term the project uses, defined once, with a worked example |
| [04 — How a request flows](04-request-lifecycle.md) | One tool call from arrival to audit record, step by step |
| [05 — Map of the code](05-codebase-map.md) | Which package holds what, and why the boundaries are where they are |

**Part 2 is one feature per file.** After Part 1 you can read these in any order, or jump to
whichever feature you need. Each one follows the same shape: what it is, how it works, the
design choice we faced, what went wrong, why, and how it was fixed.

| File | Feature |
|---|---|
| [06 — Tokens and identity](06-tokens.md) | Cryptographic identity for an agent |
| [07 — Attenuation](07-attenuation.md) | Handing a sub-agent strictly less power, offline |
| [08 — Caveats](08-caveats.md) | The restrictions written inside a token |
| [09 — Budgets and leases](09-budgets-and-leases.md) | Spending limits that hold under concurrency |
| [10 — The policy layer](10-policy-cedar.md) | What the organization allows, regardless of token |
| [11 — Intent and drift](11-intent-and-drift.md) | Binding an agent to the task a human approved |
| [12 — Revocation](12-revocation.md) | Switching off a token, and everything below it |
| [13 — The audit chain](13-audit-chain.md) | A record nobody can quietly edit |
| [14 — Escalation](14-escalation.md) | Asking a human for more authority |
| [15 — Decisions and reason codes](15-decisions-and-reason-codes.md) | Why every refusal names its cause |
| [16 — Console, SDK and demo](16-console-sdk-and-demo.md) | The surfaces people actually touch |
| [17 — Natural language to policy](17-nl-policy-compiler.md) | The LLM feature, and its honest results |
| [18 — Deployment and operations](18-deployment-and-operations.md) | Running it for real |

**Part 3 is how we know it works, and what is still missing.**

| File | What you get |
|---|---|
| [19 — Testing](19-testing.md) | Every kind of test used, why each exists, what it caught |
| [20 — Hard problems](20-hard-problems.md) | The bugs worth remembering, and the habit that found them |
| [21 — Known gaps](21-known-gaps.md) | What is deliberately not built, stated plainly |

---

## The one habit that explains this project

Before writing any specification, we checked its claims against a running system.

That sounds like busywork for a documentation task. It was the opposite. It found **nine
design errors** before a line of the corresponding code existed — every one of which would
otherwise have shipped as a passing test written from the same wrong assumption.

You will see that pattern in almost every file in Part 2. The design was defensible on paper
and wrong against the library. Reading harder would not have caught any of them.

If you take one thing from this folder, take that.

---

## A note on honesty

Several files here describe things that are broken, deferred, or only partly solved. That is
deliberate. A gap you have written down is a known limitation; the same gap undocumented is a
lie by omission, and any reviewer who finds one stops believing the rest.

[File 21](21-known-gaps.md) collects them in one place.

---

## Where to go next

- Want to run it? [`docs/RUNNING.md`](../RUNNING.md) is the operator's guide — bring-up, every
  surface, and what each one should print.
- Want to present it? [`docs/DEMO.md`](../DEMO.md) is the 10-minute script.
- Want the normative rules? [`docs/specs/`](../specs/) — ten documents, one per protocol area.
- Want to know why something is the way it is? [`docs/DECISIONS.md`](../DECISIONS.md) — 70
  architecture decision records, each naming the alternative it rejected.
