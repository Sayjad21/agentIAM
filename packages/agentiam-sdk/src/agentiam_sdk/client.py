"""The client an agent developer holds: verify a token, attenuate it, act under it.

This is the whole surface for now, and deliberately so. `spend()`, `call_tool()` and
`escalate()` from `PLAN.md` §8 all need a transport and a lease pool; they arrive with
T-018 and T-021. Stubbing them here would produce methods that look usable and enforce
nothing, which is worse than their absence.

What *is* here is the part that needs no network at all. Minting a narrower child is
pure local cryptography — no issuer round-trip, no shared state (INV-3) — and it is the
claim the demo is built on, so it is the part that ships first.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from agentiam_core.attenuation import attenuate as core_attenuate
from agentiam_core.errors import DepthExceededError, ReasonCode
from agentiam_core.models import (
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)
from agentiam_core.tokens import WARN_SIZE_LIMIT_B64, RootKeySet, verify
from agentiam_sdk.context import use_identity
from agentiam_sdk.errors import TokenSizeWarning
from agentiam_sdk.identity import AgentIdentity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from agentiam_sdk.context import _IdentityScope


def _utc_now() -> datetime:
    """The default clock.

    The SDK may read one; `agentiam-core` may not, which is why it is injected here and
    passed down as an argument there (ENGINEERING-RULES rule 3).
    """
    return datetime.now(UTC)


class AgentIAM:
    """An agent acting under one token.

    Immutable in the way that matters: `attenuate()` returns a *new* client wrapping a
    new token and never touches this one. A sub-agent receiving a child client cannot
    reach back through it to the parent's authority.
    """

    __slots__ = ("_clock", "_identity", "_key_set")

    def __init__(
        self,
        *,
        token: str,
        key_set: RootKeySet,
        role: str = "root",
        agent_id: str | None = None,
        known_caveats: Sequence[Caveat] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Verify `token` and hold it as this agent's identity.

        Verifying at construction rather than at first use means a bad token fails where
        it entered the process, with the reason code attached, instead of surfacing later
        as a confusing denial from the PEP.

        Args:
            token: Base64-encoded biscuit chain.
            key_set: Root public keys to accept. More than one during rotation (EC-T05).
            role: Human-readable role, for the console and the audit trail.
            agent_id: Identity of this agent. Defaults to one derived from the mandate.
            known_caveats: Caveats already on the chain, if the holder knows them. Supply
                these when a token is handed over along with the restrictions that were
                placed on it; omitting them makes the folded authority an upper bound
                rather than an exact one, never the reverse.
            clock: Source of the current instant. Injected so tests are reproducible.

        Raises:
            TokenTooLargeError, MalformedTokenError, InvalidSignatureError,
            TokenNotYetValidError, TokenExpiredError, DepthExceededError: As `verify()`.
        """
        self._key_set = key_set
        self._clock = clock or _utc_now
        verified = verify(token, key_set, now=self._clock())
        self._identity = AgentIdentity(
            token=token,
            verified=verified,
            agent_id=agent_id or f"agent:{verified.mandate_id}",
            role=role,
            known_caveats=tuple(known_caveats),
        )

    @property
    def identity(self) -> AgentIdentity:
        """The identity this client acts under."""
        return self._identity

    @property
    def token(self) -> str:
        """The chain as it goes on the wire."""
        return self._identity.token

    @property
    def key_set(self) -> RootKeySet:
        """The root keys this client verifies against, for handing a token onward."""
        return self._key_set

    def activate(self) -> _IdentityScope:
        """Make this client's identity current for the duration of a `with` block."""
        return use_identity(self._identity)

    def attenuate(
        self,
        *,
        role: str,
        agent_id: str | None = None,
        scopes: Iterable[str] | None = None,
        budget: Mapping[str | BudgetDimension, Decimal | int | str] | None = None,
        ttl_s: int | None = None,
        tools_allow: Iterable[str] | None = None,
        tools_deny: Iterable[str] | None = None,
        caveats: Sequence[Caveat] = (),
    ) -> AgentIAM:
        """Mint a strictly narrower child token, offline, and wrap it in a new client.

        The four common restrictions have keyword forms because they are what an agent
        reaches for; the other five caveat types go through `caveats`. Everything given
        is checked against the grant *and* against the caveats this client already knows
        about, which is the check core cannot make on its own — given only a
        `VerifiedToken`, a scope a parent gave up is still listed in the authority block.

        Args:
            role: The child's role.
            agent_id: The child's identity. Defaults to one derived from the mandate.
            scopes: Scopes the child keeps. Must be a subset of what this token holds.
            budget: Per-dimension ceilings, keyed by `BudgetDimension` or its value.
                Never floats — money is `Decimal`, `int` or `str` (rule 4).
            ttl_s: Seconds from now until the child expires. Read from the injected
                clock, so a child never outlives its parent by clock skew.
            tools_allow: The only tools the child may call.
            tools_deny: Tools the child may not call, in addition to any already denied.
            caveats: Any other caveats, including the four types with no keyword form.

        Returns:
            A new client holding the child token.

        Raises:
            ValueError: If nothing was restricted, or `ttl_s` is not positive.
            AttenuationError: If any restriction would widen authority. No token is
                produced.
            DepthExceededError: If the child would exceed the mandate's `max_depth`.

        Warns:
            TokenSizeWarning: If the child crosses the 4 KB advisory limit (EC-T11).
        """
        proposed = [
            *self._keyword_caveats(
                scopes=scopes,
                budget=budget,
                ttl_s=ttl_s,
                tools_allow=tools_allow,
                tools_deny=tools_deny,
            ),
            *caveats,
        ]
        if not proposed:
            raise ValueError(
                "attenuate() was given no caveats; a block that narrows nothing adds "
                "~410 bytes to every request and grants no additional safety"
            )

        parent = self._identity
        if parent.depth + 1 > parent.verified.max_depth:
            # Checked before minting. `verify()` would catch it a moment later, but only
            # after producing a token that must then be thrown away.
            raise DepthExceededError(
                f"delegating from depth {parent.depth} would exceed the mandate's "
                f"max_depth of {parent.verified.max_depth}",
                reason_code=ReasonCode.DEPTH_EXCEEDED,
            )

        child_agent_id = agent_id or f"agent:{parent.verified.mandate_id}:{parent.depth + 1}"
        child_token = core_attenuate(
            parent.verified,
            proposed,
            agent_id=child_agent_id,
            role=role,
            ancestor_caveats=parent.known_caveats,
        )
        if len(child_token) > WARN_SIZE_LIMIT_B64:
            warnings.warn(
                f"child token is {len(child_token)} base64 characters, past the "
                f"{WARN_SIZE_LIMIT_B64} advisory limit; some HTTP servers cap the "
                f"request line well below that",
                TokenSizeWarning,
                stacklevel=2,
            )

        return AgentIAM(
            token=child_token,
            key_set=self._key_set,
            role=role,
            agent_id=child_agent_id,
            known_caveats=(*parent.known_caveats, *proposed),
            clock=self._clock,
        )

    def _keyword_caveats(
        self,
        *,
        scopes: Iterable[str] | None,
        budget: Mapping[str | BudgetDimension, Decimal | int | str] | None,
        ttl_s: int | None,
        tools_allow: Iterable[str] | None,
        tools_deny: Iterable[str] | None,
    ) -> list[Caveat]:
        """Translate the keyword shorthands into caveats, in a stable order."""
        built: list[Caveat] = []
        if scopes is not None:
            built.append(ScopeSubset(scopes=frozenset(scopes)))
        if budget is not None:
            # `model_validate` rather than the constructor so the ceiling's own
            # `_reject_float` validator sees the raw value. Converting to `Decimal` here
            # first would silently accept `12000.5` — `Decimal(float)` is perfectly happy
            # to absorb a binary fraction, which is exactly what rule 4 forbids.
            built += [
                BudgetCeiling.model_validate(
                    {"dimension": BudgetDimension(dimension), "value": value}
                )
                for dimension, value in budget.items()
            ]
        if ttl_s is not None:
            if ttl_s <= 0:
                raise ValueError(f"ttl_s must be positive, got {ttl_s}")
            built.append(TimeWindow(not_after=self._clock() + timedelta(seconds=ttl_s)))
        if tools_allow is not None:
            built.append(ToolAllow(tools=frozenset(tools_allow)))
        if tools_deny is not None:
            built.append(ToolDeny(tools=frozenset(tools_deny)))
        return built


__all__ = ["AgentIAM"]
