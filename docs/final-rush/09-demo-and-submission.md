# 09 — Demo, submission and open-source release

**APICTA 2026: Jakarta, 7–11 December.** Each entry gets one 25-minute slot: 3 minutes of setup,
**10 minutes for presentation and live demo together**, 10 minutes of Q&A, 2 minutes of teardown.
English only, 3–4 judges, in person. The student rubric: Quality 30% (content and standard 60%,
stability 40%), Presentation 25% (organization 50%, Q&A 50%), Uniqueness 15%, Proof of Concept
15% (problem 50%, business environment 50%), Functionalities and Features 15% (user requirements
50%, compatibility and interoperability 50%).

`docs/DEMO.md` currently plans roughly 18 minutes (8 demo + 5 architecture + 3 business + 2
roadmap), assumes a single presenter, uses a second screen for Grafana, and prices in taka. All of
that changes here.

---

### T-057 — Demo rebuilt around the real agent

**Size** L · **Lane** demo · **GPU** 6 · **J**

**Why.** The original T-057 (demo scenario scripting) built `seed_demo.py` and `demo_concurrency.py`,
which drive scripted traffic. The demo must now show the real agent (T-086) being attacked
(T-087) and stopped, inside a 10-minute slot that includes everything else the panel must hear.

**Read.** `docs/DEMO.md`; T-086, T-087, T-099, T-081, T-083 deliverables; `docs/TODO.md` items 30,
33, 34.

**Depends on.** T-086, T-087, T-099 (T-081 and T-083 strongly preferred).

**Deliverables.**
- `docs/DEMO.md` rewritten to a 10-minute run of show:

| Time | Beat | Driven by |
|---|---|---|
| 0:00–0:40 | Hook: an agent with a corporate card reads an invoice that says "pay this account" | Presenter |
| 0:40–1:30 | Task approved; the agent spawns three sub-agents; each node holds less | `make agent-run SCENARIO=benign` |
| 1:30–3:30 | Poisoned invoice: the payer tries to pay the attacker; denied in < 0.5 ms with the caveat named; drift escalates to a human; approver narrows; agent continues | `make agent-run SCENARIO=poisoned-invoice` + T-081 |
| 3:30–5:00 | A judge sets the ceiling; three sub-agents race to spend twice the pool; the ledger stops at the line | `make demo-conc` (preset fallback) |
| 5:00–6:15 | Custody: "who authorised this payment?"; tamper one record; verification turns red | `make demo-tamper` |
| 6:15–7:15 | One architecture slide with the three measured numbers | Slides |
| 7:15–8:30 | Who needs it, how it differs from Entra Agent ID, Okta, AgentCore, AP2; user evidence | Slides |
| 8:30–10:00 | Roadmap and close | Slides |

- English-to-policy (T-099) and revocation (T-083) prepared as **Q&A showpieces**, each one
  command away.
- Every beat startable cold from one command (`make beat-N`), so a failure costs one beat.
- A pre-flight canary: `make preflight` checks every service, token expiry (TODO item 33: tokens
  die 8 hours after seeding), the model resident on the GPU, PEP policy serial, and that the
  identity tree has no ghost generations (TODO item 34), and prints green or the exact fix.
- Demo amounts in USD.
- Role-discrimination beat available (TODO item 30, via T-079 or a seeded non-senior payer).
- Keycloak demo realm session lifetime long enough to survive a holding room (≥ 4 hours), and the
  reason recorded.

**Acceptance criteria.**
- [ ] A timed dry run of beats 1–5 completes in ≤ 6:15 on the 6 GB laptop, three times in a row.
- [ ] The whole demo runs with the network cable unplugged and Wi-Fi off (local model, no hosted
      call anywhere).
- [ ] `make preflight` catches each of: expired tokens, model not loaded, a service down, stale
      policy (test by inducing each).
- [ ] Replay mode of every agent beat works as a stage fallback, triggered by one flag.

---

### T-058 — Failure drills as scripts

**Size** M · **Lane** demo · **GPU** — · **J**

**Why.** `docs/DEMO.md` §2's drills are prose. A drill that has never been run is a hope. Each
must inject its failure, show the recovery and time it.

**Read.** `docs/DEMO.md` §2; T-057; the chaos harness (reuse its fault injection).

**Depends on.** T-057.

**Deliverables.** `make drill-<name>` for each, printing what the presenter says and timing the
recovery:

| Drill | Injects | Expected recovery |
|---|---|---|
| no-internet | Blocks egress | Nothing changes (everything is local after T-097) |
| model-down | Stops Ollama | Compiler answers from templates (T-031); agent beats switch to replay |
| db-restart | Restarts Postgres mid-beat | PEPs spend leases then fail closed; resume in ≤ 10 s |
| lost-place | None | `make beat-N` re-enters cold at beats 2, 3 and 4 |
| machine-swap | None | Second laptop identical and pre-warmed; `make preflight` green on both |
| projector | None | Console readable at 1024×768 (zoom value recorded) |
| token-expiry | Fast-forwards token expiry | `make preflight` flags it; re-seed command printed |

