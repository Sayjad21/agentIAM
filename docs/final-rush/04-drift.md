# 04 — Drift detection, done properly

**The problem in one line.** The deployed oracle is `RuleBasedDriftOracle`, a v0 heuristic.
Three of six features (f3, f4, f6) are deferred because they need history the stateless PEP
does not hold (ADR-036). The labelled dataset (T-034) and the calibrated classifier (T-035)
are deferred, so NFR-9's < 5% false-positive rate has never been measured. And the embedding
client is a *synchronous* `httpx.Client` on the event loop (`agentiam_pep/drift.py:88`), so a
hung Ollama stalls every concurrent request (gap 22, measured in CH-8).

**The human constraint.** The team will hand-label a gold set of about 200 pairs (H-01). Every
other label is produced by a reproducible pipeline: synthetic generation plus a local LLM judge,
whose accuracy is *measured against the gold set* and reported. That keeps the published
numbers honest: "classifier FPR X% against human labels, trained on N judge-labelled pairs whose
judge agrees with humans at κ = Y".

**Design rules that stay in force.**
- Drift never denies on its own (spec 06 §2.1). It allows, flags, or escalates to a human.
- Absent features are omitted, never stored as zero (spec 06 §5).
- Hold out **by task**, not by row (`PLAN.md` §6.6), or the numbers leak.
- Never log task or action text verbatim; digest and length only.

---

### T-092 — Drift off the event loop (gap 22) and CH-9

**Size** M · **Lane** drift · **GPU** 4 · **J**

**Why.** A black-holed embedding service blocks the PEP's event loop for the full timeout on
every uncached request: about three timeouts per request, measured. On stage, a stuck model
freezes all enforcement, not only drift.

**Read.** `agentiam_pep/drift.py` (`EmbeddingClient`); spec 06 §4; ADR-037;
`tests/chaos/test_ch08_ollama_down.py`; `docs/STATUS.md` gap 22. Also the hazards about
`asyncio.wait_for` not bounding a call blocked on a dead socket.

**Probe first.** Reproduce gap 22: point the PEP at a TCP listener that accepts and never
responds, send 20 concurrent requests, and record p50/p99 latency of requests that do **not**
need drift (a scope in `off` mode). That number must drop to normal after the fix.

**Deliverables.**
- `EmbeddingClient` moved to `httpx.AsyncClient` (or the sync client run in a bounded
  thread pool), with a total-time deadline, not only a connect timeout.
- A circuit breaker: after N consecutive failures, drift is reported unavailable without
  calling the service, until a half-open probe succeeds.
- Drift unavailable → behaviour per mode (spec 06 §3): `strict` scopes escalate, `log_only`
  allows, with the reason recorded.
- **CH-9** (embedding service down) written as a chaos scenario (`tests/chaos/test_ch09_embedding_down.py`),
  with a hung-listener variant of CH-8.
- `docs/STATUS.md` gap 22 closed; `research/gap-analysis.md` C-9 updated.

**Acceptance criteria.**
- [ ] With the embedding service hung, requests to scopes with drift `off` show no latency
      change (within 10% of baseline p99).
- [ ] With it hung, `strict` scopes escalate and `log_only` scopes allow, each within the
      deadline.
- [ ] The breaker opens and recovers (test with a service that comes back).
- [ ] CH-8 (hung variant) and CH-9 pass under `make chaos`; `chaos-results.md` regenerated.

---

### T-093 — Drift features f3, f4, f6

**Size** M · **Lane** drift · **GPU** 4 · —

**Why.** The missing three are the temporal signals, where slow drift (A-27) shows up. They need
per-task history. This ticket decides where that history lives without breaking the PEP's
statelessness.

**Read.** ADR-035, ADR-036; spec 06 §1.1, §5; `docs/PLAN.md` §6.6; `db/decisions.py`.

**Depends on.** T-092.

**Probe first.** Measure what a per-request read of task history would cost on the hot path:
query the last 20 decisions for a `task_id` from Postgres and from Redis under load, and record
p99. If either exceeds about 1 ms, the stateful features cannot be computed synchronously in the
PEP and must be computed asynchronously.

