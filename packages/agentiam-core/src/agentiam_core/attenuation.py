"""Holder-side attenuation and the narrowing algebra.

Implements [`docs/specs/03-attenuation.md`](../../../../docs/specs/03-attenuation.md).

`attenuate()` is what makes AgentIAM different from an OAuth deployment: the holder of a
token mints a strictly narrower child **offline**, with no issuer round-trip and no network
access at all. A JWT holder cannot do this; narrowing a JWT requires the issuer.

**Where the security actually lives.** Not here. Biscuit blocks are append-only and every
clause in every block must pass, so a chain's authority is the *intersection* of its blocks'
constraints, and adding a caveat can only shrink an intersection. That holds whether or not
`narrows()` is ever called.

`narrows()` earns its place three other ways (spec 03 §2):

1. **Legible errors.** A child declaring a ৳100,000 ceiling under a parent capped at ৳50,000
   is not dangerous — the effective cap is still ৳50,000 — but it is a lie in the token, and
   the developer should hear about it at mint time rather than debug it later.
2. **A computable authority set**, which the identity tree and the custody query need.
3. **Testable invariants**: INV-1 as a property test needs a decidable comparison between
   two tokens' authority, and nine closed caveat kinds with a defined partial order give one.

So a `narrows()` bug produces a *misleading* token, not an over-privileged one. That
distinction is worth being able to state plainly.

No I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, cast

from biscuit_auth import BlockBuilder

from agentiam_core.caveats import block_datalog, quote_string, scale
from agentiam_core.errors import AttenuationError, CaveatError
from agentiam_core.models import (
    ArgOperator,
    ArgPredicate,
    Budget,
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    CaveatKind,
    DepthLimit,
    IntentBound,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)
from agentiam_core.tokens import HARD_SIZE_LIMIT_B64, VerifiedToken

#: Operators that bound a value from above, with whether the bound is inclusive.
_UPPER: Final[dict[ArgOperator, bool]] = {ArgOperator.LE: True, ArgOperator.LT: False}
#: Operators that bound a value from below.
_LOWER: Final[dict[ArgOperator, bool]] = {ArgOperator.GE: True, ArgOperator.GT: False}


def _op_family(op: ArgOperator) -> str:
    """Group operators that constrain the same way, so only those are compared."""
    if op in _UPPER:
        return "upper"
    if op in _LOWER:
        return "lower"
    return op.value


def comparability_slot(caveat: Caveat) -> tuple[object, ...]:
    """The key identifying which caveats constrain the same thing.

    Two caveats are comparable only within a slot. `BudgetCeiling` is per dimension and
    `ArgPredicate` is per path and operator family, so a spend ceiling and a call-count
    ceiling are simply unrelated rather than one narrowing the other. `TimeWindow` is per
    side, for the reason in `atoms()`.
    """
    match caveat:
        case BudgetCeiling():
            return (caveat.kind, caveat.dimension)
        case ArgPredicate():
            return (caveat.kind, caveat.path, _op_family(caveat.op))
        case TimeWindow():
            has_lower = caveat.not_before is not None
            has_upper = caveat.not_after is not None
            side = "both" if has_lower and has_upper else ("lower" if has_lower else "upper")
            return (caveat.kind, side)
        case _:
            return (caveat.kind,)


def atoms(caveat: Caveat) -> list[Caveat]:
    """Split a caveat into independently comparable pieces.

    Only `TimeWindow` splits, and the reason is worth stating because getting it wrong
    rejects safe attenuations.

    A `TimeWindow` carrying both bounds compiles to *two* Datalog clauses. A child that
    supplies only `not_after` is not claiming anything about the lower bound — it omits
    that clause entirely, and the ancestor's lower-bound clause still applies, because
    every clause in every block must pass. Treating the missing bound as "unbounded" would
    make a one-sided shortening look like a widening and refuse it, which is both wrong
    and the single most common thing a holder wants to do.

    Comparing side by side also keeps the order antisymmetric. Treating an absent bound as
    "inherited" *inside* `narrows()` would make an upper-bound-only and a lower-bound-only
    window mutually narrowing without being equal, and the algebra would stop being a
    partial order.
    """
    if (
        isinstance(caveat, TimeWindow)
        and caveat.not_before is not None
        and caveat.not_after is not None
    ):
        return [
            TimeWindow(not_before=caveat.not_before),
            TimeWindow(not_after=caveat.not_after),
        ]
    return [caveat]


def _bound_narrows(
    child: tuple[Decimal, bool], parent: tuple[Decimal, bool], *, upper: bool
) -> bool:
    """Compare two one-sided bounds as sets of permitted values.

    A bound is `(value, inclusive)`. For an upper bound the permitted set is `x < v` or
    `x <= v`; the child's set must be a subset of the parent's. At equal values the
    inclusive bound is the larger set, so an inclusive child under an exclusive parent is
    not narrowing.
    """
    cv, ci = child
    pv, pi = parent
    if cv == pv:
        return ci <= pi
    return cv < pv if upper else cv > pv


def narrows(child: Caveat, parent: Caveat) -> bool:
    """True iff `child` is at least as restrictive as `parent`.

    Reflexive, transitive, and antisymmetric up to equality (P-03, P-04).

    Two of the nine orders run *backwards* — `ToolDeny` and `RequiresApproval`, where
    covering more is stricter — and `IntentBound` narrows only by equality, because a
    different intent hash is a different task rather than a narrower one. Those three are
    where a hand-written implementation gets it wrong, so they are what the property tests
    hammer hardest.

    Raises:
        CaveatError: If the two caveats are not comparable. Returning False instead would
            let the mint-time check silently pass a comparison it never made.
    """
    if comparability_slot(child) != comparability_slot(parent):
        raise CaveatError(
            f"{child.kind.value} and {parent.kind.value} are not comparable "
            f"({comparability_slot(child)} vs {comparability_slot(parent)})"
        )

    match (child, parent):
        case (ScopeSubset(), ScopeSubset()):
            return child.scopes <= parent.scopes

        case (BudgetCeiling(), BudgetCeiling()):
            return child.value <= parent.value

        case (TimeWindow(), TimeWindow()):
            lower_ok = parent.not_before is None or (
                child.not_before is not None and child.not_before >= parent.not_before
            )
            upper_ok = parent.not_after is None or (
                child.not_after is not None and child.not_after <= parent.not_after
            )
            return lower_ok and upper_ok

        case (ToolAllow(), ToolAllow()):
            return child.tools <= parent.tools

        case (ToolDeny(), ToolDeny()):
            # Reversed: denying a superset is more restrictive.
            return child.tools >= parent.tools

        case (ArgPredicate(), ArgPredicate()):
            return _arg_narrows(child, parent)

        case (DepthLimit(), DepthLimit()):
            return child.max_depth <= parent.max_depth

        case (IntentBound(), IntentBound()):
            return child.intent_hash == parent.intent_hash

        case (RequiresApproval(), RequiresApproval()):
            # Reversed: requiring approval for a superset is more restrictive.
            return child.scopes >= parent.scopes

        case _:  # pragma: no cover - unreachable once slots match
            raise CaveatError(f"unhandled caveat kind {child.kind}")


def _arg_narrows(child: ArgPredicate, parent: ArgPredicate) -> bool:
    """Compare two argument predicates on the same path and operator family."""
    family = _op_family(child.op)
    if family == "upper":
        return _bound_narrows(
            (Decimal(str(child.value)), _UPPER[child.op]),
            (Decimal(str(parent.value)), _UPPER[parent.op]),
            upper=True,
        )
    if family == "lower":
        return _bound_narrows(
            (Decimal(str(child.value)), _LOWER[child.op]),
            (Decimal(str(parent.value)), _LOWER[parent.op]),
            upper=False,
        )
    if child.op is ArgOperator.IN:
        return cast("frozenset[str]", child.value) <= cast("frozenset[str]", parent.value)
    if child.op is ArgOperator.NOT_IN:
        # Reversed, for the same reason as ToolDeny: excluding more is stricter.
        return cast("frozenset[str]", child.value) >= cast("frozenset[str]", parent.value)
    # EQ pins one value and NE excludes one; either way only an identical predicate is
    # comparable, because two different pinned values are disjoint rather than nested.
    return child.value == parent.value


# ---------------------------------------------------------------------------
# The mint-time check
# ---------------------------------------------------------------------------


def _grant_caveats(parent: VerifiedToken) -> list[Caveat]:
    """Express the authority block's grant as caveats, so one rule checks everything.

    The grant is not stored as caveats in the token, but it bounds a child exactly as a
    caveat would. Rendering it this way means EC-T17, EC-T18, and EC-T19 fall out of the
    same comparison rather than needing three special cases.
    """
    caveats: list[Caveat] = [
        ScopeSubset(scopes=parent.scopes),
        DepthLimit(max_depth=parent.max_depth),
        IntentBound(intent_hash=parent.intent_hash),
        TimeWindow(not_before=parent.not_before, not_after=parent.expires_at),
    ]
    caveats += [
        BudgetCeiling(dimension=dimension, value=parent.budget.get(dimension))
        for dimension in BudgetDimension
    ]
    return caveats


def check_narrowing(caveats: Sequence[Caveat], ancestors: Sequence[Caveat]) -> None:
    """Verify every proposed caveat narrows each comparable ancestor.

    Args:
        caveats: The caveats being added.
        ancestors: Caveats already binding on the chain.

    Raises:
        AttenuationError: If any proposed caveat is wider than a comparable ancestor.
            Covers EC-T17 (a scope the parent lacks), EC-T18 (a raised ceiling), and
            EC-T19 (an extended expiry).
    """
    by_slot: dict[tuple[object, ...], list[Caveat]] = {}
    for ancestor in ancestors:
        for atom in atoms(ancestor):
            by_slot.setdefault(comparability_slot(atom), []).append(atom)

    for caveat in caveats:
        for atom in atoms(caveat):
            # An atom with no comparable ancestor is a brand-new restriction, which always
            # narrows (spec 03 §3.1).
            for ancestor in by_slot.get(comparability_slot(atom), []):
                if not narrows(atom, ancestor):
                    raise AttenuationError(
                        f"{caveat.kind.value} caveat is wider than an ancestor: "
                        f"{atom!r} does not narrow {ancestor!r}"
                    )


def attenuate(
    parent: VerifiedToken,
    caveats: Sequence[Caveat],
    *,
    agent_id: str,
    role: str,
    ancestor_caveats: Sequence[Caveat] = (),
) -> str:
    """Mint a strictly narrower child token, offline.

    No network, no issuer round-trip, no shared state — the whole point of the design
    (INV-3). The child is signed with an ephemeral key biscuit generates internally.

    The proposed caveats are checked against the authority block's grant, which is always
    available from `parent`. Caveats added by *intermediate* blocks are not recoverable
    from a `VerifiedToken`, so a holder that knows them should pass them as
    `ancestor_caveats` for a tighter check. Omitting them cannot make the child token
    wider — biscuit's append-only structure guarantees that — it only means a wider
    *declared* caveat slips through as a misleading value rather than an error.

    Args:
        parent: The verified parent token.
        caveats: Restrictions to add.
        agent_id: Identity of the sub-agent receiving the child token.
        role: Human-readable role, for the console and audit trail.
        ancestor_caveats: Caveats from intermediate blocks, if the holder knows them.

    Returns:
        The child token, base64-encoded.

    Raises:
        AttenuationError: If a proposed caveat would widen authority, or if the child
            would exceed the hard size limit. No token is produced in either case.
    """
    check_narrowing(caveats, [*_grant_caveats(parent), *ancestor_caveats])

    lines = [
        f"agent({quote_string(agent_id)});",
        f"role({quote_string(role)});",
        # Advisory only. Authorization depth comes from the block count (ADR-005).
        f"declared_depth({parent.depth + 1});",
    ]
    body = block_datalog(list(caveats))
    if body:
        lines.append(body)

    child = parent.biscuit.append(BlockBuilder("\n".join(lines))).to_base64()
    if len(child) > HARD_SIZE_LIMIT_B64:
        raise AttenuationError(
            f"child token would be {len(child)} base64 characters, over the "
            f"{HARD_SIZE_LIMIT_B64} limit; shorten the chain or use fewer caveats"
        )
    return child


# ---------------------------------------------------------------------------
# Effective bound
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectiveAuthority:
    """What a chain actually permits, folded from every caveat binding on it.

    For the identity tree (T-045) and the custody query, which must show what a token
    *can* do rather than what any single block said.
    """

    scopes: frozenset[str] | None
    budget: dict[BudgetDimension, Decimal]
    not_before: datetime | None
    not_after: datetime | None
    tools_allowed: frozenset[str] | None
    tools_denied: frozenset[str]
    max_depth: int | None
    intent_hash: str | None
    approval_required: frozenset[str]

    @property
    def is_dead(self) -> bool:
        """True if the token can never authorize anything.

        An empty scope set or an inverted time window means the chain is inert. The
        console must render that plainly rather than showing a token that looks alive.
        """
        if self.scopes is not None and not self.scopes:
            return True
        return (
            self.not_before is not None
            and self.not_after is not None
            and self.not_after <= self.not_before
        )


def effective_bound(caveats: Sequence[Caveat]) -> EffectiveAuthority:
    """Fold a caveat chain into the authority it actually leaves (spec 03 §3.3).

    Sets intersect, ceilings take the minimum, windows take the tightest overlap, and the
    two reversed orders accumulate by union.
    """
    scopes: frozenset[str] | None = None
    tools_allowed: frozenset[str] | None = None
    tools_denied: frozenset[str] = frozenset()
    approval: frozenset[str] = frozenset()
    budget: dict[BudgetDimension, Decimal] = {}
    not_before: datetime | None = None
    not_after: datetime | None = None
    max_depth: int | None = None
    intent_hash: str | None = None

    for caveat in caveats:
        match caveat:
            case ScopeSubset():
                scopes = caveat.scopes if scopes is None else scopes & caveat.scopes
            case BudgetCeiling():
                current = budget.get(caveat.dimension)
                budget[caveat.dimension] = (
                    caveat.value if current is None else min(current, caveat.value)
                )
            case TimeWindow():
                if caveat.not_before is not None:
                    not_before = (
                        caveat.not_before
                        if not_before is None
                        else max(not_before, caveat.not_before)
                    )
                if caveat.not_after is not None:
                    not_after = (
                        caveat.not_after if not_after is None else min(not_after, caveat.not_after)
                    )
            case ToolAllow():
                tools_allowed = (
                    caveat.tools if tools_allowed is None else tools_allowed & caveat.tools
                )
            case ToolDeny():
                tools_denied |= caveat.tools
            case DepthLimit():
                max_depth = (
                    caveat.max_depth if max_depth is None else min(max_depth, caveat.max_depth)
                )
            case IntentBound():
                intent_hash = caveat.intent_hash
            case RequiresApproval():
                approval |= caveat.scopes
            case ArgPredicate():
                # Argument predicates are per-path and per-operator; they do not fold into
                # a single displayable bound, so the console shows them individually.
                #
                # Coverage reports one partial branch here — the fall-through when a
                # caveat matches none of the nine cases. It is unreachable because the
                # union is closed, and keeping the arm explicit is what makes mypy fail if
                # a tenth kind is ever added without deciding how it folds.
                pass

    return EffectiveAuthority(
        scopes=scopes,
        budget=budget,
        not_before=not_before,
        not_after=not_after,
        tools_allowed=tools_allowed,
        tools_denied=tools_denied,
        max_depth=max_depth,
        intent_hash=intent_hash,
        approval_required=approval,
    )


def effective_budget(caveats: Sequence[Caveat], ceiling: Budget) -> Budget:
    """Fold budget ceilings against a starting budget, per dimension."""
    bound = effective_bound(caveats)
    return Budget.from_scaled(
        {
            dimension: min(ceiling.scaled(dimension), scale(bound.budget[dimension]))
            if dimension in bound.budget
            else ceiling.scaled(dimension)
            for dimension in BudgetDimension
        }
    )


__all__ = [
    "CaveatKind",
    "EffectiveAuthority",
    "attenuate",
    "check_narrowing",
    "comparability_slot",
    "effective_bound",
    "effective_budget",
    "narrows",
]
