# 19 — Testing

*How we know any of this works.*

The short version: **about 2,700 tests in eight distinct kinds**, each kind existing because the
others cannot catch a particular class of bug. This file explains what each one is for and what
it actually caught.

---

## 1. The numbers

| Suite | Roughly | Runs where |
|---|---|---|
| Unit | 58 files | Every push |
| Property | 4 files, thousands of generated cases | Every push |
| Security / red team | 5 files | Every push |
| Integration | 28 files, 267 with e2e | Every push, against real Postgres |
| End to end | 3 files | Every push, full stack |
| Chaos | 5 scenarios | Nightly |
| Performance | 3 files | Every push (benchmark), on demand (load) |
| Doc-drift and infrastructure gates | several | Every push |

`make test` runs everything that needs no infrastructure — currently **2,434 passing**.
`make test-integration` and `make test-e2e` add **267** more against real containers.

Nine CI jobs on every push: `quality`, `integration`, `e2e`, `purity`, `evidence-pack`,
`security-scan`, `infrastructure`, `demo-stack`, and `chaos` on a nightly schedule.

---

## 2. Unit tests — the ordinary ones

Fast, no infrastructure, one behaviour each. The bulk of the suite.

They lean hard on `agentiam-core` being pure. Because core does no I/O and reads no clock, a
test can construct any situation exactly — a specific instant, a specific budget state — and
assert on the result without mocks. **That purity rule is a testing decision as much as an
architectural one.**

The naming style is a sentence: `test_a_lease_inside_the_skew_margin_will_not_reserve`. You can
read the test list as a specification.

---

## 3. Property tests — for claims about *all* inputs

Written with Hypothesis. Where a unit test says "this input gives this output", a property test
says "**for every** token and **every** set of caveats, this relationship holds."

That is the only honest way to test invariants like:

> `authority(child) ⊆ authority(parent)` — for every parent, every caveat set.

Hypothesis generates thousands of cases and, on failure, shrinks to the smallest one that still
breaks.

**The important discipline here** is that the properties are derived from the specification,
which was written to make that possible. Spec 03 states every invariant formally *and* gives
counterexamples describing what would violate it. A property test derived from a wrong invariant
statement proves nothing — it just proves you are consistent with your own misunderstanding.

There is also a test **of the generators** (`test_strategies.py`), auditing that the strategies
actually produce the shapes they claim. A property test that only ever generates trivial cases
passes forever and means nothing.

---

## 4. Red-team tests — for the adversary

An explicit catalogue of attacks, named `A-01` through `A-33`, mapped to threat-model entries.

They cover: forging a block, stripping a block, reordering, splicing two chains, replaying an
expired token, a child claiming a scope beyond its parent, sibling budget races, self-approval,
approval widening, parameter pollution, log injection, and more.

Two of these files exist because two threats needed **66 test cases each** on their own:
`test_datalog_labels.py` for crafted identifiers (TM-24) and `test_parameter_pollution.py` for
the two-parsers problem (TM-26).

**Nine of the twenty-seven threats were found by measurement rather than brainstorming** —
TM-19 through TM-27. Those are the interesting ones, and [file 20](20-hard-problems.md) tells
their stories.

---

## 5. Integration tests — for the things only a real database does

Run against real Postgres and Redis via testcontainers, because the bugs they catch cannot
appear against a mock:

- `SELECT … FOR UPDATE` actually serializing concurrent lease acquisitions
- 50 concurrent acquires bounded correctly
- The commit-idempotency race
- Chain append under concurrent writers
- Real OIDC login against a real Keycloak

**A note that cost a milestone.** For a while, CI ran **none** of the integration tests — `make
test` and the workflow both excluded the marker. Three tickets of ledger correctness were gated
by nothing and would have rotted silently. Now there is a dedicated CI job.

If you have a marker that excludes tests, check that *something* runs them.

---

## 6. End-to-end tests — for the seams

The full stack, a real token, a real HTTP call, a real refusal.

These exist because the most expensive bugs in this project were **not inside components**.
They were in the wiring between components that were each individually correct. An e2e slice is
the cheapest thing that exercises a seam.

---

## 7. Chaos tests — for the failures you cannot schedule

Fault injection against a running stack. Five scenarios:

| Scenario | What it breaks |
|---|---|
| CH-1 | Postgres goes away mid-flight |
| CH-3 | The PEP is `SIGKILL`ed holding a lease |
| CH-4 | Network partition between PEP and control plane |
| CH-8 | The embedding model black-holes |
| CH-10 | Rolling restart under load |

