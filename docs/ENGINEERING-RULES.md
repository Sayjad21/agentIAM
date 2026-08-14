# AgentIAM — Engineering Rules

> Project standards for AgentIAM. These are not suggestions. Every one of them exists
> because violating it produces either a security bug or an unprovable claim.
>
> `docs/PLAN.md` is authoritative on *what* to build. This file governs *how*.

---

## 1. Non-negotiable rules

1. **Never write your own crypto.** Use `biscuit-python`. Zero exceptions.
2. **Never invent the caveat language, the token format, or the lease protocol.** They are
   specified in `PLAN.md` §6 and in `docs/specs/`. Follow them exactly. If a simpler variant
   looks tempting, it is wrong — those three *are* the research contribution.
3. **`agentiam-core` has zero I/O.** No `httpx`, no `sqlalchemy`, no `redis`, no
   `datetime.now()`. The clock is injected. Enforced by a CI check.
4. **Money is `Decimal` in Python and `NUMERIC(20,4)` in Postgres.** Never `float`. Not once,
   not "just for the estimate."
5. **Every deny path maps to exactly one reason code** from `PLAN.md` §6.9. A deny without a
   named cause is a bug, not a deny.
6. **Fail closed.** No lease, stale policy bundle beyond max staleness, unreachable control
   plane, ambiguous state → deny. Fail-open is per-scope, opt-in, and audited.
7. **No new dependency without a `DECISIONS.md` entry** justifying it against §4 of the plan.
8. **No paid service, ever.** 100% free/OSS is a design constraint and a scoring advantage
   (§14.4, NFR-10), not a budget compromise.
9. **Never weaken a test to make it pass.** Fix the code or fix the spec — and if the spec,
   write the ADR.
10. **No PII in logs or decision records.** Only `arg_digest`.
11. **Tokens are immutable.** Elevation issues a *new* token. Narrowing creates a *new* token.
    Nothing ever mutates an existing token.

---

## 2. Workflow per ticket

Tickets come from `PLAN.md` §9, sequenced by `docs/ROADMAP.md`. One ticket at a time.

```
read the PLAN.md sections the ticket names
  → restate the acceptance criteria in your own words; list any spec ambiguity found
  → write or extend the tests first — confirm they FAIL
  → implement
  → run: ruff check . && mypy --strict . && pytest
  → update docs/ if a contract changed
  → append to docs/DECISIONS.md if a non-obvious choice was made
  → commit
```

**Specs before code, always.** For any ticket touching a contract — token format, API, protocol
— the spec section in `docs/` is written and reviewed first. Bugs in specs are cheap; bugs in
code that encoded a wrong spec are expensive.

**Tests are part of the deliverable, not a follow-up ticket.** A ticket is not done until its
acceptance criteria are covered by automated tests that failed before the change and pass after.

**If a ticket sprawls past ~3 sittings, it is too big.** Split it and record the split in
`DECISIONS.md`.

---

## 3. Definition of Done

Every ticket, no exceptions:

- [ ] All acceptance criteria covered by automated tests
- [ ] `ruff check .` clean
- [ ] `mypy --strict` clean on changed packages
- [ ] `pytest` green; coverage ≥ 85% on changed files
- [ ] Docstrings on all public functions
- [ ] `docs/PLAN.md` or the relevant spec updated if a contract changed
- [ ] `docs/DECISIONS.md` appended if a non-obvious choice was made
- [ ] No `TODO` without a ticket ID next to it
- [ ] No `# type: ignore` without a comment explaining why

---

## 4. Self-review checklist — run every 5 tickets

Review the last five tickets against `PLAN.md`. Report findings with `file:line`. **List
everything first; fix nothing until the list is complete** — fixing while reviewing is how
findings get missed.

1. Any place the code contradicts `PLAN.md`
2. Any invariant from §6.3 (INV-1…INV-10) not covered by a property test
3. Any deny path not mapped to a reason code from §6.9
4. Any I/O that leaked into `agentiam-core`
5. Any `float` touching a money value
6. Any test that was weakened rather than fixed
7. Any `TODO` without a ticket ID

---

## 5. Spec-drift check — run at each milestone boundary

Compare `PLAN.md` §6 against the current implementation. Produce a table:

| Spec item | Implemented? | Matches spec? | `file:line` | Note |
|---|---|---|---|---|

Flag every divergence and decide — explicitly, in `DECISIONS.md` — whether the spec or the
code should change. Do not change either while producing the table.

---

## 6. Commit conventions

- Subject line: `T-0XX: <imperative summary>` for ticket work; `docs:`, `chore:`, `fix:` for
  everything else.
- One ticket per commit where practical. A commit should leave the suite green.
- **No co-author trailers, no tool-generated footers.** This repository is sole-authored work
  and the IP statement in `PLAN.md` §14.4 depends on the history reflecting that.

---

## 7. When stuck

Do not guess at a spec. Do not silently simplify a protocol. A wrong assumption in the ledger
or in the attenuation logic is a security bug that property tests may not catch if the property
itself was written from the same wrong assumption.

Write the ambiguity down in `DECISIONS.md` as an open question, resolve it against the spec,
then proceed.
