"""AgentIAM control plane.

The services behind the enforcement points: issuance (mandates and root tokens), the
ledger (budgets and leases — the hard part), policy (Cedar bundles and versioning), the
NL to Cedar compiler, drift detection, revocation, the hash-chained audit ledger, the
escalation workflow, and the admin console.

The ledger is the only authority on budget. Enforcement points hold leases, not truth.

Implemented from T-012.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
