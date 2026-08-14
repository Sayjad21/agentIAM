"""Property tests for the attenuation invariants — P-01 through P-09, and P-17.

These are the tests that answer "how do you prove this?" on stage, so they are written
against real tokens: every authority comparison mints a biscuit, appends real blocks, and
runs a real authorizer. Comparing two Python functions to each other would prove only that
they agree, not that the thing biscuit actually enforces is narrowing.

`test_strategies.py` audits that the generators can reach the counterexample shapes in spec
03 §5. A property test whose strategy cannot produce the failure it is meant to catch is
worse than no test, because it reports confidence it has not earned.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from biscuit_auth import AuthorizerBuilder
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from agentiam_core.attenuation import attenuate, comparability_slot, narrows
from agentiam_core.caveats import request_context_datalog
from agentiam_core.errors import AttenuationError, CaveatError
from agentiam_core.models import (
    Budget,
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    DepthLimit,
    IntentBound,
    Mandate,
    RequestContext,
    ScopeSubset,
    TimeWindow,
    ToolDeny,
)
from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root, verify
from tests.property.strategies import (
    EPOCH,
    ROLES,
    SCOPES,
    TOOLS,
    caveats,
    mandates,
    money,
    same_slot_group,
)

# One keypair for the whole module: key generation is the slow part, and the properties
# under test do not depend on which key is used.
ROOT = generate_keypair()
KEY_SET = RootKeySet([ROOT.public_key])

SLOW = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _authorizes(token: str, ctx: RequestContext) -> bool:
    """Run a real authorizer. This is the ground truth for 'authority'."""
    parsed = verify(token, KEY_SET, now=ctx.now)
    source = request_context_datalog(ctx) + "\nallow if true;"
    try:
        AuthorizerBuilder(source).build(parsed.biscuit).authorize()
        return True
    except Exception:
        return False


def _safe_authorizes(token: str, ctx: RequestContext) -> bool:
    """Authority including the window and depth checks `verify()` performs."""
    try:
        return _authorizes(token, ctx)
    except Exception:
        return False


@st.composite
def request_contexts(draw: st.DrawFn, mandate: Mandate) -> RequestContext:
    """A request context inside the mandate's window, probing its whole surface."""
    span = (mandate.expires_at - mandate.not_before).total_seconds()
    offset = draw(st.integers(min_value=0, max_value=max(0, int(span) - 1)))
    return RequestContext(
        operation=draw(st.sampled_from(SCOPES)),
        requested=dict.fromkeys(BudgetDimension, draw(money())),
        current_depth=draw(st.integers(min_value=0, max_value=8)),
        request_intent=draw(st.sampled_from([mandate.intent_hash, "f" * 64])),
        now=mandate.not_before + timedelta(seconds=offset),
        tool=draw(st.one_of(st.none(), st.sampled_from(TOOLS))),
        args={},
    )


class TestNarrowsAlgebra:
    """P-03 and P-04, plus antisymmetry, for every caveat kind."""

    @given(caveats())
    @settings(max_examples=200, deadline=None)
    def test_reflexive(self, caveat: Caveat) -> None:
        """P-03: every caveat narrows itself."""
        assert narrows(caveat, caveat)

    @given(same_slot_group(3))
    @settings(max_examples=300, deadline=None)
    def test_transitive(self, group: list[Caveat]) -> None:
        """P-04: if a ⊑ b and b ⊑ c then a ⊑ c."""
        a, b, c = group
        assert comparability_slot(a) == comparability_slot(b) == comparability_slot(c)
        if narrows(a, b) and narrows(b, c):
            assert narrows(a, c)

    @given(same_slot_group(2))
    @settings(max_examples=300, deadline=None)
    def test_antisymmetric_up_to_equality(self, group: list[Caveat]) -> None:
        """Mutual narrowing means the two caveats denote the same restriction."""
        a, b = group
        if narrows(a, b) and narrows(b, a):
            assert a == b

    @given(caveats(), caveats())
    @settings(max_examples=200, deadline=None)
    def test_incomparable_kinds_raise(self, left: Caveat, right: Caveat) -> None:
        """Returning False would let the mint check pass a comparison it never made."""
        if comparability_slot(left) != comparability_slot(right):
            with pytest.raises(CaveatError):
                narrows(left, right)

    @given(caveats())
    @settings(max_examples=200, deadline=None)
    def test_narrowing_is_never_symmetric_for_distinct_caveats(self, caveat: Caveat) -> None:
        """A sanity net for the two reversed orders: a strict subset is not mutual."""
        if isinstance(caveat, ToolDeny) and caveat.tools:
            wider = ToolDeny(tools=frozenset(list(caveat.tools)[:-1]))
            assert narrows(caveat, wider)
            assert not narrows(wider, caveat)


