"""Control-plane configuration: the root signing key and the escalation approver list — T-037.

Both are stated stopgaps, not decisions to build on. No issuance service exists yet to custody
the root key (threat-model A3's "Vault in dev" is the named future); until it does, this reads
a hex-encoded Ed25519 private key from the environment, mirroring `agentiam_pep.config`'s
`AGENTIAM_PEP_*` pattern rather than inventing a second one. Likewise no OIDC login exists yet
(T-043); until it does, "who may approve" is a fixed config list rather than a session
identity, and the caller names which approver is acting in the request body.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from biscuit_auth import Algorithm, PrivateKey

#: Prefix for every environment variable this reads.
ENV_PREFIX: Final = "AGENTIAM_CONTROLPLANE_"


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    """What the escalation approval endpoint needs to mint an elevated token."""

    root_private_key: PrivateKey
    approvers: frozenset[str]
    #: The elevated token cannot be delegated further: it is minted for the escalating
    #: agent's direct use on this one task, not for building a new chain from (`PLAN.md`
    #: never fixes a number here, so this is the ticket's own least-privilege choice).
    elevation_max_depth: int = 1

    @classmethod
    def from_env(cls) -> ControlPlaneSettings:
        """Build from `AGENTIAM_CONTROLPLANE_*` variables.

        Raises:
            ValueError: `AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY` is unset or is not 32 bytes
                of hex, or `AGENTIAM_CONTROLPLANE_APPROVERS` is unset or names nobody.
        """
        key_hex = os.environ.get(f"{ENV_PREFIX}ROOT_PRIVATE_KEY")
        if not key_hex:
            raise ValueError(f"{ENV_PREFIX}ROOT_PRIVATE_KEY is required")
        try:
            key_bytes = bytes.fromhex(key_hex)
        except ValueError as exc:
            raise ValueError(f"{ENV_PREFIX}ROOT_PRIVATE_KEY must be hex") from exc
        if len(key_bytes) != 32:
            raise ValueError(
                f"{ENV_PREFIX}ROOT_PRIVATE_KEY must be 32 bytes (64 hex characters), "
                f"got {len(key_bytes)}"
            )
        # `Algorithm.Ed25519` and `from_bytes`'s second argument are both absent from the
        # type stubs but required at runtime (measured against `biscuit-python` 0.4.0 —
        # `tokens.py`'s `_authorizer()` documents the same stub/runtime gap for `limits()`).
        root_private_key = PrivateKey.from_bytes(  # type: ignore[call-arg]
            key_bytes,
            Algorithm.Ed25519,  # type: ignore[attr-defined]
        )

        approvers_raw = os.environ.get(f"{ENV_PREFIX}APPROVERS", "")
        approvers = frozenset(a.strip() for a in approvers_raw.split(",") if a.strip())
        if not approvers:
            raise ValueError(f"{ENV_PREFIX}APPROVERS is required and must name at least one id")

        return cls(root_private_key=root_private_key, approvers=approvers)


__all__ = ["ENV_PREFIX", "ControlPlaneSettings"]
