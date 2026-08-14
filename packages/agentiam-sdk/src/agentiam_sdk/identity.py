"""The identity an agent is currently acting under.

`VerifiedToken` (core) answers *what did the authority block grant?*. That is not the
same question as *what can this token still do?*, because the answer to the second one
depends on every caveat added since — and those live in attenuation blocks that
`verify()` does not read back (`STATUS.md` §3, gap 2).

The SDK is the one component that knows them, because it is the component that minted
them. `AgentIdentity` is where that knowledge is kept: the token as it goes on the wire,
the grant read back out of it, and the caveat chain this process added. From those three
it folds the effective authority the console and `@requires_scope` need.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast

from agentiam_core.attenuation import EffectiveAuthority, effective_bound, effective_budget
from agentiam_core.models import BudgetDimension

if TYPE_CHECKING:
    from datetime import datetime

    from agentiam_core.models import Budget, Caveat
    from agentiam_core.tokens import VerifiedToken


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """A token plus everything the holder knows about how narrow it has become.

    Attributes:
        token: The chain, base64-encoded. What travels in the `Authorization` header.
        verified: The grant read back out of the authority block.
        agent_id: Who this token belongs to.
        role: Human-readable role, for the console and the audit trail.
        known_caveats: Caveats added by this process, in chain order. Empty for a root
            token, and empty for a token received from elsewhere — in that case the
            folded authority is an *upper bound*, never an understatement, because
            biscuit's append-only structure means an unknown caveat can only narrow.
        authority: The grant folded against `known_caveats`.
        budget: The spend ceiling, folded the same way.
    """

    token: str
    verified: VerifiedToken
    agent_id: str
    role: str
    known_caveats: tuple[Caveat, ...] = ()
    authority: EffectiveAuthority = field(init=False)
    budget: Budget = field(init=False)

    def __post_init__(self) -> None:
        """Fold the caveat chain against the grant, once, at construction.

        Eagerly rather than on demand: an identity is built once per attenuation, but
        `@requires_scope` reads `authority` on every decorated call, and NFR-1 budgets
        the whole in-process decision at p99 < 1 ms.
        """
        bound = effective_bound(self.known_caveats)
        grant = self.verified
        budget = effective_budget(self.known_caveats, grant.budget)

        folded = replace(
            bound,
            scopes=grant.scopes if bound.scopes is None else grant.scopes & bound.scopes,
            max_depth=(
                grant.max_depth
                if bound.max_depth is None
                else min(grant.max_depth, bound.max_depth)
            ),
            intent_hash=grant.intent_hash if bound.intent_hash is None else bound.intent_hash,
            not_before=(
                grant.not_before
                if bound.not_before is None
                else max(grant.not_before, bound.not_before)
            ),
            not_after=(
                grant.expires_at
                if bound.not_after is None
                else min(grant.expires_at, bound.not_after)
            ),
            budget={dimension: budget.get(dimension) for dimension in BudgetDimension},
        )
        object.__setattr__(self, "authority", folded)
        object.__setattr__(self, "budget", budget)

    @property
    def scopes(self) -> frozenset[str]:
        """Scopes this token can still exercise: the grant, narrowed by every caveat."""
        # Never None: __post_init__ substitutes the grant when no ScopeSubset applies.
        # `cast` rather than `assert` — asserts vanish under `-O`, so a guard written that
        # way is not a guard (the same reason `caveats.py` uses `cast`).
        return cast("frozenset[str]", self.authority.scopes)

    @property
    def granted_scopes(self) -> frozenset[str]:
        """Scopes the *authority block* carried, before any attenuation.

        The difference between this and `scopes` is what distinguishes
        `SCOPE_ATTENUATED_AWAY` from `SCOPE_NOT_GRANTED`.
        """
        return self.verified.scopes

    @property
    def depth(self) -> int:
        """Delegation depth, counted from the block chain rather than a declared fact."""
        return self.verified.depth

    @property
    def expires_at(self) -> datetime:
        """The effective expiry: the grant's, or an earlier one set by a caveat."""
        return cast("datetime", self.authority.not_after)


__all__ = ["AgentIdentity"]
