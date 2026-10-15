# 00 — Truth and hygiene

Cheap tickets that make every later ticket safer. T-062…T-064 are on the Jakarta-critical
path; the rest can run in any gap.

---

### T-062 — Confirm and measure the demo-concurrency work (`dca459f`)

**Size** S · **Lane** docs/tests · **GPU** — · **J**

**Why.** The code landed in `dca459f` (`make demo-conc`, plus the `SessionLedger` fix that
makes a spent mandate report `BUDGET_EXHAUSTED_MANDATE`). `make check` (2,621 tests) and
`make test-integration` (270) passed on it before the push. What remains is the part that was
never done: measuring it on the running stack, documenting it, and confirming CI.

**Read.** `scripts/demo_concurrency.py` module docstring; `docs/STATUS.md` gap 25.

**Probe first.** Run `make demo-up` then `make demo-conc` once and record: whether the
ledger total held, how many requests were refused with `BUDGET_EXHAUSTED_MANDATE` vs
`LEASE_UNAVAILABLE`, and **how long the three-PEP recreate takes**. T-076 needs that number.

**Deliverables.**
- `docs/DEMO.md` beat 4 pointing at `make demo-conc`.
- `docs/RUNNING.md` entry for `make demo-conc`.

**Acceptance criteria.**
- [ ] The CI run for `dca459f` (or its successor) is green, including the `demo-stack` job.
      If it is red, fixing it is this ticket.
- [ ] The PEP recreate time is recorded in the commit message.

**Out of scope.** Lifting the one-mandate restriction (T-076).

---

### T-063 — Reconcile every public claim with the running system

**Size** M · **Lane** docs/tests · **GPU** — · **J**

**Why.** An outside reader meets the README first, and parts of it are false. The research
audit (`research/gap-analysis.md`) and the APICTA assessment found these on 2026-10-16. A
judge or reviewer who catches one discounts everything else.

**Read.** `research/gap-analysis.md` §1–§7; `research/threat-model.md` §6; `README.md`;
`docs/DEMO.md`; `docs/PLAN.md` §1.5, §6.8, §8, T-044; `docs/STATUS.md` §1.

**Probe first.** Run each check below against the tree and record the current wording before
changing it. Some may already have been fixed.

**Deliverables.** Edits to these claims (keep the history honest, so a superseded claim gets a
dated supersession note rather than silent deletion where a document is a record):

| Where | Claim | Change to |
|---|---|---|
| `README.md` Status | "Milestone 1 — Foundation, in progress" | The real state: milestones done, tickets done, link to `docs/STATUS.md` |
| `README.md` ~line 104 | "no proprietary black-box API … all model weights open-weight and self-hosted" | "Local open-weight inference is the default; hosted inference is an optional, off-by-default backend (ADR-040)" |
| `README.md`, `docs/DEMO.md` §1 | "the OSS repo" | Per H-06: either keep (repo public) or remove |
| `docs/PLAN.md` NFR-10 | "all model weights open-weight and self-hosted" | Superseded note pointing at ADR-040 |
| `docs/specs/04-lease-protocol.md` header | "model-checked" | "hand-proved; validated by randomized interleaving with guard ablation (T-101); exhaustive model check pending (T-100)" |
| `research/threat-model.md`, `docs/threat-model.md` | "non-repudiation" | "tamper-evidence" until T-108 lands |
| `docs/threat-model.md` TM-22, TM-08, TM-14 | cite CH-7, CH-2, CH-11 | cite what actually ran; mark the scenario as pending T-102/T-104 |
| `docs/PLAN.md` T-044 rationale | "mTLS in place" | "No TLS between services today; T-109" |
| `docs/PLAN.md` §6.8 | signed checkpoints | "not implemented; T-108" |
| `docs/PLAN.md` §8 | "Full OpenAPI … committed to `docs/openapi/`" | "T-123" until it is |
| `docs/DEMO.md` beat 5 | "activate → enforced on the next call" | "activation reaches the PEP once T-072 lands", or leave once it has |
| `docs/DEMO.md` §2, §6 | solo presenter | three presenters: presenter, driver, Q&A lead |
| `docs/DEMO.md` beats | ৳ amounts | USD with ৳ in brackets, or USD only |
| `docs/DEMO.md` §3.3 | "10 invariants … all property-tested" | "all ten stated formally, each with named test coverage" (G-1) |
| `docs/STATUS.md` §1 | 57 ADRs, 2,464 tests | the real numbers |

