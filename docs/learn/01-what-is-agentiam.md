# 01 — What AgentIAM is

> **In one sentence:** AgentIAM gives every AI agent and sub-agent its own cryptographic
> identity, carrying strictly narrower permissions and a bounded spend budget than its parent,
> enforced at the tool boundary in under a millisecond, with a complete chain of custody for
> every action taken.

If that is a mouthful, the pitch version is four words: **the credit limit and the chain of
custody for AI agents.**

---

## 1. The problem, in plain terms

An AI agent that can call tools can spend money, read customer data, and send email on your
company's behalf.

Right now, when you give an agent access to a system, you hand it the same credential a human
would use. Then three things go wrong:

**It cannot hand out less than it has.** Agents spawn sub-agents. A research agent spawns a
document reader, a negotiator, a payment agent. Each one needs *some* of the parent's access —
but the only credential the parent can pass down is the whole thing. So the document reader,
whose entire job is reading PDFs, is holding a credential that can initiate payments.

**Nothing counts.** You can say "this agent may initiate payments." You cannot say "this agent
may initiate payments **up to ৳50,000 total**, across at most 100 calls, for the next hour."
There is no ceiling that anything actually enforces. The agent stops when it decides to stop.

**Nobody can answer "who authorized this?"** When a payment goes out at 3 a.m., the log says a
service account did it. It does not say which human approved which task, which agent in a
five-deep chain actually acted, or which rule permitted it.

None of these is a hypothetical. They are the three things that make a bank say no to agent
automation, and they are the three things this project fixes.

---

## 2. What AgentIAM does about it

### Holder-side attenuation

A parent agent mints a **strictly narrower** child token **by itself** — no call back to a
server, no round trip, microseconds. The child token cryptographically cannot exceed what the
parent held. Not "should not" by policy: *cannot*, by mathematics.

This is the thing OAuth genuinely cannot do, and it is the project's core technical claim. It
is built on [biscuit](https://www.biscuitsec.org/), an existing, well-reviewed token format —
we did not invent the cryptography, and we explicitly forbid ourselves from doing so.

### Quantitative mandates

A grant carries real numbers: money, tool calls, rows read, wall-clock seconds, external
emails sent. Those ceilings are enforced under concurrency and under network partition, by a
lease protocol borrowed from distributed systems rather than by hoping.

This is the part that is genuinely hard, and file [09](09-budgets-and-leases.md) is about why.

### Chain of custody

Every decision — allowed or refused — becomes a record in a hash-chained ledger. Ask "who
authorized this payment?" and the answer walks back to the human who approved the task, the
agent that acted, and the exact rule that permitted it. Alter any record and every hash after
it stops matching.

---

## 3. The capabilities, as a list

| # | Capability | Why it is in the product |
|---|---|---|
| C1 | Per-agent and per-sub-agent cryptographic identity | The core primitive |
| C2 | Holder-side offline attenuation | The differentiator versus OAuth |
| C3 | Quantitative mandates: spend, calls, data volume, wall clock | Nobody enforces this correctly today |
| C4 | Lease-based budget enforcement, correct under concurrency and partition | The distributed-systems contribution |
| C5 | Organization policy layer (Cedar) evaluated in the hot path | Enterprise legibility |
| C6 | Natural language → policy compiler, with a verification step | Research angle |
| C7 | Intent binding and goal-drift detection | Research angle |
| C8 | Just-in-time elevation with human approval | The most persuasive demo moment |
| C9 | Fast revocation of a whole subtree | Ditto |
| C10 | Hash-chained tamper-evident audit ledger, with a custody query | What compliance reviewers ask for |
| C11 | MCP gateway and generic HTTP enforcement point | How you adopt it |
| C12 | Admin console: identity tree, live decisions, escalation queue, audit explorer | The demo surface |
| C13 | Reference demo agent (procurement and payments) | Something to actually watch |
| C14 | Observability: traces, metrics, dashboards | Evidence, not decoration |
| C15 | Load, chaos and adversarial test suites, with published results | Evidence, not claims |

---

## 4. Who it is for

**The product audience** is any organization letting AI agents touch systems that cost money
or hold sensitive data — banks, fintechs, procurement and back-office automation. The adoption
shape is deliberately small: an **MCP gateway**, one URL change to install. Nobody rewrites an
agent to try it.

Concretely, four kinds of reviewer, and what each one actually cares about:

- **A bank CTO** wants fail-closed behaviour, the audit chain, real latency numbers, and what
  happens under network partition. The honest answer there is that the system continues
  inside its pre-approved lease and then fails closed — CP rather than AP, which is the
  correct choice when the resource is money.
- **A payments executive** wants hard ceilings, reconciliation, and chain of custody for a
  disputed transaction.
- **An academic reviewer** wants the protocol contribution: attenuable delegation combined
  with quantitative mandates, and the observation that capability-token literature largely
  does not address quantitative resources shared across sibling agents.
- **A commerce or trade reviewer** wants the business case, in local currency, on a real
  workflow.

**The project audience** is the Bangladesh ICT & Innovation Awards (BIIN), Tertiary Student
Project category, feeding to APICTA. Two consequences show up everywhere in the codebase:
every dependency is free and open source with no paid service anywhere, and every model weight
is open and self-hosted.

---

## 5. What it deliberately is **not**

This list matters as much as the feature list. When one of these starts to look tempting
mid-build, that is scope creep arriving exactly on schedule.

- **It does not move money.** It emits settlement *instructions*; a stub adapter consumes them.
  Anything else is a legal non-starter for a student project.
- **It contains no homemade cryptography.** Zero exceptions. Biscuit for tokens, PyCA
  `cryptography` for signing.
- **It contains no homemade policy language.** Cedar for organization policy, biscuit's own
  embedded Datalog for token-level checks.
- **No blockchain.** The audit ledger is a hash chain and we call it a hash chain.
- **No new database, queue, or key-value store.** Postgres and Redis.
- **No foundation-model training.** Nothing larger than a small classifier is fine-tuned.
- **No multi-region active-active.** Single region, documented as future work.
- **No mobile app, no SSO connector catalogue, no cloud-posture scanning.**

---

## 6. The numbers it claims, and how carefully

Ten non-functional requirements, each of which became a test rather than a slide.

| ID | Requirement |
|---|---|
| NFR-1 | Authorization decision latency p99 under 1 ms, in process |
| NFR-2 | End-to-end proxy overhead p99 under 8 ms at 500 requests/second, one instance |
| NFR-3 | Total spend never exceeds the mandate, under every tested concurrency and partition |
| NFR-4 | Revocation reaches every enforcement point in under 2 s p99 |
| NFR-5 | Zero plaintext secrets in any log line |
| NFR-6 | Audit chain verification detects any single-record tampering |
| NFR-7 | Fails closed when the control plane is unreachable; fail-open is opt-in per policy |
| NFR-8 | Full stack cold-starts in under 90 s |
| NFR-9 | Drift detector false-positive rate under 5% on a benign corpus |
| NFR-10 | All development in Bangladesh; all model weights open and self-hosted |

**One honesty note that shapes how every number is reported.** Python cannot proxy an HTTP
request in under a millisecond. It *can* evaluate an authorization decision in well under one.
Those are two different numbers and the project always reports them separately and labels
them. A reviewer who catches you conflating them discounts everything else you said; a
reviewer who watches you separate them voluntarily believes the rest.

The measured figures live in [`docs/benchmarks/performance.md`](../benchmarks/performance.md).

---

## Next

[02 — Why normal auth breaks](02-why-existing-auth-fails.md) explains why this needed new
machinery at all, rather than a careful configuration of what already exists.