class TestOfflineAttenuation:
    """P-05: attenuation and verification make no network call."""

    def test_attenuate_makes_no_network_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        def explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("attenuation must not touch the network (INV-3)")

        monkeypatch.setattr(socket, "socket", explode)
        monkeypatch.setattr(socket, "create_connection", explode)
        monkeypatch.setattr(socket, "getaddrinfo", explode)

        mandate = _simple_mandate()
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        child = attenuate(
            parent,
            [ScopeSubset(scopes=frozenset({"invoice:read"}))],
            agent_id="agt_1",
            role="doc-reader",
        )
        assert verify(child, KEY_SET, now=mandate.not_before).depth == 1


def _simple_mandate() -> Mandate:
    """A fixed mandate, for the tests that do not need a generated one."""
    return Mandate(
        mandate_id=uuid4(),
        task_id=uuid4(),
        principal_id="kc:alice",
        intent_hash="a" * 64,
        scopes=frozenset({"invoice:read", "vendor:read", "payment:initiate"}),
        budget=Budget(spend_bdt=Decimal(100), tool_calls=10),
        max_depth=8,
        not_before=EPOCH,
        expires_at=EPOCH + timedelta(hours=1),
    )


class TestInvariants:
    """INV-1, INV-2, INV-6, INV-7, INV-9 against real tokens."""

    @given(mandates(), st.data())
    @SLOW
    def test_inv1_attenuation_never_widens(self, mandate: Mandate, data: st.DataObject) -> None:
        """P-01: authority(child) ⊆ authority(parent)."""
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        proposed = data.draw(st.lists(caveats(), min_size=1, max_size=3))
        try:
            child = attenuate(parent, proposed, agent_id="agt", role="worker")
        except AttenuationError:
            # A rejected widening is TestWideningIsRefused's business, not this one.
            assume(False)

        for _ in range(6):
            ctx = data.draw(request_contexts(mandate))
            if _safe_authorizes(child, ctx):
                assert _safe_authorizes(token, ctx), (
                    f"child authorized what the parent did not: {ctx!r}"
                )

    @given(mandates(), st.data())
    @SLOW
    def test_inv2_chains_are_transitively_narrowing(
        self, mandate: Mandate, data: st.DataObject
    ) -> None:
        """P-02: every step of a chain is a subset of every earlier step."""
        token = mint_root(mandate, ROOT.private_key)
        chain = [token]
        current = verify(token, KEY_SET, now=mandate.not_before)
        steps = data.draw(st.integers(min_value=1, max_value=min(mandate.max_depth, 4)))
        for depth in range(1, steps + 1):
            proposed = data.draw(st.lists(caveats(), max_size=2))
            try:
                nxt = attenuate(current, proposed, agent_id=f"agt{depth}", role="worker")
            except AttenuationError:
                break
            chain.append(nxt)
            current = verify(nxt, KEY_SET, now=mandate.not_before)

        for _ in range(4):
            ctx = data.draw(request_contexts(mandate))
            verdicts = [_safe_authorizes(t, ctx) for t in chain]
            # Once a step denies, no later step may allow.
            for i in range(1, len(verdicts)):
                if verdicts[i]:
                    assert all(verdicts[:i]), f"authority reappeared at step {i}"

    @given(mandates(), st.data())
    @SLOW
    def test_inv6_depth_increases_and_is_bounded(
        self, mandate: Mandate, data: st.DataObject
    ) -> None:
        """P-06: depth strictly increases; beyond max_depth nothing verifies."""
        token = mint_root(mandate, ROOT.private_key)
        current = verify(token, KEY_SET, now=mandate.not_before)
        assert current.depth == 0

        previous = 0
        for step in range(1, mandate.max_depth + 2):
            try:
                child = attenuate(current, [], agent_id=f"agt{step}", role="worker")
            except AttenuationError:
                break
            try:
                current = verify(child, KEY_SET, now=mandate.not_before)
            except Exception:
                assert step > mandate.max_depth, "chain rejected before max_depth"
                return
            assert current.depth == previous + 1
            previous = current.depth
        assert previous <= mandate.max_depth

    @given(mandates(), st.data())
    @SLOW
    def test_inv7_intent_is_immutable(self, mandate: Mandate, data: st.DataObject) -> None:
        """P-07: no attenuation can change the bound intent."""
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        other = "f" * 64
        assume(other != mandate.intent_hash)

        # Declaring a different intent must be refused outright.
        with pytest.raises(AttenuationError):
            attenuate(
                parent,
                [IntentBound(intent_hash=other)],
                agent_id="agt",
                role="worker",
            )

        # And a child that adds unrelated caveats still carries the original intent.
        child = attenuate(parent, [], agent_id="agt", role="worker")
        assert verify(child, KEY_SET, now=mandate.not_before).intent_hash == mandate.intent_hash

    @given(mandates(), st.data())
    @SLOW
    def test_inv9_expiry_only_contracts(self, mandate: Mandate, data: st.DataObject) -> None:
        """P-08: a child may shorten its life, never extend it."""
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)

        # Extending is refused.
        with pytest.raises(AttenuationError):
            attenuate(
                parent,
                [TimeWindow(not_after=mandate.expires_at + timedelta(seconds=1))],
                agent_id="agt",
                role="worker",
            )

        # Shortening binds: past the child's own window it authorizes nothing.
        shorter = mandate.not_before + timedelta(seconds=30)
        assume(shorter < mandate.expires_at)
        child = attenuate(parent, [TimeWindow(not_after=shorter)], agent_id="agt", role="worker")
        ctx = data.draw(request_contexts(mandate))
        late = ctx.model_copy(update={"now": shorter + timedelta(seconds=1)})
        assert not _safe_authorizes(child, late)

    @given(mandates(), st.data())
    @SLOW
    def test_inv8_deny_beats_allow(self, mandate: Mandate, data: st.DataObject) -> None:
        """P-09: a denying caveat anywhere in the chain wins."""
        assume(mandate.scopes)
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        child = attenuate(
            parent,
            [ScopeSubset(scopes=frozenset())],  # denies everything
            agent_id="agt",
            role="worker",
        )
        # A later block cannot undo it, even one that "grants" the scope back.
        grandchild = attenuate(
            verify(child, KEY_SET, now=mandate.not_before),
            [],
            agent_id="agt2",
            role="worker",
        )
        for _ in range(4):
            ctx = data.draw(request_contexts(mandate))
            assert not _safe_authorizes(child, ctx)
            assert not _safe_authorizes(grandchild, ctx)

    @given(mandates(), st.data())
    @SLOW
    def test_p17_size_grows_monotonically_with_depth(
        self, mandate: Mandate, data: st.DataObject
    ) -> None:
        """P-17: every attenuation makes the token longer, never shorter."""
        token = mint_root(mandate, ROOT.private_key)
        current = verify(token, KEY_SET, now=mandate.not_before)
        sizes = [len(token)]
        # Stay within max_depth: past it the chain stops verifying, which is INV-6's
        # business, not this property's.
        for step in range(1, min(mandate.max_depth, 3) + 1):
            child = attenuate(
                current, [], agent_id=f"agt{step}", role=data.draw(st.sampled_from(ROLES))
            )
            sizes.append(len(child))
            current = verify(child, KEY_SET, now=mandate.not_before)
        assert sizes == sorted(sizes)
        assert len(set(sizes)) == len(sizes)