They use an **in-process fault proxy** rather than an external tool like toxiproxy (ADR-050) —
fewer moving parts in CI, and the injection point is exactly where the code under test is.

**What chaos caught that nothing else did:** the double-spend. CH-10 measured 992 requests
spending ৳4,960 while the ledger recorded `committed = 0`. No unit, property or integration test
had a chance — each component was behaving correctly, and the invariant checker was comparing
two numbers that were both wrong in the same way.

---

## 8. Performance tests — for the claims with numbers on them

Two kinds, reported separately and never conflated:

- **Micro-benchmarks** (`pytest-benchmark`) for NFR-1: the in-process decision, p99 under 1 ms.
  One runs on every push, so a regression is caught immediately.
- **Load tests** for NFR-2: end-to-end proxy overhead at 500 requests/second, profiled with
  `py-spy`.

The reporting discipline matters as much as the measurement. Results are published as a
**range**, not a single flattering number (ADR-052), and the two figures are always labelled
separately because Python cannot proxy a request in under a millisecond even though it can
decide one.

There is also a **published-number drift check**: the committed benchmark document must match
its source data, or CI fails.

---

## 9. The gates that are not really tests

Five checks that guard properties no assertion in a test file would catch.

**Core purity.** Walks the AST of every core source file and fails if it imports anything that
does I/O or reads a clock. Static rather than runtime, because a runtime import check would miss
a lazily imported violation inside a function body — which is exactly where one would appear.

**The 51-case policy corpus.** Every policy bundle must pass it before activation. Each case
explains *why* its expected outcome is correct.

**Doc-drift checks.** Several documents are generated from data, and CI asserts they still
match. This is how a stale evidence pack gets caught.

**Secret scanning, at two layers.** A pattern scan of the repository, plus a scanner that reads
the **actual log output** of a running service (NFR-5). The second one found a real secret leak
in shipped code that the first could not see.

**Compose and manifest checks.** The demo stack's environment variables must match what the
scripts write. A drifting filename fails closed but late — the PEP refuses to start, in CI, at
the worst moment. A cheap test catches it early.

---

## 10. Coverage, and the rule that is kept

`agentiam-core` is held at **100% of statements**. The whole tree sits around 98%.

**Coverage is reported but not yet gated**, and that is written down as a known gap rather than
implied to be fine. It has already slipped once — core dropped to 96% during a ticket and was
caught by hand, not by CI. One `fail_under` line would fix it.

**Mutation testing is specified and not yet run.** Coverage says the lines executed; mutation
says the assertions bite. Also written down as a gap.

---

## 11. Five lessons about testing itself

These generalise well beyond this project.

**A passing test proves nothing if it supplies the missing input itself.** Every one of the 51
corpus cases passed while the deployed policy engine had no tool catalogue, because every case
built its own engine with the catalogue in hand. Nobody asserted the *deployed* object had one.
When a component takes a dependency as an argument, test the composition root, not just the
component.

**Ask what the test would do if the feature were absent.** The invariant checker asserted
`committed == Σ settled`, which held as `0 == 0` while money vanished. If your check would still
pass with the feature deleted, it is not testing the feature.

**A test that passes by finding nothing is the most dangerous kind.** Two log-assertion tests
went vacuous when the suite ran in one process. They were green. They were checking an empty
list.

**Do not mark a flaky test as known-flaky before you know why.** The intermittent INV-1
violation looked like test flakiness and was a real product bug: under load, legitimate requests
were being refused because a library's default timeout was wall clock. Marking it flaky would
have shipped it.

**Correct the test to the spec, not the spec to the test.** One endpoint returned 200 with an
empty list where the acceptance case required a 404 — and the existing test asserted the
*deviation*, so the requirement read as covered while being unmet. When code and spec disagree,
decide which is right on the merits; do not let whichever was written first win by default.

---

## 12. Running them

```bash
make test               # everything with no infrastructure  (~2,434)
make test-unit          # unit and property only
make test-integration   # needs Postgres and Redis
make test-e2e           # needs the full stack
make chaos              # fault injection
make bench              # NFR-1 micro-benchmarks
make check              # lint + types + test, everything CI runs
```

Lint is `ruff`, types are `mypy --strict`. Both clean is the baseline, not an achievement.

---

## Next

[20 — Hard problems](20-hard-problems.md) collects the bugs worth remembering.
