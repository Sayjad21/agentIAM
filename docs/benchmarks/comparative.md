<!--
  GENERATED FILE — do not edit.

  Regenerate with:  make bench-compare     (measures, needs Docker and an LLM backend)
                    uv run python scripts/generate_comparative_results.py   (render only)
  Source:           docs/benchmarks/comparative.json
  Verify in CI:     uv run python scripts/generate_comparative_results.py --check
-->

# Comparative benchmarks

AgentIAM against the designs it replaces, on one host, in one run. Medians and percentiles
only (`PLAN.md` §13.1). Every ratio below is computed from the measured figures.

Measured 2026-10-18T10:42:06.740510+00:00 on `Linux-7.2.11-200.fc44.x86_64-x86_64-with-glibc2.43`, Python 3.12.15, 16 CPUs.

## 1. Delegating authority: offline attenuation vs a token exchange

A parent hands a child a narrower credential. AgentIAM appends a block to the parent's biscuit in process (`attenuate()`); the alternative asks an authorization server for a new token.

| Path | Calls | median ms | p99 ms |
|---|---|---|---|
| `attenuate()`, in process | 500 | 0.089 | 0.254 |
| simulated OAuth2 token exchange | 500 | 2.424 | 4.274 |

The exchange's median is **27.2×** the in-process median, and its p99 16.8×.

**This is a simulated OAuth2 token exchange, and its setup favours it:** an HTTP endpoint on loopback, one keep-alive connection reused for every call, one primary-key `SELECT` per call against the same Postgres container, no TLS, and an issued token that is a random string rather than a signed JWT. A real authorization server adds TLS, a signature, a network hop and its own policy evaluation. The in-process side is the real `attenuate()` producing a real child biscuit, which is verified once to confirm it is narrower and valid.


## 2. Enforcing a shared budget: a ledger round trip per call vs leases

3 PEPs spend from one pool at once, 300 calls of ৳10.0000 each, closed loop. **B1** checks and debits the ledger inside one row-locking transaction per call (`FOR UPDATE`) — the central-check shape. **B3** is AgentIAM: each PEP reserves from a lease in memory, settles through the deployed settlement queue, and the ledger sees only lease acquisitions, releases and batched commits. Latency is per call, including any wait for a lease top-up.

| Design | Calls allowed | median ms | p99 ms | calls/s | ledger round trips | refused |
|---|---|---|---|---|---|---|
| B1 — FOR UPDATE per call | 900 | 17.124 | 27.352 | 169.4 | 900 | 0 |
| B3 — lease reserve | 900 | 0.007 | 0.063 | 7368.2 | 36 | 0 |

B3's median is 2481.7× faster than B1's, and it made 36 ledger round trips where B1 made 900. B3's p99 is where its cost went: the calls that waited for a lease top-up (176 waits across the run).

**B3's ledger UNDER-RECORDED ৳4080.0000 of ৳9000.0000 spent** (committed ৳4920.0000). Settlements reached the ledger after their lease had been released and were rejected, so that money went back to the pool as available — spendable twice. `STATUS.md` gap 38; the latency figures above stand, the accounting does not.


## 3. The PEP's cost beside the call it guards

The PEP's in-process cost per request is **0.405 ms** — `verify()` + `decide()` + extraction, medians from `pb2-breakdown.json` (see `performance.md`).

A median LLM call from the configured backend (`gemini`, resolved to `gemini-3.5-flash-lite`) took **1252 ms** over 5 calls (slowest 1302 ms), each a short structured-output request of the shape the demo agent makes, timed end to end including any retry the client made.

So the PEP adds **0.03%** to an agent step that calls the model once and then one tool — the demo agent's shape. A step making several tool calls per model call pays the PEP's cost once per tool call.
