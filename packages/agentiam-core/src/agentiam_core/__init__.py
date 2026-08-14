"""Pure domain logic for AgentIAM.

This package holds the correctness core: the token model, the caveat language, the
attenuation algebra, budget arithmetic, intent hashing, and the decision pipeline.

It performs **no I/O of any kind** — no network, no database, no filesystem, no clock
reads. The clock is injected. This is not a style preference: every correctness claim
made about AgentIAM is a claim about this package, and those claims are only meaningful
if the package is deterministic and fully testable in isolation.

See ``PLAN.md`` §5 and ``ENGINEERING-RULES.md`` rule 3. The constraint is enforced
statically by ``tests/unit/test_core_purity.py``.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
