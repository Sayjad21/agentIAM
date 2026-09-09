# 08 — Caveats

*Feature: the restrictions written inside a token, that travel with it and work offline.*

---

## 1. What a caveat is

A caveat is a rule stored **inside** the token. When a parent attenuates, it writes caveats into
the new block, and from then on every request the child makes must satisfy them.

The important word is **monotonic**: adding a caveat can only ever narrow. There is no caveat
that grants.

## 2. The nine kinds

| Caveat | What it restricts | Example |
|---|---|---|
| `ScopeSubset` | Which operations | keep only `invoice:read` |
| `BudgetCeiling` | How much of a dimension | at most ৳25,000 |
| `TimeWindow` | When | valid until 17:00 |
| `ToolAllow` | Which tools may be used | only `invoice_api` |
| `ToolDeny` | Which tools may **not** | never `payment_api` |
| `ArgPredicate` | Values of specific arguments | `invoice.id` must start with `inv_` |
| `DepthLimit` | How much deeper delegation may go | no further children |
| `IntentBound` | Which task | this exact intent hash |
| `RequiresApproval` | Which operations need a human first | `payment:initiate` |

Nine kinds, and they compose. A single block can carry several.

## 3. How they are enforced

Each caveat compiles to biscuit's embedded Datalog and lives in the block:

```datalog
check if operation($op), ["invoice:read"].contains($op);
check if requested("spend_bdt", $v), $v <= 250000000;
```

Two things then enforce them, and it is worth knowing both exist:

1. **Biscuit's own authorizer** evaluates every check in every block. This is the real
   enforcement, and it is cryptographically bound to the token.
2. **`decide()`** separately evaluates a caveat list so it can name *which* caveat refused, for
   the decision record.

The second is about explainability, not security. If the two ever disagreed, biscuit wins — and
a caveat that `decide()` cannot read is reported as unreadable rather than ignored.

---

## 4. The rule that took two threat findings to learn

**`check if` for facts that are always present. `reject if` only for facts that are optional.**

That single sentence is ADR-007, and it exists because both halves were got wrong first:

- Use `check if` on an optional fact and the request **fails closed** when the fact is missing
  — annoying, but safe.
- Use `reject if` on a mandatory fact and the request **fails open** when the fact is missing —
  the token silently never expires. That is TM-20, described in [file 07](07-attenuation.md).

And the facts a check reads must be **verifier-supplied request facts**, never the token's own
grant facts — that is TM-19, the most important measurement in the project, also in file 07.

If you remember nothing else about caveats, remember: **a caveat that reads the token's own
facts is asking a question about the token, not about the request.**

---

## 5. Reading caveats back out

Once a token has been attenuated a few times, a console wants to show *what does this token
actually permit?* That requires reading the rendered Datalog back into caveat objects — which
is harder than it sounds, and produced its own set of findings.

### The grammar to parse is not the one you generate

The obvious implementation tests `parse(render(caveat)) == caveat`. That tests against the
wrong input. The text you actually receive comes from `block_source()`, and measured against
the real library, **that method normalizes**: whitespace collapses to a fixed form, and facts
get grouped ahead of checks regardless of the order the block was built in.

Good news — the grammar is small and canonical. But you only learn it by running it. So every
round-trip test mints, attenuates, verifies and reads back through a real biscuit.

### Statements cannot be split on `;` or on newlines

`block_source()` escapes nothing. A `;` inside a string literal survives rendering, and a `\n`
in a literal renders as a **real newline**. So the reader tracks quote state instead of
splitting on either.

### The rule carrying the security weight

**An unrecognised statement is reported, never dropped.**

A caveat the reader cannot understand is a restriction the token *has* and the computed bound
does *not*. Dropping it silently would make the token look **more** powerful than it is — the
one direction a display path must never get wrong. So the result carries a `complete` flag, and
a console showing the bound must show that too.

Injection cannot widen a bound in any case, and it is worth being able to say why rather than
hoping: the fold intersects and takes minima, so a statement smuggled inside a string can only
add an apparent restriction or land in the unrecognised list. Neither raises a ceiling.

---

## 6. Design choice: `RequiresApproval` is not a Datalog clause

Eight of the nine caveats compile to Datalog. `RequiresApproval` does not — it is stored as a
block **fact** and evaluated in Python.

Why: "this operation needs a human to approve it first" is not a question about the request. It
is a question about whether an approval *exists somewhere else*, which Datalog inside a token
cannot see. Encoding it as a check would produce a rule that always passes or always fails.

Recorded as ADR-008. The general lesson: when a restriction depends on state outside the token,
it does not belong in the token's logic — it belongs in the pipeline, with the token merely
flagging that the pipeline must ask.

---

## 7. Design choice: a `TimeWindow` has two independent sides

A time window has a start and an end, and they compile to two separate Datalog clauses. When
read back, they come back as two one-sided windows.

That is not a bug and the comparison logic treats them as separate slots (ADR-011). Comparing
them as a single object would fail on a difference that does not exist — the restriction is
identical, only its representation is split.

---

## 8. Where to look

| Thing | File |
|---|---|
| The nine caveat types | `packages/agentiam-core/src/agentiam_core/models.py` |
| Compilation and evaluation | `packages/agentiam-core/src/agentiam_core/caveats.py` |
| Reading them back | `packages/agentiam-core/src/agentiam_core/datalog.py` |
| The normative rules | [`docs/specs/02-caveat-language.md`](../specs/02-caveat-language.md) |

---

## Next

[09 — Budgets and leases](09-budgets-and-leases.md) — the hardest component in the system.