- **`tests/unit/test_doc_claims.py`** — a test that makes the numbers impossible to go stale:
  - the ADR count stated in `docs/STATUS.md` equals the number of `## ADR-` headings in
    `docs/DECISIONS.md`;
  - `README.md` contains no "Milestone 1 — Foundation, in progress";
  - every `CH-n` cited in `docs/threat-model.md` has a matching `tests/chaos/test_chNN_*.py`
    file, or the citation carries the word "pending";
  - every path written as `` `docs/...` `` or `` `scripts/...` `` in `README.md` exists.

**Acceptance criteria.**
- [ ] Every row of the table above is resolved, or marked "kept, still true" with evidence.
- [ ] `test_doc_claims.py` fails on the pre-change tree (confirm by stashing the doc edits) and
      passes after.
- [ ] `make check` green; CI green.

**Out of scope.** Fixing the underlying gaps; each has its own ticket. This ticket only makes
the words match the system.

**Hazard.** Edit files with the file tools or Python with explicit `encoding="utf-8"`.
`tests/unit/test_source_encoding.py` will catch mojibake; do not round-trip through a shell
`sed` on lines containing §, —, or ৳.

---

### T-064 — Pin assumption A1 with a CI test

**Size** S · **Lane** docs/tests · **GPU** — · **J**

**Why.** The whole attenuation design rests on biscuit scoping block facts so a later block
cannot widen authority (`docs/threat-model.md` §4, assumption A1). It is verified by hand,
never by CI (`docs/STATUS.md` §4.1, gap 6). A `biscuit-python` upgrade that changed it would
collapse INV-1 silently.

**Read.** `docs/STATUS.md` §4.1; `docs/specs/01-token-format.md`; `docs/specs/03-attenuation.md`;
`docs/threat-model.md` §4.

**Probe first.** In a scratchpad, append a block to a real token that asserts each of
`operation`, `scope`, `requested`, `current_depth` and a budget fact, and confirm against the
installed `biscuit-python` that it authorizes nothing the earlier blocks narrowed out. Record the
library version.

**Deliverables.**
- `tests/security/test_assumption_a1.py`: for each fact name the authorizer or an earlier
  block's checks read, a test that mints root → narrowed child → hostile block injecting that
  fact, and asserts the request the child narrowed out is still refused.
- A test asserting the installed `biscuit-python` version equals the one pinned in `uv.lock`, so
  an upgrade forces someone to read this file.
- `docs/STATUS.md` gap 6 and §4.1 closed.

**Acceptance criteria.**
- [ ] One test per injected fact name; each test names the fact in its id.
- [ ] Proven load-bearing: build a deliberately wrong authorizer that reads facts from all
      blocks (the encoding TM-19 rejected) and show at least one test fails against it.
- [ ] Runs in `make check` (not marked integration).

**Out of scope.** Changing the token format.

---

### T-065 — Gate coverage in CI

**Size** S · **Lane** docs/tests · **GPU** — · —

**Why.** Coverage is reported but never enforced (gap 14). `agentiam-core` has slipped from
100% once and was caught by hand.

**Read.** `docs/STATUS.md` gap 14; `pyproject.toml`; `.github/workflows/ci.yml`.

**Probe first.** Run coverage per package and record the real numbers today.

**Deliverables.**
- Per-package thresholds: `agentiam-core` 100% statements; every other package at its measured
  value rounded down, never below 85% (ENGINEERING-RULES §3).
- CI fails if any threshold is missed.

**Acceptance criteria.**
- [ ] Deleting one test from a core module makes CI fail (confirm locally).
- [ ] Thresholds documented in `docs/STATUS.md` §1.

---

### T-066 — Property-test `to_datalog` / `evaluate` agreement

**Size** M · **Lane** docs/tests · **GPU** — · —

