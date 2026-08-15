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

The drift score is cached keyed on: `(task_id, scope, arg_digest)`. 
Identical actions under the same task bypass the embedding model entirely.
