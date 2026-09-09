# 16 — The console, the SDK and the demo

*The three surfaces people actually touch.*

---

## Part A — The console

Six screens, each answering one question. They live in the control plane at
`http://localhost:8000` and render client-side from the `/v1` API.

| Screen | The question it answers |
|---|---|
| `/identity-tree` | Who delegated to whom, and what does each one hold? |
| `/decisions` | What is happening right now? |
| `/budgets` | How much is left, and where did it go? |
| `/audit` | Can I prove this? |
| `/escalations` | What needs a human? |
| `/policy` | What are the rules, and can I change them safely? |

### The identity tree is the money shot

It is the screen that makes the product's central claim visible: authority is a **tree**, and
each node holds strictly less than its parent.

It also had the most bugs, all of the same shape — the data was right and the drawing was wrong.

**Every sibling had the same name.** The PEP derived agent names as `agt-depth-{N}`, so three
agents at depth 1 were all called `agt-depth-1`. The tree is built from decision records keyed
on that name, so **three agents collapsed into one node**. The product's claim is a tree; the
screen drew a chain.

The names had been in the tokens the whole time — attenuation writes `agent` and `role` into
every block. Nothing could read them back, because block facts are invisible to the authorizer.
Building the Datalog reader was what fixed it.

**The canvas silently stayed blank.** `d3.stratify()` rejects duplicate node ids by raising
`ambiguous: agt-depth-1` — and a `catch` block swallowed it. The page loaded, reported no error,
and drew nothing. A failure that is invisible is worse than a crash.

**The root node had no name of its own.** A root token carries no attenuation block, so it
declares no agent and no role, and the fallback rendered `agt-depth-0` beside real names like
`agt-doc-reader`. The fallback is right where it is and wrong on screen, so the console now
names it for what it is: the principal (ADR-061).

**A practical note.** Re-seeding the demo in place adds a node per generation, because each seed
run mints new blocks and the audit chain is append-only. The tree is cluttered, not wrong —
every node it draws really did make those calls. For a clean tree, start from a fresh volume.

---

## Part B — The SDK

What an agent developer actually imports. Deliberately small.

```python
from agentiam_sdk import AgentIAM

client = AgentIAM(token=my_token, root_keys=keys)

# Spawn a narrower child. Local, offline, microseconds.
child = client.attenuate(
    scopes={"invoice:read"},
    spend_bdt=Decimal("0"),
    agent_id="agt-doc-reader",
    role="reader",
)

# Headers for a tool call, including the intent assertion.
headers = client.headers(action_intent="reading invoice inv_001")
```

Three things it gives you:

- **`attenuate()`** — the whole delegation story in one call.
- **`headers()`** — including `AgentIAM-Task-Intent`, so intent binding works without the
  developer thinking about hashes.
- **`@requires_scope(...)`** — a decorator that fails fast inside the agent's own process
  instead of waiting for a refusal over HTTP.

> **The decorator is a convenience, never a security control.** It runs in the agent's process,
> which is exactly the place an attacker would already be. The PEP is the security control. The
> decorator exists so a developer gets a clear local error instead of a confusing 403.

The SDK also propagates identity across threads by carrying **the identity itself**, not by
copying a context object (ADR-012) — a copied context goes stale the moment the token is
elevated or attenuated.

---

## Part C — The demo

A procurement scenario, because it makes money movement concrete without pretending to move
money.

**The tree** — three children, two of them going deeper, ceilings shrinking at every level:

```
root                              ৳500,000 (the mandate)
├── agt-doc-reader                ৳0
├── agt-negotiator                ৳50,000
└── agt-payer                     ৳200,000
    └── agt-settlement            ৳25,000
        └── agt-subcontractor     ৳5,000  ← depth 3
```

**Thirteen calls**, chosen so every distinct refusal appears exactly once. This is the expected
output, verified live:

```
allow 200  root reads an invoice                        OK
allow 200  doc-reader reads an invoice                  OK
deny  403  doc-reader attempts a payment                SCOPE_ATTENUATED_AWAY
allow 200  negotiator looks up a vendor                 OK
allow 200  payer settles a small invoice                OK
allow 200  settlement agent pays within its slice       OK
deny  429  settlement agent exceeds its ceiling         BUDGET_EXHAUSTED_CAVEAT
deny  429  root attempts more than the mandate grants   BUDGET_EXHAUSTED_MANDATE
deny  403  sub-contractor is too deep for the policy    POLICY_DENIED
```

Each refusal is a **different layer** saying no. That is the pitch, in one screen.

`agt-subcontractor` earns its place: without it the scenario has no `POLICY_DENIED` at all,
because the mandate's ceiling and the policy's are both ৳500,000, so no *amount* can be refused
by one and not the other. Depth is the only axis where the two layers disagree.

### Two things the demo will do to you

**The tokens expire 8 hours after seeding.** Past that, everything is `401 TOKEN_EXPIRED` — the
token layer working exactly as designed, and the single most likely way to find a dead demo. A
stack brought up the night before a morning presentation is outside the window. **Re-seed before
presenting**; the runbook has the command.

**The first payment on a long-idle stack used to fail.** Fixed (see
[file 09](09-budgets-and-leases.md)), but it is why the lease now renews on a timer.

### One thing the demo does not show

The role-based forbid — a worker refused a critical tool while a senior is allowed — is
**enforced and corpus-tested but invisible in the scenario**. Every agent that can reach the
payment API is one the organization made senior, and the one non-senior agent that tries to pay
is refused by its own scope subset several steps earlier.

That is filed as an open item rather than glossed over. Showing it needs a fourth sub-agent,
which changes the tree the docs describe.

---

## Where to look

| Thing | File |
|---|---|
| Console templates | `packages/agentiam-controlplane/src/agentiam_controlplane/console/` |
| The tree API | `packages/agentiam-controlplane/src/agentiam_controlplane/tree_api.py` |
| The SDK | `packages/agentiam-sdk/src/agentiam_sdk/` |
| Seeding and driving the demo | `scripts/seed_demo.py` |
| The stub tools | `scripts/serve_tools.py` |
| The presentation script | [`docs/DEMO.md`](../DEMO.md) |
| The operator's guide | [`docs/RUNNING.md`](../RUNNING.md) |

---

## Next

[17 — Natural language to policy](17-nl-policy-compiler.md).
