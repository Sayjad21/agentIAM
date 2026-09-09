# 06 — Tokens and identity

*Feature: every agent carries its own cryptographic identity.*

---

## 1. What it is

A token is the thing an agent holds that says who it is and what it may do. In AgentIAM a
token is a **biscuit** — an existing, well-reviewed capability-token format.

A biscuit is made of **blocks**, stacked:

```
┌────────────────────────────────────────┐
│ authority block   (the mandate)        │  ← signed by the root key
├────────────────────────────────────────┤
│ block 1           (agt-payer)          │  ← appended by the root agent
├────────────────────────────────────────┤
│ block 2           (agt-settlement)     │  ← appended by agt-payer
└────────────────────────────────────────┘
```

Two properties make it the right choice, and both come from the format rather than from our
code:

- **Append-only.** You may add a block. You may not remove or edit one.
- **Every block's rules must pass.** Authority is the *intersection* of all blocks.

Put together: adding a block can only shrink what a token permits. That is the guarantee the
whole delegation story rests on, and we get it for free.

---

## 2. What is inside

The **authority block** carries the mandate — the facts, and six checks that must hold on every
single request:

```datalog
check if operation($op), scope($op);                       // asking for a granted scope
check if current_depth($d), $d <= 8;                       // not deeper than allowed
check if request_intent($h), intent($h);                   // the right task
check if requested("spend_bdt", $v), $v <= 5000000000;     // one per budget dimension
check if requested("tool_calls", $v), $v <= 2000;
...
```

Each **attenuation block** carries the child's identity (`agent`, `role`) and whatever new
restrictions the parent chose.

Money in a token is a **scaled integer**, never a float. `spend_bdt` at four decimal places
means ৳500,000 is written `5000000000`. Floats are rejected rather than coerced, everywhere in
the project.

---

## 3. Verifying one

```python
token = verify(raw_token, root_key_set, now=datetime.now(UTC))
```

Verification needs **only the root public key and the token itself**. No database, no network,
no cached state. That property is invariant **INV-3**, offline soundness, and it is why the
enforcement point can be fast: the most expensive step in the request path is this one, at
about 216 µs, and it touches nothing outside the process.

Verification rejects: a wrong key, a single flipped bit, a truncated chain, a spliced chain, a
token outside its validity window, and a chain deeper than the mandate allows. Those are all
measured, not assumed — see `tests/security/`.

---

## 4. How big, and does that matter

Token size grows with depth, because each attenuation appends a block.

| Depth | base64 characters |
|---|---|
| 8 (the maximum) | 4,892 |
| hard limit | 8,192 |

At the deepest chain the system allows, a token is about 60% of the ceiling. That measurement
is the reason a whole planned feature — "token reference mode", where oversized tokens are
replaced by a lookup — stays **deferred**. It was specified, then measured, then shelved,
because the problem it solves does not occur at the depths this system permits.

That is the pattern to notice: the deferral is backed by a number, so it can be revisited when
the number changes.

---

## 5. Design choice: why biscuit and not something else

| Option | Why not |
|---|---|
| JWT | A holder cannot narrow a signed JWT. That is the entire requirement. |
| Macaroons | The right shape conceptually, but the mature libraries and the audited Rust/Python implementation are on biscuit's side. |
| Roll our own | Explicitly forbidden. Rule 1 of the project: never write your own crypto. Zero exceptions. |

Biscuit also gives an **embedded Datalog engine**, which is what lets a restriction travel
*inside* the token and be enforced offline. That turned out to matter more than expected — it
is what makes caveats work at all.

---

## 6. What went wrong

### TM-25 — a library timeout shorter than the operation it guards

**The symptom.** Property tests reported that a child token had authorized something its
parent did not — an INV-1 violation, which would mean the core claim of the project was false.
Intermittent. Roughly 2 in 42,000 calls.

**The root cause.** `biscuit-python`'s authorizer defaults to `max_time = 1 millisecond`, and
that is **wall clock, not work**. A query that takes microseconds of CPU raises
`Reached Datalog execution limits` whenever the process happens to lose the CPU for a
millisecond. Under load — exactly the load NFR-1's 1 ms budget exists to describe — legitimate
requests were being refused for want of scheduling.

The property harness read that error as "denied", and so reported the parent denying what the
child allowed.

**How we proved it.** Fault injection: 10 out of 10 injected timeouts on a parent check
reproduced the false violation. Measured cost of a real depth-8 authorize: 290 µs quiet, 478 µs
under 24-way contention — under 2× headroom against a 1 ms wall-clock limit that includes
scheduling delay.

**The fix.** Every authorizer now sets its limits explicitly — `max_time=250 ms`,
`max_facts=10,000`, `max_iterations=1,000` — so they are bounded on purpose rather than by
accident. The property harness re-raises the error instead of reading it as a denial. Recorded
as ADR-021.

**Why it is worth remembering.** This was a *product* bug wearing a test bug's clothes. A
loaded PEP would have denied legitimate requests in production, and the only reason anyone
looked was that a test went flaky. The instinct to mark a flaky test as "known flaky" would
have shipped it.

### TM-24 — a name that rewrites the token when you read it back

**The symptom.** None, at first. This was found by measurement, not by failure.

**The root cause.** Biscuit escapes strings correctly when it *writes* them, so a crafted role
name cannot forge a fact inside a signed token. But `block_source()` — the function that
renders a block back to text so a console can display it — renders the string **unescaped**.
Measured: a role of `x"); admin(true); //` renders as block text that re-parses into a genuine
second fact.

Every consumer of block source is a display or parsing path: the console's caveat chain, the
audit explorer, the Datalog reader. So the attack lands on the screen a human trusts.

A bidirectional-text override does the same thing visually, reordering a displayed role without
changing a byte.

**The fix.** `validate_label` refuses quotes, backslashes, control characters and
bidi controls in the three fields that become Datalog string facts — `principal_id`, and
`attenuate()`'s `agent_id` and `role` — at the only three places they can enter a token.
Non-ASCII is otherwise unrestricted, so a Bengali role name renders as itself. 66 test cases.

---

## 7. Where to look

| Thing | File |
|---|---|
| Minting and verifying | `packages/agentiam-core/src/agentiam_core/tokens.py` |
| The validated types | `packages/agentiam-core/src/agentiam_core/models.py` |
| Reading a token's rules back | `packages/agentiam-core/src/agentiam_core/datalog.py` |
| The normative rules | [`docs/specs/01-token-format.md`](../specs/01-token-format.md) |

---

## Next

[07 — Attenuation](07-attenuation.md) — the feature that makes this project different from
everything else.