**Deliverables.**
- An ADR choosing one of: (a) PEP-local bounded per-task history in memory (lost on restart,
  per-PEP view only); (b) Redis per-task ring buffer; (c) asynchronous scoring in the control
  plane, with stateful features contributing to flag or escalate after the fact, never to the
  synchronous decision. State the cost of each and which one T-095 needs.
- `drift_observations` table (the one `PLAN.md` sketches), written for every scored decision.
- f3: scope-in-task-plan prior, from observed `(task_type, scope)` frequencies with a task type
  derived from the canonical intent. f4: sequence anomaly, from the action's position against
  observed sequences for that task type. f6: historical `(task_type, scope)` frequency.
- spec 06 §5 updated to six features; ADR-036 marked superseded.

**Acceptance criteria.**
- [ ] Each feature has unit tests including the no-history case (feature omitted, not zero).
- [ ] Hot-path p99 (NFR-1 benchmark) unchanged within noise, or the regression stated and
      justified in the ADR.

---

### T-034 — Drift dataset pipeline (synthetic + LLM-judge labels)

**Size** L · **Lane** drift / research · **GPU** 6 · —

**Why.** Deferred since M7 on the grounds that labelling takes weeks of human effort. Most of it
can be automated; what cannot is a gold set to measure the automation against (H-01, T-094).
This is the long, unattended job that can run during November.

**Read.** `docs/PLAN.md` §6.6 dataset paragraph and T-034; spec 06; the demo tool catalogue;
T-087's attack scenarios (they are drift pairs already).

**Depends on.** T-093 (so the dataset records all six features).

**Probe first.** Generate 50 pairs with the T-097 model and have the same model judge them.
Hand-check all 50 yourself and record agreement. If agreement is below about 80%, change the
judge prompt or the model before scaling up; a bad judge at scale produces a large, wrong
dataset.

**Deliverables.**
- `research/drift/generate.py`: task templates across at least 8 task types (procurement,
  invoicing, vendor onboarding, HR lookup, reporting, travel, support, data export), each with
  benign action sequences and drift variants: different-task actions, injected instructions,
  privilege escalation, exfiltration shapes, and gradual (slow) drift trajectories.
- `research/drift/judge.py`: LLM-judge labelling with a fixed rubric, the model and prompt
  hashed into every label record.
- Output: ≥ 2,000 pairs, ≥ 30% drift, versioned JSONL with a datasheet
  (`research/drift/DATASHEET.md`: sources, generation process, known biases, licence).
- Task-level train / validation / test split, fixed seed.
- `make drift-dataset` runnable unattended with resumable checkpoints and a plain log file.
- Features (all six) computed and stored per pair.

**Acceptance criteria.**
- [ ] Re-running with the same seed and model reproduces the same pairs (determinism test on a
      small sample).
- [ ] No task appears in more than one split (test).
- [ ] Judge agreement on the 50-pair hand check recorded in the datasheet.
- [ ] Dataset size within what the repository should hold; if larger than about 20 MB, store it
      as a release artefact or on a dataset host, with a checksum in the repo.

---

### T-094 — Gold-set labelling tool and agreement report

**Size** S · **Lane** drift · **GPU** — · —

**Why.** The ~200-pair human gold set (H-01) is what makes every drift number defensible. Two
people labelling independently gives inter-annotator agreement, which `PLAN.md` §6.6 calls the
single most credibility-buying number.

**Read.** T-034 output format; `docs/PLAN.md` §6.6.

**Depends on.** T-034.

**Deliverables.**
- `research/drift/label.py`: a terminal (or tiny local web) labeller that shows one pair at a
  time with the judge's label hidden, records the labeller's id and label, and resumes.
- Stratified sample of 200 pairs from the **test** split, drift and benign balanced across task
  types.
- `research/drift/agreement.py`: Cohen's κ between the two humans, and between each human and
  the LLM judge; disagreements listed for adjudication.

**Acceptance criteria.**
- [ ] The labeller never displays the judge's label or the generator's intended label.
- [ ] κ computation tested against a known example.

