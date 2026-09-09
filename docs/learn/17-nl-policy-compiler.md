# 17 — Natural language to policy

*Feature: type a rule in English, get Cedar, with a verification step in front of it.*

This file is here as much for **how the result was measured** as for the feature itself. It is
the most honest thing in the project.

---

## 1. What it does

Type into the console:

> *"Managers can approve expenses under 50,000 taka."*

A local language model emits Cedar. The console shows it, runs it against the 51-case corpus,
and only then offers to activate it.

The value is not the generation. It is that a compliance officer can read the result, and that
**nothing generated can reach production without passing the same gate a hand-written policy
passes**.

---

## 2. Dual gating

The generated policy goes through two gates, and this is the design point (ADR-034):

1. **Auto-generated test cases.** The compiler derives test cases from the request via JSON
   Schema, and evaluates them **off the hot path**.
2. **The corpus.** The same 51 cases every bundle must pass, enforced server-side, refusing with
   409 and naming the failures.

An LLM is a *drafting* tool here. It is never trusted, and the gate is exactly the one a human
author faces.

## 3. No external egress, by construction

The client uses plain `httpx` against a configured endpoint, deliberately, so it is
straightforward to prove no request leaves for a third-party API (ADR-032).

That is not a stylistic preference. NFR-10 requires all model weights to be open and
self-hosted, and a vendor SDK that might phone home would make that claim unverifiable.

---

## 4. The measurement, reported honestly

This is the part worth reading.

**First real measurement: 0 out of 30.**

The instinct is to blame the prompt. The investigation found three separate defects, and the
third is the one that matters:

- **(a)** The ambiguity instruction over-fired, refusing 77% of the corpus on prompts as plain
  as *"Managers can approve expenses"*.
- **(b)** The model's Cedar was wrong in a small, fixed set of ways — `Resource::*` for an
  unconstrained resource, `resource in Expense` where Cedar wants `resource is Expense`,
  conditions in the scope instead of a `when` clause.
- **(c)** **The dataset could not be passed by anything.**

### Why (c) is the finding

Of 30 cases, the positive principal id appeared verbatim in the English in only **10**.
Thirteen required guessing an arbitrary suffix — *"Admins"* → `admin`, *"HR"* → `hr_rep`. Seven
named an id the prompt never mentioned at all: `alice`, `manager1`, `guest`.

So **≤ 10/30 was the ceiling for any compiler**, perfect or otherwise. And reaching even that
required emitting `principal == User::"manager1"` — a policy about one named person, which is
the *wrong generalisation* to reward.

The right answer, a role-based policy, could not work either: the harness passed `entities=[]`,
so `principal.role` did not exist to be referenced.

**The harness had been measuring nothing.** It passed bare names where Cedar needs entity uids,
so every request returned `NoDecision` — 0/30 regardless of compiler quality. Fixed and
verified against a control policy: 0/30 → 1/30, correct, because only one case concerns admins.

### The rebuild

Dataset v2 evaluates against AgentIAM's own entity model, so a case is won by **generalising**
rather than by guessing an id. And it is **self-validating**: `--validate` scores the reference
policies with no model at all and must hit 30/30 before any run is worth starting.

**Result on a local 7B model:**

| | |
|---|---|
| Passed | **13 (43%)** |
| Wrong | 7 |
| Unparseable | 8 |
| Asked for clarification | **0** (was 23) |
| Errors | 2 |
| Median latency | 4.4 s |

43% is stated plainly as **not good enough to put in front of a judge unrehearsed**. Remaining
failures are Cedar syntax slips, several of them a missing trailing semicolon on an otherwise
correct policy.

---

## 5. Three lessons that generalise beyond LLMs

**Fix the instrument before tuning the thing it measures.** Prompt engineering against an
unpassable dataset optimises toward a target that is both unreachable and wrong. Every hour
spent on the prompt before rebuilding the dataset would have been wasted.

**A benchmark should be self-validating.** `--validate` scoring reference answers with no model
is the cheapest possible guard against the harness silently breaking again. If a perfect
answer does not score perfectly, the harness is broken — and you find that out in seconds.

**A 0% result is more likely to be your bug than their incapacity.** The first measurement was
entirely a harness defect. "The model is bad at this" is a comfortable conclusion and it was
wrong twice over.

---

## 6. Two smaller findings

**The timeout was below the operation's own warm median.** The compiler's configured timeout was
shorter than the median time a *successful* warm call took, so the feature failed by timeout
under entirely normal conditions (ADR-038). Worth checking any timeout against a measured
distribution rather than against intuition.

**It runs on hosted inference for now; local is the production target.** Recorded as a temporary
state with the gap between the two numbers named as the thing a migration back must close
(ADR-040) — not quietly left as the permanent answer.

---

## 7. When Ollama is not running

The feature degrades cleanly, verified live:

```
Error: Ollama network error: All connection attempts failed
```

Inline in the page. No hang, no 500, no blank screen. The demo runbook's fallback drill scripts
exactly this, because a model that is down during a presentation is a *when*, not an *if*.

---

## 8. Where to look

| Thing | File |
|---|---|
| The compiler | `packages/agentiam-controlplane/src/agentiam_controlplane/nl_compiler/` |
| The evaluation harness | `scripts/evaluate_compiler.py` |
| The dataset | `packages/.../nl_compiler/dataset.json` |
| The corpus it must pass | `packages/agentiam-core/src/agentiam_core/corpus.py` |

---

## Next

[18 — Deployment and operations](18-deployment-and-operations.md).
