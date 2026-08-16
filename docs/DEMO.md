# AgentIAM — Demo & Presentation Guide

> Everything related to the live BIIN demo, failure drills, judge handling, and rehearsal. Separated from the implementation plan to keep concerns clean.

---

## 1. Demo Runbook — 8 Beats in 10 Minutes

Every beat is scripted, deterministic, and individually runnable. `make demo-reset` returns to a known state in under 10 seconds.

| # | Beat | Time | What the judge sees |
|---|---|---|---|
| 0 | Setup | 0:00–0:30 | Console open, identity tree empty, Grafana on a second screen |
| 1 | Human approves a task | 0:30–1:30 | "Procure 500 units, budget ৳500,000." Root token minted. Intent hash shown. |
| 2 | Delegation tree grows | 1:30–2:30 | Root spawns 3 sub-agents, animated. Each node's scopes visibly smaller. Biscuit block ids shown. |
| 3 | Least privilege enforced | 2:30–3:30 | The doc-reader attempts a payment. **Denied in 0.4 ms**, naming the exact caveat. Latency panel visible. |
| 4 | **Judge sets the ceiling** | 3:30–5:00 | Hand over the keyboard. They set ৳50,000. Agent spends up to the line across 3 concurrent sub-agents and hard-stops. **Invariant checker green throughout.** |
| 5 | **Judge writes a policy in English** | 5:00–6:30 | Their sentence → Cedar → generated tests → pass/fail table → decision diff → activate → enforced on the next call. |
| 6 | Goal drift | 6:30–7:30 | Inject an instruction redirecting the agent. Drift score crosses the band. **Escalation raised to a human, not a silent block.** |
| 7 | Revocation | 7:30–8:15 | Revoke the root. 12 agents across 3 PEPs die. Timer on screen shows the actual propagation time. |
| 8 | Chain of custody | 8:15–9:30 | "Who authorized this payment?" → full chain to the human and the permitting caveat. Verify the audit chain live. Then tamper with a record and watch verification catch it. |
| 9 | Close | 9:30–10:00 | Three numbers, the OSS repo, the APICTA export story. |

**Beats 4, 5, and 8 are the ones they will remember.** Beat 4 in particular: a judge setting a limit with their own hands and watching a system refuse to exceed it is a fundamentally different experience from watching a slide about it.

---

## 2. Failure Drills

Rehearse **every single one** of these before the presentation. Each must have a documented recovery under 30 seconds.

| # | Failure | What happens | Recovery |
|---|---|---|---|
| F-1 | No internet | **Beat 5 only.** Since ADR-040 the NL→Cedar compiler calls hosted inference; everything else — tokens, decisions, budgets, ledger, audit chain, Postgres, Redis — is local and unaffected. | Set `AGENTIAM_LLM_BACKEND=ollama` and restart the console: the compiler falls back to the local model. Narrate honestly: "enforcement never left this machine; only the English-to-policy step is hosted, and it fails over to on-device inference." Rehearse this — the local model is slower (ADR-038), so know what the wait looks like. |
| F-2 | Ollama slow or down | Beat 5 (NL→Cedar) hangs. | Template fallback engages automatically (T-031). The flow is identical. Narrate: "the template fallback just activated — this is a production-grade failsafe." |
| F-3 | Postgres restarts mid-demo | PEPs fail closed. No spend goes through. | Narrate: "Fail-closed is the correct behaviour for a financial system. Watch the PEPs resume when the DB comes back." Then wait 10 seconds. |
| F-4 | Judge input breaks the parser | Beat 5: judge types something the compiler can't parse. | Clarifying-question path engages. Also narrate as a feature: "rather than guessing, the system asks for clarification." |
| F-7 | You lose your place mid-demo | Solo, there is no second presenter to pick up the thread while you recover. | Each of the 8 beats is an independently runnable scenario. Reset with `make demo-reset`, name the beat out loud, and re-enter at that beat. Rehearse re-entering at beats 4, 5, and 8 cold. |
| F-5 | Demo machine dies | Everything lost. | Second laptop with the same compose stack, pre-warmed, **already connected to the projector via a switch or a second input** — solo there is nobody to re-cable while you keep talking. Switch inputs and keep going. Rehearse the switch itself, not just the setup. |
| F-6 | Projector resolution change | Console text too small / layout broken. | Test at 1024×768 beforehand. The console must be readable at that size. Use browser zoom if needed. |

### Fallback of last resort

Record a 90-second screencast of the full demo on a phone. If both machines die, play the video and narrate over it. This has never not worked.

---

## 3. Judge Archetypes & How to Handle Them

BIIN panels typically include 4 judge profiles. Know what each one wants to hear:

### 3.1 Bank CTO
**Cares about:** fail-closed behaviour, audit chain, latency numbers, partition semantics, rollback procedure.

**Lead with:** "Under network partition, the system continues operating within its pre-approved lease, then fails closed. This is CP, not AP — the correct choice for money."

