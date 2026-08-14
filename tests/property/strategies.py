"""Hypothesis strategies for attenuation property tests.

Spec 03 §6 lists what these generators MUST be able to produce, because a strategy that
cannot reach the counterexample shapes in §5 passes vacuously. `test_strategies.py`
audits that claim rather than trusting this docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from hypothesis import strategies as st

from agentiam_core.models import (
    MAX_DELEGATION_DEPTH,
    ArgOperator,
    ArgPredicate,
    Budget,
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    DepthLimit,
    IntentBound,
    Mandate,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)

EPOCH = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

#: A small vocabulary. Small on purpose: overlap between generated sets is what produces
#: interesting subset relationships, and a large alphabet would make almost every pair
#: disjoint and almost every test vacuous.
SCOPES = ["invoice:read", "vendor:read", "vendor:negotiate", "payment:initiate", "email:send"]
TOOLS = ["invoice_api", "vendor_api", "payment_api", "email_api"]
ARG_PATHS = ["payment.amount", "email.domain", "rows.count"]
DOMAINS = ["example.com", "corp.example", "evil.com"]

#: Bengali text, so EC-T16 is exercised by the generators rather than only by one example.
ROLES = ["negotiator", "doc-reader", "auditor", "ক্রয়-প্রতিনিধি", "নিরীক্ষক"]

HEX = "0123456789abcdef"


def intent_hashes() -> st.SearchStrategy[str]:
    """64-char lowercase hex. Only a few distinct values, so mismatches actually occur."""
    return st.sampled_from([c * 64 for c in "0123456789ab"])


def money() -> st.SearchStrategy[Decimal]:
    """Exact at 4 decimal places, non-negative, and small enough to overlap."""
    return st.decimals(
        min_value=Decimal(0),
        max_value=Decimal(1000),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    )


def scope_sets() -> st.SearchStrategy[frozenset[str]]:
    """Includes the empty set — EC-T14, a token that grants nothing."""
    return st.sets(st.sampled_from(SCOPES), max_size=len(SCOPES)).map(frozenset)


def tool_sets() -> st.SearchStrategy[frozenset[str]]:
    return st.sets(st.sampled_from(TOOLS), max_size=len(TOOLS)).map(frozenset)


def instants() -> st.SearchStrategy[datetime]:
    return st.integers(min_value=-120, max_value=120).map(lambda m: EPOCH + timedelta(minutes=m))


@st.composite
def time_windows(draw: st.DrawFn) -> TimeWindow:
    """Both bounds, either alone, and inverted intervals are excluded by the model."""
    shape = draw(st.sampled_from(["both", "lower", "upper"]))
    if shape == "lower":
        return TimeWindow(not_before=draw(instants()))
    if shape == "upper":
        return TimeWindow(not_after=draw(instants()))
    lower = draw(instants())
    return TimeWindow(
        not_before=lower,
        not_after=lower + timedelta(minutes=draw(st.integers(min_value=1, max_value=240))),
    )


@st.composite
def arg_predicates(draw: st.DrawFn) -> ArgPredicate:
    path = draw(st.sampled_from(ARG_PATHS))
    op = draw(st.sampled_from(list(ArgOperator)))
    if op in {ArgOperator.IN, ArgOperator.NOT_IN}:
        value: object = frozenset(draw(st.sets(st.sampled_from(DOMAINS), max_size=3)))
    elif op in {ArgOperator.EQ, ArgOperator.NE}:
        value = draw(st.one_of(st.sampled_from(DOMAINS), money()))
    else:
        value = draw(money())
    return ArgPredicate(path=path, op=op, value=value)  # type: ignore[arg-type]


def scope_subsets() -> st.SearchStrategy[ScopeSubset]:
    return scope_sets().map(lambda s: ScopeSubset(scopes=s))


def budget_ceilings() -> st.SearchStrategy[BudgetCeiling]:
    return st.builds(BudgetCeiling, dimension=st.sampled_from(list(BudgetDimension)), value=money())


def tool_allows() -> st.SearchStrategy[ToolAllow]:
    return tool_sets().map(lambda t: ToolAllow(tools=t))


def tool_denies() -> st.SearchStrategy[ToolDeny]:
    return tool_sets().map(lambda t: ToolDeny(tools=t))


def depth_limits() -> st.SearchStrategy[DepthLimit]:
    return st.integers(min_value=0, max_value=MAX_DELEGATION_DEPTH).map(
        lambda n: DepthLimit(max_depth=n)
    )


def intent_bounds() -> st.SearchStrategy[IntentBound]:
    return intent_hashes().map(lambda h: IntentBound(intent_hash=h))


def requires_approvals() -> st.SearchStrategy[RequiresApproval]:
    return scope_sets().map(lambda s: RequiresApproval(scopes=s))


#: One dedicated strategy per kind. Filtering a union instead would discard eight draws in
#: nine and trip hypothesis's filter health check.
BY_KIND: dict[type, st.SearchStrategy[Caveat]] = {
    ScopeSubset: scope_subsets(),
    BudgetCeiling: budget_ceilings(),
    TimeWindow: time_windows(),
    ToolAllow: tool_allows(),
    ToolDeny: tool_denies(),
    ArgPredicate: arg_predicates(),
    DepthLimit: depth_limits(),
    IntentBound: intent_bounds(),
    RequiresApproval: requires_approvals(),
}


def caveats() -> st.SearchStrategy[Caveat]:
    """Every one of the nine kinds, including the two with reversed narrowing order."""
    return st.one_of(*BY_KIND.values())


def caveats_of_kind(kind: type) -> st.SearchStrategy[Caveat]:
    """Caveats of one kind, drawn directly rather than filtered out of the union."""
    return BY_KIND[kind]


#: Operator families that constrain the same way, so caveats within one are comparable.
_ARG_FAMILIES: list[list[ArgOperator]] = [
    [ArgOperator.LE, ArgOperator.LT],
    [ArgOperator.GE, ArgOperator.GT],
    [ArgOperator.EQ],
    [ArgOperator.NE],
    [ArgOperator.IN],
    [ArgOperator.NOT_IN],
]


@st.composite
def same_slot_group(draw: st.DrawFn, size: int = 3) -> list[Caveat]:
    """Generate `size` caveats that are guaranteed comparable to each other.

    Reflexivity, transitivity, and antisymmetry are statements *within* a comparability
    slot. Drawing independently and filtering to matching slots would discard almost
    everything for `BudgetCeiling` (five dimensions) and `ArgPredicate` (three paths times
    six operator families), so the slot is fixed first and the values vary inside it.
    """
    kind = draw(
        st.sampled_from(
            [
                ScopeSubset,
                BudgetCeiling,
                TimeWindow,
                ToolAllow,
                ToolDeny,
                ArgPredicate,
                DepthLimit,
                IntentBound,
                RequiresApproval,
            ]
        )
    )
    if kind is BudgetCeiling:
        dimension = draw(st.sampled_from(list(BudgetDimension)))
        return [BudgetCeiling(dimension=dimension, value=draw(money())) for _ in range(size)]
    if kind is ArgPredicate:
        path = draw(st.sampled_from(ARG_PATHS))
        family = draw(st.sampled_from(_ARG_FAMILIES))
        return [draw(arg_predicates_in(path, family)) for _ in range(size)]
    if kind is TimeWindow:
        # A window's two sides are separate slots (see `attenuation.atoms`), so a group
        # must fix the side as well as the kind.
        side = draw(st.sampled_from(["lower", "upper", "both"]))
        return [draw(time_windows_on(side)) for _ in range(size)]
    return [draw(BY_KIND[kind]) for _ in range(size)]


@st.composite
def time_windows_on(draw: st.DrawFn, side: str) -> TimeWindow:
    """A window bounded on one named side, or on both."""
    if side == "lower":
        return TimeWindow(not_before=draw(instants()))
    if side == "upper":
        return TimeWindow(not_after=draw(instants()))
    lower = draw(instants())
    span = draw(st.integers(min_value=1, max_value=240))
    return TimeWindow(not_before=lower, not_after=lower + timedelta(minutes=span))


@st.composite
def arg_predicates_in(draw: st.DrawFn, path: str, family: list[ArgOperator]) -> ArgPredicate:
    """An argument predicate on a fixed path and operator family."""
    op = draw(st.sampled_from(family))
    if op in {ArgOperator.IN, ArgOperator.NOT_IN}:
        value: object = frozenset(draw(st.sets(st.sampled_from(DOMAINS), max_size=3)))
    elif op in {ArgOperator.EQ, ArgOperator.NE}:
        value = draw(st.sampled_from(DOMAINS))
    else:
        value = draw(money())
    return ArgPredicate(path=path, op=op, value=value)  # type: ignore[arg-type]


def budgets() -> st.SearchStrategy[Budget]:
    return st.builds(
        Budget,
        spend_bdt=money(),
        tool_calls=st.integers(min_value=0, max_value=1000),
        rows_read=st.integers(min_value=0, max_value=1000),
        external_emails=st.integers(min_value=0, max_value=100),
        wall_clock_s=st.integers(min_value=0, max_value=3600),
    )


@st.composite
def mandates(draw: st.DrawFn) -> Mandate:
    """A random mandate, with a validity window wide enough to attenuate inside."""
    not_before = draw(instants())
    return Mandate(
        mandate_id=UUID(int=draw(st.integers(min_value=0, max_value=2**60))),
        task_id=UUID(int=draw(st.integers(min_value=0, max_value=2**60))),
        principal_id=draw(st.sampled_from(["kc:alice", "kc:bob", "kc:ফারহানা"])),
        intent_hash=draw(intent_hashes()),
        scopes=draw(scope_sets()),
        budget=draw(budgets()),
        max_depth=draw(st.integers(min_value=1, max_value=MAX_DELEGATION_DEPTH)),
        not_before=not_before,
        expires_at=not_before + timedelta(hours=draw(st.integers(min_value=1, max_value=24))),
    )
