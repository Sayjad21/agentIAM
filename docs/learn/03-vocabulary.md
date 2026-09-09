# 03 — Vocabulary

Every term the project uses, defined once. The codebase, the specs and the pitch all use these
words consistently, so learning them once pays off everywhere.

Skim it now, come back when a word stops making sense.

---

## 1. The people and the work

| Term | Meaning |
|---|---|
| **Principal** | The **human** who ultimately authorizes work. Authenticated with OIDC (Keycloak in the demo). Every action traces back to one. |
| **Task** | A unit of work a principal approved. Has an id, a plain-English description, and an intent hash. |
| **Mandate** | The **root grant** for a task: which scopes, which quantitative budgets, when it expires, and how deep delegation may go. |
| **Agent** | A process acting under a token. Has a role. |
| **Sub-agent** | An agent spawned by another agent, holding an attenuated (narrower) token. |

## 2. The authority

| Term | Meaning |
|---|---|
| **Token** | A biscuit. Carries identity, task binding, and caveats. Verifiable **offline** with nothing but the root public key. |
| **Scope** | A capability string, like `invoice:read` or `payment:initiate`. |
| **Caveat** | A restriction written **inside** a token. Monotonic: adding one can only ever narrow authority. |
| **Attenuation** | A holder creating a narrower child token, locally, with no issuer round trip. The core differentiator. |
| **Block** | One layer of a biscuit. The first (the *authority block*) is the mandate; each attenuation appends one. |
| **Depth** | How many attenuation blocks a token carries. The root is depth 0, its child depth 1, and so on. |
| **Intent hash** | A hash of the approved task description, fixed in the authority block and unchangeable by attenuation. |

## 3. The enforcement

| Term | Meaning |
|---|---|
| **PEP** | Policy Enforcement Point. The proxy that sits in front of the tools and decides. This is the hot path. |
| **Control plane** | The central service: the ledger, the audit store, the console, the APIs. Deliberately **not** on the hot path. |
| **Decision record** | The permanent record of one authorization decision — what was asked, what was decided, why, and the budget before and after. |
| **Reason code** | The machine-readable cause of a refusal. `POLICY_DENIED`, `SCOPE_ATTENUATED_AWAY`, and so on. Every deny has one. |
| **Bundle** | A signed package of organization policy (Cedar source plus the tool catalogue), with a version and a serial. |

## 4. The money

| Term | Meaning |
|---|---|
| **Budget** | A quantitative ceiling, per dimension. |
| **Dimension** | What is being counted: `spend_bdt`, `tool_calls`, `rows_read`, `wall_clock_s`, `external_emails`. |
| **Ledger** | The database that holds the truth about budgets. The only authority on what remains. |
| **Lease** | A slice of budget the ledger grants to one PEP for a limited time, so the PEP can spend without asking. |
| **Reservation** | An amount held locally against a lease while a call is in flight. |
| **Settlement** | Telling the ledger what was *actually* spent, which may differ from what was reserved. |
| **Top-up** | Replacing a lease that has drained or aged out with a fresh one. |

## 5. The safety machinery

| Term | Meaning |
|---|---|
| **Revocation** | Switching a token off before it expires — optionally the whole subtree beneath it. |
| **Escalation** | An agent asking a human for more authority than it holds. |
| **Elevation** | The narrower, time-boxed grant a human issues in response. Always a **new** token; tokens are never mutated. |
| **Drift** | An agent acting in a way that no longer matches the task the human approved. |
| **Audit chain** | The hash-chained ledger of decision records. Each record binds the previous record's hash. |
| **Chain of custody** | The query that walks an action back to the human who approved it. |

---

## 6. One worked example, used everywhere

Almost every file refers to the demo scenario. Here it is once, in full.

**A human approves a task:**

> *"Procure 500 units of packaging stock, budget BDT 500,000."*

That becomes a **mandate**: scopes `invoice:read`, `vendor:read`, `payment:initiate`; budget
৳500,000; expires in 8 hours; maximum delegation depth 4. A **root token** is minted, bound to
the task id and the intent hash.

**The root agent spawns three children**, each strictly narrower:

| Agent | Scopes it keeps | Its own spend ceiling |
|---|---|---|
| `agt-doc-reader` | `invoice:read`, `vendor:read` | ৳0 |
| `agt-negotiator` | `vendor:read` | ৳50,000 |
| `agt-payer` | `payment:initiate` | ৳200,000 |

**Two of those go deeper:**

```
root  (depth 0)
├── agt-doc-reader     (depth 1)
├── agt-negotiator     (depth 1)
└── agt-payer          (depth 1)   ceiling ৳200,000
    └── agt-settlement (depth 2)   ceiling ৳25,000
        └── agt-subcontractor (depth 3)  ceiling ৳5,000
```

Every arrow is an attenuation: local, offline, and impossible to widen.

**Then they call tools, and five different things can refuse them:**

| What happens | Refused by | Reason code |
|---|---|---|
| `agt-doc-reader` tries to pay | Its own token — it never held `payment:initiate` | `SCOPE_ATTENUATED_AWAY` |
| `agt-settlement` tries to pay ৳30,000 | A caveat its **parent** wrote (ceiling ৳25,000) | `BUDGET_EXHAUSTED_CAVEAT` |
| `root` tries to pay ৳600,000 | The **mandate** the human approved | `BUDGET_EXHAUSTED_MANDATE` |
| `agt-subcontractor` tries to pay ৳100 | **Cedar**: policy permits payments only at depth ≤ 2 | `POLICY_DENIED` |
| A payment arrives with no usable lease | The PEP's local budget lease | `LEASE_UNAVAILABLE` |

**Those five distinctions are the product.** Each is a different layer saying no, each has a
different fix, and a system that reported all five as "denied" would be worth much less. That
is why [file 15](15-decisions-and-reason-codes.md) exists.

---

## 7. Two invariant names you will see constantly

The specs number their guarantees. Two come up so often they are worth memorising:

- **INV-1 (Monotonicity)** — `authority(child) ⊆ authority(parent)`. No caveat set can widen.
  This is the promise the whole delegation story rests on.
- **INV-5 (Budget subadditivity)** — the *sum* of what children spend cannot exceed the
  parent's ceiling, enforced at spend time even when it cannot be enforced statically. This is
  the sibling problem, and it is the reason the ledger exists.

The full list of ten is in [`docs/PLAN.md`](../PLAN.md) §6.3.

---

## 8. Threat labels

The threat model numbers its entries `TM-01` … `TM-27`, and the files here cite them. Nine of
those (TM-19 onward) were found **by measurement rather than by brainstorming** — they are the
interesting ones, and [file 20](20-hard-problems.md) tells those stories.

Test suites use their own prefixes: `A-nn` for red-team attacks, `CH-nn` for chaos scenarios,
`P-nn` for properties, `EC-xxx` for edge cases from the plan.

---

## Next

[04 — How a request flows](04-request-lifecycle.md) walks one tool call through the whole
system, using exactly this example.
