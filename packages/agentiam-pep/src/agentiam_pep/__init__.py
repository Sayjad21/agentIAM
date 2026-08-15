"""AgentIAM Policy Enforcement Point.

The gateway that sits at the tool boundary. Runs the 10-step decision pipeline
(``PLAN.md`` §3.1) on every call: extract, verify, check revocation, evaluate token
Datalog, evaluate Cedar policy, check drift, reserve budget, forward, commit, emit.

This is the hot path. It must never block on the network to reach a decision: leases,
revocation lists, and policy bundles are all held locally and refreshed asynchronously.

**What is actually built so far.** T-014 added `lease.py` — the PEP-side half of the lease
protocol, pure and local. T-018 added `app.py`, `config.py` and `headers.py`: the ASGI
gateway and its reverse proxy, which forwards transparently and **decides nothing**.

Steps 1 through 10 of that pipeline arrive with T-019 (the decision itself, pure, in
`agentiam-core`), T-020 (scope and argument extraction) and T-021 (the lease pool). Until
they land, `/readyz` reports ``enforcing: false`` — the gap is stated at runtime rather
than left to be inferred from a version number.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
