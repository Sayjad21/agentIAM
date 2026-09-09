# 14 — Escalation and human approval

*Feature: an agent asks a human for authority it does not have, and gets a narrower,
time-boxed grant.*

This is the most persuasive thing to show a non-technical audience. Everything else prevents
something; this one *does* something, with a person in the loop.

---

## 1. The flow

```
1. agent hits a wall              →  denied, with a decision_id
2. agent opens an escalation      →  "I need payment:initiate up to ৳1,000, because …"
3. it appears in the console      →  a human sees the request, the agent, the task
4. a human approves or denies     →  approving mints a NEW token, narrower and short-lived
5. the agent retries              →  now with the elevated token
```

Note step 4. **The original token is not modified.** Tokens are immutable values —
elevation issues a new one. That is a project-wide rule and it is what keeps the audit story
coherent: there is no moment where a token means something different than it did before.

---

## 2. The four rules that make an approval screen mean what it says

Every one of these is a separate enforced check, and every one exists because the obvious
implementation gets it wrong.

### An approval can only narrow, never widen

The approver may hand back **less** than was asked for — fewer scopes, a smaller amount — but
never more. Both dimensions are checked:

```python
widened = scopes - escalation.requested_scopes
if widened:
    raise NarrowingWidensRequest(...)
```

Without this, "approve" becomes a general-purpose authority-granting button that happens to be
next to a request. A reviewer clicking approve must be agreeing to *what is on the screen*.

### An approver may not approve their own escalation

Self-approval is checked and refused. The acting identity comes from the caller's **OIDC
session**, never from anything the request body claims — the approve endpoint has no `approver`
field at all, deliberately (ADR-046).

That is the difference between authentication and assertion. A body field saying "approver:
alice" is a claim; a session is evidence.

### Approvals expire

An escalation carries a TTL (default 15 minutes) and the elevation it produces is short-lived
(default 5 minutes). An approval a human granted an hour ago for a situation that has moved on
is not an approval.

### An approval granting nothing is refused

Narrowing to an empty scope set raises rather than minting a token that permits nothing. The
error says why: *deny it instead, so the agent receives a reason rather than a token that
permits nothing.*

A useless token is worse than a denial, because the agent cannot tell what happened.

---

## 3. Verified live

Unauthenticated approve and deny are both refused:

```
POST /v1/escalations/<id>/approve   →  401 {"detail": "login required"}
POST /v1/escalations/<id>/deny      →  401 {"detail": "login required"}
```

Opening one works, and a duplicate is rejected with a useful message rather than an error:

```
POST /v1/escalations   →  201
POST /v1/escalations   →  409  decision <id> already raised escalation <id>,
                               opened 2026-09-08T15:44:35 and now pending
```

---

## 4. Design choice: a duplicate is a 409, and is *not* absorbed

A repeated **revocation** is idempotent — asking twice for the same block to be revoked returns
the existing record, because the caller's intent is already satisfied.

A repeated **escalation** is not. It returns **409 Conflict**, naming the escalation that
already exists (ADR-058).

The difference is that a revocation is a statement about a desired end state, while an
escalation is a request awaiting a human. Silently returning the existing one would let an agent
in a retry loop believe it had opened a fresh request each time, and would hide the fact that a
human has already been asked.

**It was a 500 before it was a 409** — the unique constraint fired and nothing caught it. An
unhandled database error is not an API contract.

---

## 5. Where the design is honest about a limit

**TM-05, the confused deputy.** A low-privilege sub-agent induces a higher-privileged agent to
act on its behalf.

The higher agent's own caveats still apply, so the action is bounded by *its* authority, and
the audit chain records who actually acted. But the residual is real: **the high-privilege agent
may hold authority the requester should not reach.**

Recorded as **partially mitigated**. Not claimed as solved.

---

## 6. Where to look

| Thing | File |
|---|---|
| The state machine and the four checks | `packages/agentiam-core/src/agentiam_core/escalation.py` |
| The API | `packages/agentiam-controlplane/src/agentiam_controlplane/escalations_api.py` |
| OIDC session handling | `packages/agentiam-controlplane/src/agentiam_controlplane/auth.py` |
| The PEP's side | `packages/agentiam-pep/src/agentiam_pep/escalation_sink.py` |

---

## Next

[15 — Decisions and reason codes](15-decisions-and-reason-codes.md).