**Why.** Caveats are enforced twice: as Datalog inside the token and as Python `evaluate()`.
Their agreement is a security property (ADR-008) covered by 60 hand-written cases, not by a
search (`docs/STATUS.md` §4.2).

**Read.** `docs/specs/02-caveat-language.md`; ADR-008; `tests/property/` generators from T-009.

**Probe first.** Run the existing conformance suite and count clause types and operators it
exercises; list any that no case covers.

**Deliverables.** `tests/property/test_caveat_agreement.py` — Hypothesis over random caveats
(every clause type) and random request contexts, asserting `evaluate()` and the biscuit
authorizer agree on allow/deny for every draw.

**Acceptance criteria.**
- [ ] Covers every caveat clause type in spec 02.
- [ ] Proven to bite: introduce an off-by-one in one comparison in `to_datalog` and confirm the
      property finds it within the default example count.
- [ ] Any disagreement found is fixed in the code (or the spec plus an ADR), never by narrowing
      the strategy.

---

### T-067 — Mutation testing on attenuation and caveats

**Size** M · **Lane** docs/tests · **GPU** — · —

**Why.** T-009 requires ≤ 10% surviving mutants on `attenuation.py` and `caveats.py`; it has
never been run (gap 4). Coverage says lines run; mutation says assertions bite.

**Read.** `docs/STATUS.md` gap 4; `docs/PLAN.md` §10.2.

**Probe first.** Install `mutmut` in a throwaway venv, run it on one small module, and record
runtime, so the full run's cost is known before committing to a target.

**Deliverables.**
- `make mutation` target (excluded from `check`; optional nightly CI job).
- `docs/evidence/mutation.md` generated from the run: mutants, killed, survived, and a reason for
  every surviving mutant kept.
- New tests that kill survivors until ≤ 10% remain on both files.
- ADR for the `mutmut` dev dependency.

**Acceptance criteria.**
- [ ] ≤ 10% survivors on `attenuation.py` and `caveats.py`.
- [ ] Every surviving mutant listed with a one-line justification (equivalent mutant, etc.).

---

### T-068 — Consolidate test fixtures; clear deprecations

**Size** M · **Lane** docs/tests · **GPU** — · —

**Why.** Two copies of the container and migration fixtures drift apart (gap 10,
`tests/integration/test_budget_schema.py` vs `tests/integration/conftest.py`), four older unit
modules carry near-duplicate token fixtures (`docs/STATUS.md` §4.3), and `make check` emits 14
deprecation warnings, including `on_event` in `scripts/serve_pep.py:410`.

**Read.** `docs/STATUS.md` §4.3, gap 10; `tests/fixtures/tokens.py`.

**Deliverables.**
- One set of integration fixtures; `test_budget_schema.py` uses the shared conftest.
- The four older unit modules use `tests/fixtures/tokens.py`.
- The deterministic keypair and golden depth-4 chain from `PLAN.md` §10.4 added to fixtures.
- `serve_pep.py` moved to a lifespan handler; zero deprecation warnings from project code.
- `filterwarnings = error` for `DeprecationWarning` raised from project modules, so new ones fail.

**Acceptance criteria.**
- [ ] Test count unchanged or higher; no test deleted without a stated duplicate.
- [ ] `make check` shows no warnings originating in `packages/` or `scripts/`.

---

### T-069 — Randomize test order; chase order-dependent failures

**Size** S · **Lane** docs/tests · **GPU** — · —

**Why.** `pytest -p no:randomly` appears in commands but the plugin is not installed, and one
integration failure in CI was never explained (`docs/TODO.md` item 23). Order dependence is a
real bug class here (TODO item 19 was one).

**Read.** `docs/TODO.md` items 19 and 23.

**Deliverables.**
- `pytest-randomly` as a dev dependency (ADR), on in CI with the seed printed.
- Every order-dependent test found is fixed.
- `docs/TODO.md` item 23 closed or updated with what the randomized runs showed.

**Acceptance criteria.**
- [ ] Ten consecutive randomized runs of `make check` and five of `make test-integration` green.
- [ ] The seed of any failure is reproducible with `-p randomly -p "randomly_seed=N"`.