**Show them:** Beat 8 (chain of custody), Beat 4 (hard budget stop), Grafana latency panel, the invariant checker.

**One-pager content:**
- Fail-closed default with configurable fail-open per scope
- Audit chain with cryptographic verification
- Decision latency p99 (in-process, honestly separated from proxy overhead)
- Lease protocol partition behaviour: bounded spend, then deny
- Rollback procedure documented

### 3.2 Payments Executive
**Cares about:** mandate ceilings, hard stops, reconciliation, chain of custody for a disputed transaction.

**Lead with:** "Every payment traces back to a human who approved the task, the exact caveat that permitted it, and the exact budget state before and after."

**Show them:** Beat 4 (spend ceiling), Beat 8 (custody query), the `/audit/custody` API response.

**One-pager content:**
- Mandate ceilings enforced under concurrency
- Hard stops: denied with exact reason, never silent pass
- Reconciliation: every reservation tracked, refunds exact to 4 decimal places
- Custody chain: human → task → agent → sub-agent → caveat → action

### 3.3 Professor (Academic Judge)
**Cares about:** invariants, the lease safety argument, drift calibration, property testing, formal correctness.

**Lead with:** "We have 10 formal invariants for the attenuation semantics. All are property-tested with Hypothesis. The lease protocol has a written safety proof."

**Show them:** Property test output, the invariant checker, the formal invariant table, `docs/specs/04-lease-protocol.md` (safety argument).

**One-pager content:**
- 10 invariants (INV-1 through INV-10)
- Property tests with Hypothesis (stateful machine for P-10)
- Lease protocol safety proof: `committed + leased ≤ total` always
- Partition behaviour: CP choice, liveness bound
- Known limitations stated honestly (bearer replay, stranded lease window, slow-drift evasion)

### 3.4 Trade/Commerce Judge
**Cares about:** exportability, TAM, pricing, deployment model, market opportunity.

**Lead with:** "The protocol needs no localization. It's a single URL change to install via the MCP gateway. The TAM is every organization deploying AI agents with tool access."

**Show them:** The architecture slide, the APICTA roadmap slide, the IP statement.

**One-pager content:**
- Exportability: protocol-based, no localization needed
- Target economies: Indonesia, Vietnam, Malaysia (AI agent adoption is exploding)
- Pricing model: open-core, hosted control plane
- Deployment: single VM via docker compose, or Kubernetes
- IP: 100% BD development, all OSS, no black-box dependencies

---

## 4. The Three Numbers to Lead With in the Close

These are the three numbers you put on the closing slide. Each is measured, not claimed:

1. **Decision latency p99 (in-process)** — proves the architecture works at scale
2. **Budget invariant held across N chaos runs** — proves correctness under failure
3. **Revocation propagation p99** — proves control when things go wrong

---

## 5. Presentation Structure

Per BIIN report requirements, the pitch should follow this structure:

1. **Hook** (30s) — "We built the credit limit and chain of custody for AI agents."
2. **Live demo** (8 min) — The 8 beats above. This is the core.
3. **Architecture deep dive** (5 min) — Component diagram, the two-layer auth model, the lease protocol, the 10-step pipeline.
4. **Business model** (3 min) — TAM, pricing, open-core, deployment model.
5. **APICTA roadmap** (2 min) — Target economies, protocol exportability, no localization needed.
6. **Q&A** (variable) — See judge archetypes above.

---

## 6. Pre-Presentation Checklist

### Technical
- [ ] `docker compose up` reaches healthy on the demo machine
- [ ] `make demo-reset` completes in under 10 seconds
- [ ] All 8 beats individually runnable
- [ ] Grafana dashboards show live data
- [ ] Invariant checker shows green
- [ ] Ollama responds to a test inference
- [ ] Console readable at 1024×768

### Hardware
- [ ] Primary machine tested and warm
- [ ] Second machine tested and warm (same compose stack)
- [ ] **Both machines connected to the display simultaneously** — HDMI switch, or two projector inputs. Switching must be one action, not a re-cabling job
- [ ] The switch itself rehearsed under time pressure
- [ ] Power cables for both machines
- [ ] HDMI/USB-C adapter tested with the venue projector
- [ ] Phone with screencast fallback video, charged, volume tested

### Content
- [ ] 3 judge-facing one-pagers printed
- [ ] Closing slide with the 3 numbers
- [ ] BIIN compliance statement ready (100% BD dev, own IP, OSS, no black-box)
- [ ] Limitation slide ready (bearer replay, stranded leases, slow-drift evasion)

### Rehearsal
- [ ] Full demo run-through at least 3 times
- [ ] All 7 failure drills rehearsed at least once
- [ ] Cold re-entry rehearsed at beats 4, 5, and 8 (F-7)
- [ ] Timed: stays under 10 minutes
- [ ] Q&A prep: answers for bearer-token replay, Python latency, partition semantics
