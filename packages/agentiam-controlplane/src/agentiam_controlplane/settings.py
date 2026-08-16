"""Control-plane configuration.

The root signing key, the escalation approver allowlist, the session-cookie secret, and
(T-043) Keycloak OIDC client settings.

The root key is a stated stopgap, not a decision to build on: no issuance service exists yet
to custody it (threat-model A3's "Vault in dev" is the named future); until it does, this reads
a hex-encoded Ed25519 private key from the environment, mirroring `agentiam_pep.config`'s
`AGENTIAM_PEP_*` pattern rather than inventing a second one.

`approvers` is unchanged by T-043 in shape (still a fixed allowlist of `kc:<sub>` strings) but
changed in *source*: ADR-041 point 2's "caller names which approver is acting in the request
body" is gone. `agentiam_controlplane.auth` now derives that identity from a real OIDC session,
and `escalations_api` checks the session's principal against this same allowlist — see
ADR-046.
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
    #: Signs the session cookie (T-043) — `SessionMiddleware` carries OAuth `state`/`nonce`
    #: before login and `principal_id` after it. Required whenever the escalation router is
    #: mounted, independent of whether `OIDCSettings` (the login routes themselves) is also
    #: configured, so a test can fabricate a signed session without a running Keycloak.
    session_secret_key: str
    #: The elevated token cannot be delegated further: it is minted for the escalating
    #: agent's direct use on this one task, not for building a new chain from (`PLAN.md`
    #: never fixes a number here, so this is the ticket's own least-privilege choice).
    elevation_max_depth: int = 1

    @classmethod
    def from_env(cls) -> ControlPlaneSettings:
        """Build from `AGENTIAM_CONTROLPLANE_*` variables.

        Raises:
            ValueError: `AGENTIAM_CONTROLPLANE_ROOT_PRIVATE_KEY` is unset or is not 32 bytes
                of hex, `AGENTIAM_CONTROLPLANE_APPROVERS` is unset or names nobody, or
                `AGENTIAM_CONTROLPLANE_SESSION_SECRET_KEY` is unset.
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

        session_secret_key = os.environ.get(f"{ENV_PREFIX}SESSION_SECRET_KEY")
        if not session_secret_key:
            raise ValueError(f"{ENV_PREFIX}SESSION_SECRET_KEY is required")

        return cls(
            root_private_key=root_private_key,
            approvers=approvers,
            session_secret_key=session_secret_key,
        )


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    """What the console's login/callback/logout routes need to talk to Keycloak — T-043.

    Kept separate from `ControlPlaneSettings` because the two are wired independently in
    `create_app`: a deployment (or a test) can require a session on the escalation routes
    without necessarily mounting the routes that mint one, e.g. a test that fabricates a
    signed session cookie directly rather than running a real Keycloak.
    """

    #: The realm's issuer URL, e.g. `http://localhost:8080/realms/agentiam`. Authlib fetches
    #: `{issuer}/.well-known/openid-configuration` for the rest (authorize/token/jwks/
    #: end-session endpoints) rather than this package hand-tracking Keycloak's URL layout.
    issuer: str
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> OIDCSettings:
        """Build from `AGENTIAM_CONTROLPLANE_OIDC_*` variables.

        Raises:
            ValueError: any of `AGENTIAM_CONTROLPLANE_OIDC_ISSUER`,
                `_OIDC_CLIENT_ID`, `_OIDC_CLIENT_SECRET` is unset.
        """
        issuer = os.environ.get(f"{ENV_PREFIX}OIDC_ISSUER")
        if not issuer:
            raise ValueError(f"{ENV_PREFIX}OIDC_ISSUER is required")
        client_id = os.environ.get(f"{ENV_PREFIX}OIDC_CLIENT_ID")
        if not client_id:
            raise ValueError(f"{ENV_PREFIX}OIDC_CLIENT_ID is required")
        client_secret = os.environ.get(f"{ENV_PREFIX}OIDC_CLIENT_SECRET")
        if not client_secret:
            raise ValueError(f"{ENV_PREFIX}OIDC_CLIENT_SECRET is required")
        return cls(issuer=issuer, client_id=client_id, client_secret=client_secret)


__all__ = ["ENV_PREFIX", "ControlPlaneSettings", "OIDCSettings"]
