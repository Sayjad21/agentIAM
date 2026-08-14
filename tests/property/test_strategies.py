"""Audit the generators against spec 03 §6.

The roadmap's note on T-009 is: *check the property-test strategies, not just that the
tests pass — a weak strategy passes vacuously.* A generator that never produces two
overlapping scope sets would make every subset check trivially true, and INV-1 would report
confidence it had not earned.

So each test here asserts a shape from spec 03 §6 is actually reachable, by drawing until
it appears. These fail if the strategies are ever narrowed.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agentiam_core.attenuation import comparability_slot, narrows
from agentiam_core.models import (
    ArgOperator,
    BudgetCeiling,
    CaveatKind,
    DepthLimit,
    IntentBound,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)
from tests.property.strategies import arg_predicates, caveats, mandates

DRAWS = 400


def _sample(strategy: st.SearchStrategy[object], n: int = DRAWS) -> list[object]:
    """Collect n draws through hypothesis's own generation.

    Via `@given` rather than `.example()`: the latter warns, is slower, and does not use
    the same generation path the real properties do — so auditing it would be auditing
    something other than what the tests run against.
    """
    collected: list[object] = []

    @given(strategy)
    @settings(
        max_examples=n,
        deadline=None,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    def collect(value: object) -> None:
        collected.append(value)

    collect()
    return collected


@pytest.fixture(scope="module")
def caveat_sample() -> list[object]:
    return _sample(caveats())


class TestKindCoverage:
    def test_every_caveat_kind_is_generated(self, caveat_sample: list[object]) -> None:
        """All nine kinds, or a whole branch of the algebra goes untested."""
        seen = {c.kind for c in caveat_sample}  # type: ignore[attr-defined]
        missing = set(CaveatKind) - seen
        assert not missing, f"strategy never generates {sorted(k.value for k in missing)}"

    def test_every_budget_dimension_is_generated(self, caveat_sample: list[object]) -> None:
        dims = {c.dimension for c in caveat_sample if isinstance(c, BudgetCeiling)}
        assert len(dims) >= 4, f"only {len(dims)} budget dimensions generated"

    def test_every_arg_operator_is_generated(self) -> None:
        """Drawn from the ArgPredicate strategy directly.

        Sampling the union instead would leave only about a ninth of the draws here, and
        an operator could be missed by chance rather than by a real gap — which would make
        this audit flaky, and a flaky audit gets deleted.
        """
        ops = {c.op for c in _sample(arg_predicates(), 300)}  # type: ignore[attr-defined]
        missing = set(ArgOperator) - ops
        assert not missing, f"strategy never generates {sorted(o.value for o in missing)}"


class TestShapeCoverage:
    """The specific shapes spec 03 §6 requires."""

    def test_empty_scope_sets_occur(self, caveat_sample: list[object]) -> None:
        """EC-T14: a token that grants nothing."""
        assert any(isinstance(c, ScopeSubset) and not c.scopes for c in caveat_sample)

    def test_zero_ceilings_occur(self, caveat_sample: list[object]) -> None:
        assert any(isinstance(c, BudgetCeiling) and c.value == 0 for c in caveat_sample)

    def test_depth_zero_and_max_both_occur(self, caveat_sample: list[object]) -> None:
        depths = {c.max_depth for c in caveat_sample if isinstance(c, DepthLimit)}
        assert 0 in depths
        assert max(depths) >= 6

    def test_time_windows_of_all_three_shapes_occur(self, caveat_sample: list[object]) -> None:
        shapes: Counter[tuple[bool, bool]] = Counter()
        for c in caveat_sample:
            if isinstance(c, TimeWindow):
                shapes[(c.not_before is not None, c.not_after is not None)] += 1
        assert shapes[(True, False)], "no lower-bound-only window"
        assert shapes[(False, True)], "no upper-bound-only window"
        assert shapes[(True, True)], "no two-sided window"

    def test_non_ascii_text_occurs(self) -> None:
        """EC-T16: Bengali must be exercised by the generators, not one example."""
        roles = {m.principal_id for m in _sample(mandates(), 200)}  # type: ignore[attr-defined]
        assert any(not r.isascii() for r in roles), "no non-ASCII principal generated"

    def test_mandates_reach_the_full_depth_range(self) -> None:
        depths = {m.max_depth for m in _sample(mandates(), 200)}  # type: ignore[attr-defined]
        assert min(depths) == 1
        assert max(depths) == 8


class TestRelationshipCoverage:
    """Generating the kinds is not enough: the *relationships* must occur too."""

    def test_comparable_pairs_are_common(self, caveat_sample: list[object]) -> None:
        """If pairs were almost always incomparable, transitivity would be vacuous."""
        slots = Counter(comparability_slot(c) for c in caveat_sample)  # type: ignore[arg-type]
        repeated = sum(n for n in slots.values() if n > 1)
        assert repeated > len(caveat_sample) // 2, (
            "most generated caveats share no slot with another; the ordering properties "
            "would pass vacuously"
        )

    def test_both_narrowing_directions_occur(self, caveat_sample: list[object]) -> None:
        """Strict narrowing in each direction, and neither, must all be reachable."""
        outcomes: Counter[tuple[bool, bool]] = Counter()
        by_slot: dict[tuple[object, ...], list[object]] = {}
        for c in caveat_sample:
            by_slot.setdefault(comparability_slot(c), []).append(c)  # type: ignore[arg-type]
        for group in by_slot.values():
            for a in group[:12]:
                for b in group[:12]:
                    outcomes[(narrows(a, b), narrows(b, a))] += 1  # type: ignore[arg-type]
        assert outcomes[(True, False)], "no strictly-narrowing pair generated"
        assert outcomes[(False, True)], "no strictly-widening pair generated"
        assert outcomes[(False, False)], "no incomparable-but-same-slot pair generated"
        assert outcomes[(True, True)], "no equivalent pair generated"

    def test_reversed_order_kinds_produce_strict_pairs(self, caveat_sample: list[object]) -> None:
        """ToolDeny and RequiresApproval are where the order runs backwards.

        If the generator never produced a strict subset for these, an implementation with
        the comparison inverted would pass every test.
        """
        for kind in (ToolDeny, RequiresApproval):
            items = [c for c in caveat_sample if isinstance(c, kind)]
            strict = [(a, b) for a in items for b in items if narrows(a, b) and not narrows(b, a)]
            assert strict, f"no strictly-narrowing {kind.__name__} pair generated"

    def test_intent_mismatches_occur(self, caveat_sample: list[object]) -> None:
        """IntentBound narrows only by equality, so mismatches must be reachable."""
        hashes = {c.intent_hash for c in caveat_sample if isinstance(c, IntentBound)}
        assert len(hashes) > 1, "only one intent hash generated; mismatch never tested"

    def test_tool_allow_sets_overlap_partially(self, caveat_sample: list[object]) -> None:
        """Partial overlap is the interesting case: neither set contains the other."""
        items = [c.tools for c in caveat_sample if isinstance(c, ToolAllow)]
        assert any(a & b and not (a <= b or b <= a) for a in items[:40] for b in items[:40]), (
            "tool sets never partially overlap"
        )


class TestMandateCoverage:
    @given(mandates())
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_generated_mandates_are_valid(self, mandate: object) -> None:
        """Every draw must be constructible; a filtered-out shape is a silent gap."""
        assert mandate.expires_at > mandate.not_before  # type: ignore[attr-defined]

    def test_windows_are_wide_enough_to_attenuate_inside(self) -> None:
        """A zero-width window would make the INV-9 shortening test vacuous."""
        for mandate in _sample(mandates(), 100):
            span = mandate.expires_at - mandate.not_before  # type: ignore[attr-defined]
            assert span >= timedelta(hours=1)
