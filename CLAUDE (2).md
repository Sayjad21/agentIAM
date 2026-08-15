# Working instructions — AgentIAM

**This file is gitignored and must stay that way.** AgentIAM is a BIIN submission whose IP claim
is *100% own development, all core code original* (`PLAN.md` §14.4, NFR-10). The repository
carries no AI attribution: no co-author trailers, no generated-with footers, no document stating
that code was AI-written. `.gitignore` covers `CLAUDE.md`, `CONTEXT.md` and `.claude/`.

---

## Read these first

| Document | Authority |
|---|---|
| `docs/PLAN.md` | **What** to build. The root document |
| `docs/specs/` | The precise contract. A spec supersedes the plan on its own subject |
| `docs/ENGINEERING-RULES.md` | **How** to build. The eleven non-negotiable rules, the per-ticket loop, the Definition of Done |
| `docs/ROADMAP.md` | **Order.** Milestones, exit gates, what is deferred |
| `docs/STATUS.md` | Where the project currently is |
| `docs/DECISIONS.md` | Twenty ADRs. Read before revisiting any settled question |

`docs/ENGINEERING-RULES.md` is authoritative on process. What follows is what that file does not
cover: how this machine works, and what has already gone wrong.

---

## The habit that matters

**Check the claim against the running system before writing it down.**

From T-002 through T-014 this found nine design errors, every one of which was defensible on
paper and wrong against the running system — plus one security finding (TM-24) that came from
asking a library a single question. Reading harder would not have caught any of them.
`docs/JOURNAL.md` lists them.

**Two of the nine were errors in our own specs**, not in a library: spec 04's `ACQUIRE` formula
cannot pass its own ticket's acceptance test (ADR-015), and spec 04's `LEDGER_COMMIT` statement
order is a TOCTOU race (ADR-017). Writing the spec first is what makes the project defensible;
it does not make the spec right. Probe it the same way.

Concretely: before specifying protocol behaviour, write a throwaway probe in the scratchpad and
run it. Before asserting a safety argument, model it and remove each guard to check the guard is
load-bearing. A guard whose removal changes nothing is not protecting anything.

**Re-verify assumption A1 on any `biscuit-python` upgrade.** The whole design rests on biscuit
scoping block facts so a later block cannot widen authority (`docs/threat-model.md` §4). It is
verified by hand, not by CI. `docs/STATUS.md` §4.1 proposes fixing that.

---

## Environment

Windows 11, Python 3.12.4, Docker Desktop. PowerShell is the primary shell.

**`uv` is not on the session PATH by default.** Prefix commands:

```powershell
$env:Path = "$env:Path;C:\Users\Legion\AppData\Roaming\Python\Python312\Scripts"
```

It *is* on the persistent user PATH, so new terminals are fine; only fresh tool sessions need it.

**`make` does not exist here.** Use `.\make.ps1 <target>` — it mirrors the Makefile
one-for-one (ADR-003). The Makefile stays authoritative because CI runs it on Linux.

```powershell
.\make.ps1 check             # ruff + ruff format + mypy --strict + pytest — the CI `quality` job
.\make.ps1 up                # Postgres + Redis, wait for healthy
.\make.ps1 test
.\make.ps1 test-integration  # the 82 ledger tests — needs Docker Desktop running
```

**`check` is not the whole story.** It excludes the `integration` marker, which is every test
that touches Postgres — the `FOR UPDATE` serialization proof, the 50-concurrent-acquire bound,
the `LEDGER_COMMIT` dedup race. After touching anything under `agentiam-controlplane` or
`agentiam-pep`, run `test-integration` too. CI runs both as separate jobs.

**Testcontainers cannot start under Docker Desktop for Windows** without
`TESTCONTAINERS_RYUK_DISABLED=true`. Its Ryuk reaper sidecar never gets a published port, so
every container fails with `ConnectionError: Port mapping ... is not available`. `.\make.ps1
test-integration` sets it for you; a bare `uv run pytest -m integration` does not. It stays out
of the Makefile because Linux CI does not need it.

---

## Hazards, learned the hard way

**Never round-trip a source file through PowerShell string handling.** `Get-Content -Raw`
without `-Encoding utf8` reads UTF-8 as ANSI; writing it back produces one confusable character
per byte. It happened once, corrupting 232 characters in a spec and adding a *doubly*-encoded
layer to a test module. The files still parsed and still passed every other test.

Use the Edit/Write tools, or Python with explicit `encoding="utf-8"`.
`tests/unit/test_source_encoding.py` now catches it, but repair means peeling the encoding
layers byte by byte — hand-editing the visible characters leaves the underlying bytes wrong.

**`uv sync` fails inside OneDrive without `UV_LINK_MODE=copy`.** The repo lives under
`OneDrive\Desktop`, and uv's default hardlinking hits
`os error 396: The cloud operation cannot be performed on a file with incompatible hardlinks`.
Export `UV_LINK_MODE=copy` before any `uv sync` that installs new packages. Reads and
`uv run` are unaffected, which is why this only appeared at T-018 — the first ticket to add
a dependency in a while.

**Do not use PowerShell here-strings for commit messages.** Embedded quotes break the parse.
Write the message to a file in the scratchpad and use `git commit -F <file>`.

**Do not `curl` the GitHub API and dump the response.** It is enormous. Pipe it through
`python -c` and print only the fields needed.

---

## Per-ticket loop

From `docs/ENGINEERING-RULES.md` §2. One ticket at a time.

```
read the PLAN.md sections the ticket names, and its specs
  → restate the acceptance criteria; list any spec ambiguity found
  → probe anything the spec asserts but has not verified
  → write the tests first — confirm they FAIL
  → implement
  → .\make.ps1 check
  → update docs/ if a contract changed
  → append to docs/DECISIONS.md if a non-obvious choice was made
  → commit with -F, push, confirm CI
```

**Commits carry no co-author trailer.** Subject line `T-0XX: <imperative summary>`. The body
should explain *why*, and name anything measured.

---

## Standing expectations

- **Tests before implementation**, and confirm they fail. A guard never seen to fire is not a
  guard.
- **Coverage on `agentiam-core` is 100% of statements.** Keep it. The Definition of Done says
  ≥85%; the correctness core has held 100% since T-005 and that is worth protecting.
- **A red property test is not automatically an implementation bug.** In T-009, two of four
  failures were the test building chains past `max_depth`. Read the falsifying example before
  changing the implementation.
- **Report failures with the output.** If something is skipped, say so.
- **Every deviation from `PLAN.md` needs an ADR** with its cost stated, and a supersession note
  in the plan pointing at the spec that now governs.

---

## Ticket arithmetic

61 tickets defined · 8 fully deferred · **53 in scope** · 24 done.

T-010 is one of the deferred ones — `PLAN.md` marks it `[DEFERRED — see §21]`, so T-011 follows
T-009 by the plan, not by choice. ADR-006 strengthened the reason with measurement.
