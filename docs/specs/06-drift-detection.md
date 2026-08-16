# 06 — Drift Detection and Intent Canonicalization

## 1. Intent canonicalization & hashing

A core invariant of the token format (spec 01 §5) is that authority is bound to an English-language intent, representing the operator's approved task (e.g. "Transfer 500 units to vendor A").

This intent is cryptographically bound into the `intent_hash` of the mandate using canonical JSON serialization and SHA-256 (`agentiam_core.hashing.canonical_json`).

### 1.1 Stateless Verification
Because the PEP is completely stateless (has no network lookup for the mandate database), it cannot verify *which* English task the token corresponds to just by looking at the hash. 

To overcome this, **the client must assert its intent in plain English**.
- The client provides `AgentIAM-Task-Intent` (the mandate's intent) and `AgentIAM-Action-Intent` (what the agent is doing right now).
- The PEP hashes the `AgentIAM-Task-Intent` string using the exact same `hash_object` algorithm.
- The PEP compares the resulting hash against `token.intent_hash`.
- If they differ, the request is denied with `INTENT_MISMATCH`. 

This guarantees the PEP has the authentic, cryptographically-proven English text of the mandate, which is required for Drift scoring.

## 2. Rule-Based Drift (v0)

AgentIAM defines "drift" as the semantic divergence between the approved `AgentIAM-Task-Intent` and the attempted `AgentIAM-Action-Intent`.

Instead of a heavy calibrated ML model (deferred to research), v0 relies on a deterministic, rule-based approach using semantic embeddings:

1. **Embedding Generation**: Both intents are sent to a local Ollama instance (using `nomic-embed-text` or similar) to generate dense vectors.
2. **Cosine Similarity**: The cosine similarity between the two vectors is computed.
3. **Thresholding**: 
   - A configurable similarity threshold (e.g., 0.3) defines orthogonal/drifted actions. 
   - If `cosine_similarity < 0.3`, it yields a drift score of `> 0.7`.
4. **Drift Escalation**: A drift score `> 0.7` escalates the decision (reason code `DRIFT_ESCALATION`), requiring a human to unblock it.

### 2.1 Drift never denies
Drift is a heuristic. Therefore, a high drift score **never** results in a hard deny (spec 09 §3.3). It results in `Outcome.ESCALATE`. If the Oracle is unavailable (e.g., Ollama is down), the PEP fails open regarding drift (`score = None`), rather than blocking all transactions.

## 3. Drift Modes (T-036)

Drift assessment is configurable per scope with three modes:
- `off`: No drift calculation is performed.
- `log_only`: Drift is calculated and attached to the audit record, but a high score does not escalate.
- `strict`: Drift is calculated. A high score (e.g. `> 0.7`) changes the decision to `ESCALATE`.

## 4. Performance and Caching

Embedding generation is slow (milliseconds to seconds depending on hardware). 
To prevent this from breaking the NFR-1 latency budget, the Drift Oracle implements caching.

The drift score is cached keyed on the pair of intent strings, `(task_intent_text,
action_intent_text)`. `PLAN.md` §6.6 sketched `(task_id, scope, arg_digest)`; the
implemented key is the one that matches what the computation actually depends on. Two
requests with the same digest but different asserted action text are different questions,
and two requests with different digests but identical text are the same question.

### 4.1 Cold start dominates, and it is not a startup-only cost — measured

Against `nomic-embed-text` on the development machine (768 dimensions):

| | latency |
|---|---|
| cold call (model not resident in Ollama) | **14,244 ms** |
| warm, n=20, median | 17.8 ms |
| warm, n=20, p95 | 83.4 ms |

Three consequences, none of which are visible on paper:

1. A cold call exceeds the oracle's own 2 s timeout by 7x, so the **first** scored request
   after the model unloads always fails open. Drift is not merely slow at startup; it is
   *absent* until something warms the model.
2. Ollama evicts an idle model (default ~5 minutes), so this recurs during normal operation,
   not only at boot. The warm-up must set `keep_alive` rather than fire once.
3. `lru_cache` does not memoize exceptions, so every request during the cold window re-pays
   the full timeout.

Hence T-033's "embedding model cached at startup" is a correctness requirement, not a
performance one.

### 4.2 The connection is cached too, and that was the larger cost — measured

T-032 opened a fresh `httpx.Client` inside every cache miss. Constructing one is not free:

| | median | p95 |
|---|---|---|
| `httpx.Client` construction alone | 724.7 ms | 1,603.5 ms |
| cache miss, client per call (T-032) | 747.9 ms | — |
| cache miss, shared client (T-033) | **83.3 ms** | — |

A ~9x difference, and the 748 ms was spent **blocking the asyncio event loop** inside
`decide()` — a path whose own timeout is 2 s, so 37% of the budget went to setup before a
single byte was sent. At p95 the construction alone approaches the timeout.

`EmbeddingClient` therefore holds one client for the process. That is what makes the 83 ms
figure, and the 17.8 ms warm single-embedding figure above, reachable at all.

---

## 5. Features (T-033)

`PLAN.md` §6.6 defines six features feeding a calibrated classifier. T-034 (the labelled
dataset) and T-035 (the classifier) are deferred, so nothing consumes a feature vector this
cycle; T-033 delivers the **computation**, against the day one does.

Three of the six are in scope. f3, f4 and f6 are deferred — see ADR-036.

The vector is persisted on `DecisionRecord.drift_features`, alongside `drift_score`. Both
were wired together, because until then the pipeline dropped the score `decide()` already
produced — so there was no honest place to put a vector either.

**Absent features are omitted from the dict, never stored as null.** A deferred dataset
(T-034) has to be able to tell *not measured* from *measured as zero*, and f5 = 0.0 is a
real observation: every argument was foreign to the task.

| # | Feature | Type | Inputs |
|---|---|---|---|
| f1 | `cosine(embed(task_intent), embed(action_template))` | embedding | task text, scope + tool |
| f2 | `cosine(embed(task_intent), embed(rendered_action))` | embedding | task text, scope + tool + args |
| f5 | argument-entity overlap with the task text | symbolic | task text, extracted args |

`action_template` is the action *without* argument values (`"payment:initiate using
payment_api"`); `rendered_action` is the same with values substituted. The distinction is
what separates f1 from f2.

### 5.1 Why f1 and f2 are two features and not one — measured

Against the task *"Pay invoice INV-2291 from vendor Rahman Textiles for 45000 BDT"*:

| case | f1 | f2 |
|---|---|---|
| aligned | 0.4834 | 0.8139 |
| same scope, wrong vendor | 0.4834 | 0.6983 |
| same scope, inflated amount (45,000 → 9,500,000) | 0.4834 | 0.8037 |
| related read | 0.5412 | 0.6156 |
| drifted (external email) | 0.3607 | 0.4902 |
| hard drift (delete production database) | 0.3550 | 0.4036 |

f1 is constant across the three payment rows because the template is identical — it cannot
see arguments by construction. f2 moves with them. They measure different things.

Two limitations, both measured and both stated rather than hidden:

* **f1 is not monotonic in alignment.** The *related read* scores higher on f1 (0.5412) than
  the correctly-aligned payment (0.4834). f1 is a weak signal alone.
* **Embeddings are near-blind to numeric magnitude.** A 211x payment inflation moved f2 by
  0.0102 — inside the noise between aligned cases. **No embedding feature can catch an
  amount attack.** This is precisely why f5 is symbolic.

### 5.2 f5 — argument entity overlap

f5 is deterministic and requires no model. For each extracted argument value, decide whether
that entity appears in the task text, and return the fraction that do:

```
f5 = |{a in args : appears_in(a, task_text)}| / |args|      (1.0 when args is empty)
```

`appears_in` is case-folded and NFKC-normalized. Numbers compare by **numeric value**, not by
string: `45000`, `45,000` and `45000.0000` are the same entity, so the ledger's 10^4 scaling
(spec 10 §4.3) does not produce a spurious mismatch. This is the guard that sees the amount
attack f2 cannot.

f5 is pure and lives in `agentiam-core`; f1 and f2 require I/O and live in `agentiam-pep`,
per the core purity rule (`PLAN.md` §5).

### 5.3 Features never decide

Feature extraction is observational this cycle. §2.1 still governs: the v0 rule-based score is
what escalates, features are recorded alongside it, and an extraction failure degrades to
absent features rather than to a denial.

This is defended twice — `FeatureExtractor.extract` swallows its own failures, and the
pipeline swallows anything that escapes it. The second guard exists because the first is a
convention a future extractor could forget, and no feature is worth failing a request that
policy and budget both allowed. Both are tested by making an extractor raise.

### 5.4 What f5 sees that the rest of the pipeline does not

f5 compares argument values against the authenticated task text, so it notices an argument
the task never mentioned — a payment to an unnamed vendor, or an invoice id that is not the
one the operator approved. That is the same shape as the amount attack in §5.1, and it is
now visible in the audit record rather than only inside the extractor.

It is a *record*, not a gate. Nothing denies on f5 this cycle.
