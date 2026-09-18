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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from biscuit_auth import Algorithm, PrivateKey, PublicKey

#: Prefix for every environment variable this reads.
ENV_PREFIX: Final = "AGENTIAM_CONTROLPLANE_"


def _parse_operator_tokens(raw: str, *, approvers: frozenset[str]) -> dict[str, str]:
    """Parse `kc:<sub>=<secret>,...` into `{secret: principal}`.

    Keyed on the secret because that is what the lookup has: the caller presents a bearer
    credential and the route needs the identity it stands for. Deriving the identity from
    the credential — rather than reading it off the request — is the whole point.

    Raises:
        ValueError: an entry is malformed, names an id outside `approvers` (a token that
            could never authorize anything, so almost certainly a typo), or reuses a secret
            already bound to a different id (which would make the acting identity depend on
            dict ordering).
    """
    tokens: dict[str, str] = {}
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        principal, separator, secret = entry.partition("=")
        principal, secret = principal.strip(), secret.strip()
        if not separator or not principal or not secret:
            raise ValueError(
                f"{ENV_PREFIX}OPERATOR_TOKENS entries must be '<principal>=<secret>', got {entry!r}"
            )
        if principal not in approvers:
            raise ValueError(
                f"{ENV_PREFIX}OPERATOR_TOKENS names {principal!r}, which is not in "
                f"{ENV_PREFIX}APPROVERS"
            )
        if secret in tokens and tokens[secret] != principal:
            raise ValueError(
                f"{ENV_PREFIX}OPERATOR_TOKENS reuses one secret for {tokens[secret]!r} "
                f"and {principal!r}"
            )
        tokens[secret] = principal
    return tokens


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
    #: Shared secret -> the `kc:<sub>` it authenticates, for callers that revoke without a
    #: browser session (incident response, scripts, the demo stack — which deliberately
    #: leaves OIDC unwired). Presented as `Authorization: Bearer <secret>`.
    #:
    #: This exists because ADR-046 removed "the caller names itself in the request body"
    #: from approve/deny and the revoke route kept it, so `revoked_by` was an unverified
    #: claim: any unauthenticated caller reaching the control plane could revoke the root
    #: block and take every agent down. A secret the caller must possess is evidence; a
    #: field naming yourself is not. Empty means only a session can revoke.
    operator_tokens: Mapping[str, str] = field(default_factory=dict)
    #: The root *public* key, needed to verify a token before attenuating it when the
    #: console spawns a sub-agent. biscuit's `PrivateKey` cannot derive its own public half
    #: (measured against `biscuit-python` 0.4.0 — it exposes only `to_bytes`/`from_*`), so
    #: this is read separately rather than computed. `None` disables task creation and the
    #: console says so, rather than failing at the first spawn.
    root_public_key: PublicKey | None = None
    #: Where the console sends a request when the operator drives a task's agent. Only the
    #: demo driver uses it; nothing on the authorization path depends on it.
    pep_url: str = "http://pep:8080"

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

        # Optional, and deliberately not fatal: without it the console cannot start a task,
        # but every inspection screen still works. A missing key here should narrow what the
        # console offers, not stop it booting.
        public_hex = os.environ.get(f"{ENV_PREFIX}ROOT_PUBLIC_KEY", "").strip()
        root_public_key: PublicKey | None = None
        if public_hex:
            try:
                public_bytes = bytes.fromhex(public_hex)
            except ValueError as exc:
                raise ValueError(f"{ENV_PREFIX}ROOT_PUBLIC_KEY must be hex") from exc
            if len(public_bytes) != 32:
                raise ValueError(
                    f"{ENV_PREFIX}ROOT_PUBLIC_KEY must be 32 bytes (64 hex characters), "
                    f"got {len(public_bytes)}"
                )
            root_public_key = PublicKey.from_bytes(  # type: ignore[call-arg]
                public_bytes,
                Algorithm.Ed25519,  # type: ignore[attr-defined]
            )

        return cls(
            root_private_key=root_private_key,
            approvers=approvers,
            session_secret_key=session_secret_key,
            operator_tokens=_parse_operator_tokens(
                os.environ.get(f"{ENV_PREFIX}OPERATOR_TOKENS", ""), approvers=approvers
            ),
            root_public_key=root_public_key,
            pep_url=os.environ.get(f"{ENV_PREFIX}PEP_URL", "http://pep:8080").rstrip("/"),
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
