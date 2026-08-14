"""Client SDK for AgentIAM.

What an agent developer imports. Holder-side attenuation, identity propagation across
asyncio tasks and worker threads, and declarative scope checks.

```python
client = AgentIAM(token=root_token, key_set=key_set)

reader = client.attenuate(role="doc-reader", scopes=["invoice:read"], budget={"spend_bdt": 0})

with reader.activate():
    await read_invoices()          # @requires_scope("invoice:read") sees the child token
```

`attenuate()` is entirely local: no issuer round-trip, no shared state, no network
(INV-3). The child is cryptographically incapable of exceeding the parent, and that
holds whether or not this SDK is the thing that checked.

Nothing here is a security boundary. The PEP enforces; this package makes the enforced
thing convenient to hold and hard to leak between concurrent sub-agents.
"""

from agentiam_sdk.client import AgentIAM
from agentiam_sdk.context import (
    bind_identity,
    current_identity,
    current_identity_or_none,
    run_in_executor,
    use_identity,
)
from agentiam_sdk.decorators import requires_scope
from agentiam_sdk.errors import (
    IdentityContextError,
    NoIdentityError,
    ScopeDeniedError,
    SDKError,
    TokenSizeWarning,
)
from agentiam_sdk.identity import AgentIdentity

__version__ = "0.1.0"

__all__ = [
    "AgentIAM",
    "AgentIdentity",
    "IdentityContextError",
    "NoIdentityError",
    "SDKError",
    "ScopeDeniedError",
    "TokenSizeWarning",
    "__version__",
    "bind_identity",
    "current_identity",
    "current_identity_or_none",
    "requires_scope",
    "run_in_executor",
    "use_identity",
]
