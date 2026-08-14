"""Reference demo: a procurement agent under AgentIAM governance.

Contains the root procurement agent, its sub-agents, stub tool servers (invoice,
vendor, payment, email), and the scripted demo scenarios. Every scenario is
deterministic, individually runnable, and resettable via ``make demo-reset``.

The demo substrate is not throwaway code — it is the surface the whole system is
judged through. See ``DEMO.md``.

Implemented from T-057.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