---

### T-035 — Calibrated drift classifier and model card

**Size** L · **Lane** drift / research · **GPU** — · —

**Why.** `PLAN.md` T-035's acceptance criteria, unchanged: calibrated logistic regression,
AUROC, AUPRC, Brier score, reliability diagram, FPR at the operating threshold < 5% on the
benign holdout (NFR-9), a model card, and the 20 worst errors analysed.

**Read.** `docs/PLAN.md` §6.6, T-035; T-034 datasheet; T-094 agreement report.

**Depends on.** T-034, H-01 (the gold set labelled).

**Deliverables.**
- `research/drift/train.py`: logistic regression on the six features with
  `CalibratedClassifierCV`; hyperparameters chosen on validation only.
- Evaluation on two test sets, both reported: the judge-labelled test split, and the human gold
  set. The gold-set numbers are the headline.
- `docs/drift-model-card.md`: intended use, data, metrics with confidence intervals, the
  reliability diagram, FPR at the threshold, the 20 worst errors with analysis, known
  failure modes (including slow drift).
- The trained model serialized in a safe, inspectable format (coefficients as JSON, not a
  pickle), so the PEP loads it without executing code.
- scikit-learn as a dependency of the research tooling only, never of `agentiam-core` (ADR).
- `generate_evidence_pack.py` picks up the model card (it currently states its absence).

**Acceptance criteria.**
- [ ] NFR-9 reported against the human gold set, met or not. If not met, the number is still
      published and the threshold trade-off shown.
- [ ] Results regenerated by `make drift-eval` from committed artefacts.
- [ ] `research/threat-model.md` N2 updated to what the numbers support.

---

### T-095 — Trajectory-level drift scoring (slow drift, A-27)

**Size** M · **Lane** drift / research · **GPU** — · —

**Why.** A-27 and TM-11: an adversary drifting in many small steps stays under any per-action
threshold. `PLAN.md` §12.4 calls this limitation "itself a good paper contribution". A
trajectory score turns an accepted risk into a measured result.

**Read.** `docs/PLAN.md` A-27; `docs/threat-model.md` TM-11; T-093's history store; T-034's slow
drift trajectories.

**Depends on.** T-035.

**Deliverables.**
- A per-task trajectory score over the sequence of per-action drift probabilities, such as CUSUM
  or an exponentially weighted moving average, with the choice justified by validation results.
- Evaluation on T-034's slow-drift trajectories: detection rate and steps-to-detection, against
  the per-action classifier alone.
- Wired to escalate (never deny) when the trajectory crosses its threshold.
- T-087's slow-drift scenario updated with the new outcome.

**Acceptance criteria.**
- [ ] Reported: per-action detection vs trajectory detection on slow drift, and the trajectory
      score's false-positive rate on benign trajectories.
- [ ] A-27's status in the red-team table updated from "accepted risk" to whatever the numbers
      support.

---

### T-096 — Classifier in the PEP oracle

**Size** M · **Lane** drift · **GPU** — · —

**Why.** Replace the v0 heuristic with the calibrated model wherever its features are available,
keeping the rule-based oracle as the fallback.

**Read.** `agentiam_pep/drift.py`; spec 06 §2–§3; T-035 model format; T-093's ADR on where
stateful features are computed.

**Depends on.** T-035.

**Deliverables.**
- `CalibratedDriftOracle` loading the JSON model; bands from `PLAN.md` §6.6 (< 0.3 allow,
  0.3–0.7 allow and flag, > 0.7 escalate) or the bands the model card justifies.
- Falls back to `RuleBasedDriftOracle` when a required feature is unavailable, recorded in the
  decision.
- The model version recorded in every decision record.
- Model distribution: shipped inside the policy bundle (signed, versioned) or as its own signed
  artefact, decided in an ADR.

**Acceptance criteria.**
- [ ] NFR-1 benchmark re-run with the classifier; p99 still < 1 ms, or the cost stated.
- [ ] A tampered model file is refused (signature check).
- [ ] The T-087 task-redirection scenario escalates via the classifier.