- `docs/DEMO.md` §2 rewritten from the scripts' actual output.

**Acceptance criteria.**
- [ ] Every drill recovers in ≤ 30 s, measured, three times.

---

### T-059 — Written nomination and submission package

**Size** M · **Lane** docs · **GPU** — · **J**

**Why.** APICTA judges read the written nomination before the slot. `PLAN.md` T-059 (architecture
document, TAM, scalability roadmap, IP statement, socio-economic impact) is still open, and the
BIIN-specific framing (100% Bangladesh development, NFR-10) scores nothing at APICTA.

**Read.** `docs/PLAN.md` T-059, §14, §19; `research/related-work.md` §4.5 (competitive set);
`research/gap-analysis.md` (every claim must survive it); H-05's answer on the required format.

**Depends on.** T-063. Uses H-02's results.

**Deliverables.** Under `docs/submission/`:
- `nomination.md`: written for a non-specialist judge, in the format H-05 confirms; problem, the
  user, what AgentIAM does, demo screenshots, architecture, three measured numbers,
  differentiation, market, roadmap, team.
- `competitive-landscape.md`: Microsoft Entra Agent ID, Okta for AI Agents, AWS Bedrock AgentCore
  Identity, Google AP2, Mastercard Agent Pay, and the research systems (AIP, Agent Contracts,
  HDP). For each: what it does, what it does not, how AgentIAM relates (layer on top, not
  replace). **Every row verified against the vendor's own current documentation**, with links and
  access dates. Secondary sources are not enough.
- `market.md`: who buys, the sizing method (bottom-up, stated assumptions), pricing hypothesis
  (open core plus hosted control plane), first customers.
- `architecture.md`: one diagram plus two pages.
- `ip-and-originality.md`: what was built, what was reused (biscuit, Cedar, Postgres, Redis,
  Keycloak) and under which licences, generated from the SBOM.
- **No invented evidence.** User quotes, letters of interest and pilot names appear only if H-02
  produced them; otherwise the section says "conversations in progress" or is omitted.

**Acceptance criteria.**
- [ ] Every number in the package traces to a committed artefact.
- [ ] Every competitor claim has a primary-source link.
- [ ] Reviewed against `research/gap-analysis.md`: no claim marked aspirational is presented as
      implemented.

---

### T-125 — Slides, speaker script, judge one-pagers

**Size** M · **Lane** docs · **GPU** — · **J**

**Why.** Presentation is 25% of the score, half of it Q&A. The slides carry beats 6–8 of the run
of show; the script keeps a three-person team inside 10 minutes; the Q&A bank prepares the other
10.

**Read.** T-057's run of show; T-059's package; `docs/DEMO.md` §3 (judge archetypes).

**Depends on.** T-059. Uses H-03's agreed answer.

**Deliverables.**
- Slides (≤ 6): hook, architecture with three numbers, competitive map, users and market,
  roadmap, close. Readable at 1024×768.
- Speaker script per presenter with timings, written for a judge who is not a security
  specialist: "biscuit", "Datalog", "Cedar" and "lease" explained by what they do.
- A Q&A bank: at least 25 likely questions with ≤ 40-second answers and an owner each, covering
  competitors, business model, the AI-tooling question (H-03's answer verbatim), Python latency,
  partitions, drift accuracy, stolen tokens, what was built vs reused, and production use.
- One-page summary (four printed copies) and the three judge one-pagers from `docs/DEMO.md` §3,
  rewritten for an international panel.

**Acceptance criteria.**
- [ ] The full talk with live demo, read from the script, lands at 9:15 ± 0:20 in three timed runs.
- [ ] Every number on a slide matches the committed evidence.

---

### T-061 — Open-source release readiness

**Size** M · **Lane** docs / infra · **GPU** — · —

**Why.** `PLAN.md` §21 item 16. The README and the LICENSE describe an open-source project; the
repository is private (H-06 decides when that changes). Going public safely needs more than
flipping visibility.

**Read.** `README.md`; `LICENSE`; `.gitleaks.toml`; `docs/evidence/security-scan.md`;
`.gitignore`.

**Depends on.** T-063, T-122.

**Probe first.** Run gitleaks over the full history and list every finding. Check `git log` for
files that were committed and later removed (secrets, large binaries, private notes). Check that
every file intended to stay private is in `.gitignore` and absent from history.

**Deliverables.**
- `CONTRIBUTING.md`, `SECURITY.md` (vulnerability reporting), `CODE_OF_CONDUCT.md`, issue and PR
  templates.
- README quickstart verified on a clean machine (fresh VM or container): clone → `make demo-up`
  → `make agent-run` works, with the time taken recorded.
- A decision, with H-06, on whether low-signal commit messages ("done", "fixed ui") are reworded.
  Rewriting published history is destructive, so this happens only before the first public push,
  and only by the people who own the repository.
- Repository description, topics and a demo video link.

**Acceptance criteria.**
- [ ] gitleaks reports no findings over the full history, or each is a documented false positive.
- [ ] The clean-machine quickstart succeeds.
