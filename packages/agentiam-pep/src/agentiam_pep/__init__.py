"""AgentIAM Policy Enforcement Point.

The gateway that sits at the tool boundary. Runs the 10-step decision pipeline
(``PLAN.md`` §3.1) on every call: extract, verify, check revocation, evaluate token
Datalog, evaluate Cedar policy, check drift, reserve budget, forward, commit, emit.

This is the hot path. It must never block on the network to reach a decision: leases,
revocation lists, and policy bundles are all held locally and refreshed asynchronously.

Implemented from T-018.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