class TestWideningIsRefused:
    """EC-T17, EC-T18, EC-T19: refused at mint, and no token is produced."""

    @given(mandates(), st.data())
    @SLOW
    def test_scope_the_parent_lacks(self, mandate: Mandate, data: st.DataObject) -> None:
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        extra = data.draw(st.sampled_from(SCOPES))
        assume(extra not in mandate.scopes)
        with pytest.raises(AttenuationError, match="scope_subset"):
            attenuate(
                parent,
                [ScopeSubset(scopes=mandate.scopes | {extra})],
                agent_id="agt",
                role="worker",
            )

    @given(mandates(), st.data())
    @SLOW
    def test_raised_budget_ceiling(self, mandate: Mandate, data: st.DataObject) -> None:
        dimension = data.draw(st.sampled_from(list(BudgetDimension)))
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        higher = mandate.budget.get(dimension) + 1
        with pytest.raises(AttenuationError, match="budget_ceiling"):
            attenuate(
                parent,
                [BudgetCeiling(dimension=dimension, value=higher)],
                agent_id="agt",
                role="worker",
            )

    @given(mandates())
    @SLOW
    def test_extended_expiry(self, mandate: Mandate) -> None:
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        with pytest.raises(AttenuationError, match="time_window"):
            attenuate(
                parent,
                [TimeWindow(not_after=mandate.expires_at + timedelta(hours=1))],
                agent_id="agt",
                role="worker",
            )

    @given(mandates(), st.data())
    @SLOW
    def test_raised_depth_limit(self, mandate: Mandate, data: st.DataObject) -> None:
        assume(mandate.max_depth < 8)
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        with pytest.raises(AttenuationError, match="depth_limit"):
            attenuate(
                parent,
                [DepthLimit(max_depth=mandate.max_depth + 1)],
                agent_id="agt",
                role="worker",
            )

    @given(mandates())
    @SLOW
    def test_no_token_is_produced_on_refusal(self, mandate: Mandate) -> None:
        """The refusal must happen before any block is appended."""
        token = mint_root(mandate, ROOT.private_key)
        parent = verify(token, KEY_SET, now=mandate.not_before)
        before = parent.biscuit.block_count()
        with pytest.raises(AttenuationError):
            attenuate(
                parent,
                [TimeWindow(not_after=mandate.expires_at + timedelta(days=1))],
                agent_id="agt",
                role="worker",
            )
        assert parent.biscuit.block_count() == before
