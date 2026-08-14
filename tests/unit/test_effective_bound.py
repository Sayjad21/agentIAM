"""Folding a caveat chain into the authority it actually leaves (spec 03 §3.3).

The identity tree (T-045) and the custody query must show what a token *can* do, not what
any single block said. That means intersecting sets, taking minima of ceilings, and
overlapping windows — and it means the two reversed orders accumulate by union rather than
intersection, which is the part that is easy to get backwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from agentiam_core.attenuation import (
    atoms,
    attenuate,
    comparability_slot,
    effective_bound,
    effective_budget,
)
from agentiam_core.errors import AttenuationError
from agentiam_core.models import (
    ArgOperator,
    ArgPredicate,
    Budget,
    BudgetCeiling,
    BudgetDimension,
    DepthLimit,
    IntentBound,
    Mandate,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)
from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root, verify

T0 = datetime(2026, 8, 14, 11, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)
INTENT = "a" * 64


class TestFold:
    def test_scope_sets_intersect(self) -> None:
        bound = effective_bound(
            [
                ScopeSubset(scopes=frozenset({"invoice:read", "vendor:read"})),
                ScopeSubset(scopes=frozenset({"vendor:read", "payment:initiate"})),
            ]
        )
        assert bound.scopes == frozenset({"vendor:read"})

    def test_ceilings_take_the_minimum_per_dimension(self) -> None:
        bound = effective_bound(
            [
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(100)),
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(40)),
                BudgetCeiling(dimension=BudgetDimension.TOOL_CALLS, value=Decimal(7)),
            ]
        )
        assert bound.budget[BudgetDimension.SPEND_BDT] == Decimal(40)
        assert bound.budget[BudgetDimension.TOOL_CALLS] == Decimal(7)

    def test_windows_take_the_tightest_overlap(self) -> None:
        bound = effective_bound(
            [TimeWindow(not_before=T0, not_after=T2), TimeWindow(not_before=T1, not_after=T2)]
        )
        assert bound.not_before == T1
        assert bound.not_after == T2

    def test_one_sided_windows_combine(self) -> None:
        bound = effective_bound([TimeWindow(not_before=T0), TimeWindow(not_after=T1)])
        assert bound.not_before == T0
        assert bound.not_after == T1

    def test_tool_allow_intersects_and_deny_unions(self) -> None:
        """The asymmetry is the point: allow narrows by intersection, deny by union."""
        bound = effective_bound(
            [
                ToolAllow(tools=frozenset({"a", "b", "c"})),
                ToolAllow(tools=frozenset({"b", "c"})),
                ToolDeny(tools=frozenset({"c"})),
                ToolDeny(tools=frozenset({"d"})),
            ]
        )
        assert bound.tools_allowed == frozenset({"b", "c"})
        assert bound.tools_denied == frozenset({"c", "d"})

    def test_depth_takes_the_minimum(self) -> None:
        assert effective_bound([DepthLimit(max_depth=5), DepthLimit(max_depth=2)]).max_depth == 2

    def test_approval_scopes_accumulate(self) -> None:
        bound = effective_bound(
            [
                RequiresApproval(scopes=frozenset({"payment:initiate"})),
                RequiresApproval(scopes=frozenset({"email:send"})),
            ]
        )
        assert bound.approval_required == frozenset({"payment:initiate", "email:send"})

    def test_intent_is_carried_through(self) -> None:
        assert effective_bound([IntentBound(intent_hash=INTENT)]).intent_hash == INTENT

    def test_arg_predicates_do_not_fold(self) -> None:
        """Per-path and per-operator, so there is no single displayable bound."""
        bound = effective_bound([ArgPredicate(path="n", op=ArgOperator.LE, value=Decimal(1))])
        assert bound.scopes is None
        assert bound.budget == {}

    def test_an_arg_predicate_does_not_swallow_later_caveats(self) -> None:
        """The skipped kind must not short-circuit the fold."""
        bound = effective_bound(
            [
                ArgPredicate(path="n", op=ArgOperator.LE, value=Decimal(1)),
                ScopeSubset(scopes=frozenset({"invoice:read"})),
                DepthLimit(max_depth=2),
            ]
        )
        assert bound.scopes == frozenset({"invoice:read"})
        assert bound.max_depth == 2

    def test_empty_chain_constrains_nothing(self) -> None:
        bound = effective_bound([])
        assert bound.scopes is None
        assert bound.tools_allowed is None
        assert bound.tools_denied == frozenset()
        assert bound.max_depth is None
        assert not bound.is_dead


class TestIsDead:
    def test_empty_scope_set_is_dead(self) -> None:
        """EC-T14: the console must show this plainly, not as a live token."""
        assert effective_bound([ScopeSubset(scopes=frozenset())]).is_dead

    def test_disjoint_scope_sets_are_dead(self) -> None:
        bound = effective_bound(
            [
                ScopeSubset(scopes=frozenset({"invoice:read"})),
                ScopeSubset(scopes=frozenset({"payment:initiate"})),
            ]
        )
        assert bound.is_dead

    def test_inverted_window_is_dead(self) -> None:
        bound = effective_bound([TimeWindow(not_before=T2), TimeWindow(not_after=T0)])
        assert bound.is_dead

    def test_touching_window_is_dead(self) -> None:
        """The expiry boundary is exclusive, so a zero-width window authorizes nothing."""
        assert effective_bound([TimeWindow(not_before=T1), TimeWindow(not_after=T1)]).is_dead

    def test_a_normal_chain_is_alive(self) -> None:
        bound = effective_bound(
            [
                ScopeSubset(scopes=frozenset({"invoice:read"})),
                TimeWindow(not_before=T0, not_after=T2),
            ]
        )
        assert not bound.is_dead

    def test_only_a_lower_bound_is_not_dead(self) -> None:
        assert not effective_bound([TimeWindow(not_before=T0)]).is_dead


class TestEffectiveBudget:
    def test_caveats_lower_the_starting_ceiling(self) -> None:
        result = effective_budget(
            [BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(40))],
            Budget(spend_bdt=Decimal(100), tool_calls=9),
        )
        assert result.spend_bdt == Decimal(40)
        assert result.tool_calls == 9  # untouched dimension keeps the mandate's ceiling

    def test_a_caveat_cannot_raise_the_ceiling(self) -> None:
        """Even a wider caveat folds to the minimum; the mandate still bounds it."""
        result = effective_budget(
            [BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(500))],
            Budget(spend_bdt=Decimal(100)),
        )
        assert result.spend_bdt == Decimal(100)

    def test_no_caveats_leaves_the_ceiling_alone(self) -> None:
        ceiling = Budget(spend_bdt=Decimal("12.3456"), rows_read=5)
        assert effective_budget([], ceiling) == ceiling

    def test_zero_caveat_zeroes_the_dimension(self) -> None:
        result = effective_budget(
            [BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal(0))],
            Budget(spend_bdt=Decimal(100)),
        )
        assert result.spend_bdt == Decimal(0)


class TestAtoms:
    def test_two_sided_window_splits(self) -> None:
        split = atoms(TimeWindow(not_before=T0, not_after=T1))
        assert len(split) == 2
        assert {comparability_slot(a)[1] for a in split} == {"lower", "upper"}

    @pytest.mark.parametrize(
        "caveat",
        [
            TimeWindow(not_before=T0),
            TimeWindow(not_after=T1),
            ScopeSubset(scopes=frozenset({"invoice:read"})),
            DepthLimit(max_depth=1),
        ],
    )
    def test_everything_else_is_already_atomic(self, caveat: object) -> None:
        assert atoms(caveat) == [caveat]  # type: ignore[arg-type]

    def test_a_one_sided_child_narrows_a_two_sided_ancestor(self) -> None:
        """The reason atoms exist: shortening expiry must not look like widening."""
        root = generate_keypair()
        key_set = RootKeySet([root.public_key])
        mandate = Mandate(
            mandate_id=uuid4(),
            task_id=uuid4(),
            principal_id="kc:alice",
            intent_hash=INTENT,
            scopes=frozenset({"invoice:read"}),
            budget=Budget(spend_bdt=Decimal(10)),
            max_depth=4,
            not_before=T0,
            expires_at=T2,
        )
        parent = verify(mint_root(mandate, root.private_key), key_set, now=T0)

        # Shortening only the upper bound is accepted.
        child = attenuate(parent, [TimeWindow(not_after=T1)], agent_id="agt", role="worker")
        assert verify(child, key_set, now=T0).depth == 1

        # Extending it is still refused.
        with pytest.raises(AttenuationError, match="time_window"):
            attenuate(
                parent,
                [TimeWindow(not_after=T2 + timedelta(hours=1))],
                agent_id="agt",
                role="worker",
            )


class TestChildSizeLimit:
    """A child that would exceed the hard limit is refused, like an oversized mint."""

    def test_oversized_child_is_refused(self) -> None:
        root = generate_keypair()
        key_set = RootKeySet([root.public_key])

        # 140 scopes puts the root at ~7,050 base64 characters — inside the 8 KB ceiling,
        # but close enough that one attenuation block tips it over. Measured; above 160
        # the root itself is refused, below 120 the child still fits.
        scopes = frozenset(f"resource{i:04d}:read" for i in range(140))
        mandate = Mandate(
            mandate_id=uuid4(),
            task_id=uuid4(),
            principal_id="kc:alice",
            intent_hash=INTENT,
            scopes=scopes,
            budget=Budget(spend_bdt=Decimal(10)),
            max_depth=4,
            not_before=T0,
            expires_at=T2,
        )
        token = mint_root(mandate, root.private_key)
        assert len(token) < 8192, "fixture must start under the limit"
        parent = verify(token, key_set, now=T0)

        with pytest.raises(AttenuationError, match="over the"):
            attenuate(
                parent,
                [ScopeSubset(scopes=scopes)],
                agent_id="agt",
                role="worker",
            )
