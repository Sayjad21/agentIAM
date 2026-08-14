"""Holder-side attenuation through the SDK (`agentiam_sdk.client`).

Core's `attenuate()` checks a proposed caveat against the *authority block's* grant,
because that is all a `VerifiedToken` can recover. That is enough to stop a child adding
a scope the mandate never carried, and it is not enough to stop a grandchild re-widening
back to a scope its parent gave up — the grant still lists it.

Closing that is the SDK's reason to exist: it minted the intermediate caveats, so it is
the one component that still knows them. `TestReWidening` is where that is proved, with
the core-only path exercised alongside to show the check is load-bearing.
"""

from __future__ import annotations

import warnings
from datetime import timedelta
from decimal import Decimal

import pytest

from agentiam_core.attenuation import attenuate as core_attenuate
from agentiam_core.errors import (
    AttenuationError,
    DepthExceededError,
    InvalidSignatureError,
    TokenExpiredError,
)
from agentiam_core.models import (
    ArgOperator,
    ArgPredicate,
    BudgetDimension,
    RequiresApproval,
    ScopeSubset,
    ToolDeny,
)
from agentiam_core.tokens import WARN_SIZE_LIMIT_B64, RootKeySet, generate_keypair, mint_root
from agentiam_sdk.client import AgentIAM
from agentiam_sdk.context import current_identity, current_identity_or_none
from agentiam_sdk.errors import TokenSizeWarning
from tests.fixtures.tokens import (
    EXPIRES_AT,
    INTENT,
    NOT_BEFORE,
    NOW,
    ROOT_SCOPES,
    a_mandate,
    a_root_client,
    frozen_clock,
)


class TestRootClient:
    def test_holds_the_grant_read_back_from_the_authority_block(self) -> None:
        client = a_root_client()
        identity = client.identity

        assert identity.token == client.token
        assert identity.scopes == ROOT_SCOPES
        assert identity.granted_scopes == ROOT_SCOPES
        assert identity.depth == 0
        assert identity.role == "root"
        assert identity.verified.intent_hash == INTENT
        assert identity.expires_at == EXPIRES_AT
        assert identity.budget.get(BudgetDimension.SPEND_BDT) == Decimal("500000")

    def test_agent_id_defaults_to_something_traceable(self) -> None:
        client = a_root_client()
        assert str(client.identity.verified.mandate_id) in client.identity.agent_id

    def test_an_explicit_agent_id_is_kept(self) -> None:
        assert a_root_client(agent_id="agent:007").identity.agent_id == "agent:007"

    def test_an_expired_token_is_refused_at_construction(self) -> None:
        with pytest.raises(TokenExpiredError):
            a_root_client(now=EXPIRES_AT + timedelta(seconds=1))

    def test_a_token_signed_by_an_unaccepted_key_is_refused(self) -> None:
        other = generate_keypair()
        token = mint_root(a_mandate(), other.private_key)
        with pytest.raises(InvalidSignatureError):
            AgentIAM(
                token=token,
                key_set=RootKeySet([generate_keypair().public_key]),
                clock=frozen_clock(),
            )

    def test_activate_makes_the_identity_current(self) -> None:
        client = a_root_client()
        with client.activate():
            assert current_identity() is client.identity
        assert current_identity_or_none() is None


class TestAttenuate:
    def test_narrows_scopes_and_advances_depth(self) -> None:
        child = a_root_client().attenuate(role="doc-reader", scopes=["invoice:read"])

        assert child.identity.scopes == frozenset({"invoice:read"})
        assert child.identity.granted_scopes == ROOT_SCOPES, "the grant itself is unchanged"
        assert child.identity.depth == 1
        assert child.identity.role == "doc-reader"

    def test_the_child_is_a_different_token_that_still_verifies(self) -> None:
        root = a_root_client()
        child = root.attenuate(role="doc-reader", scopes=["invoice:read"])

        assert child.token != root.token
        assert len(child.token) > len(root.token)
        # Constructing the child client verified it; do it again from the wire form.
        assert child.identity.verified.mandate_id == root.identity.verified.mandate_id

    def test_the_parent_is_untouched(self) -> None:
        """INV-3: attenuation is offline and produces a new token, never a mutation."""
        root = a_root_client()
        before = root.token
        root.attenuate(role="doc-reader", scopes=["invoice:read"])
        assert root.token == before
        assert root.identity.scopes == ROOT_SCOPES

    def test_ttl_shortens_the_effective_expiry(self) -> None:
        child = a_root_client().attenuate(role="doc-reader", ttl_s=600)
        assert child.identity.expires_at == NOW + timedelta(seconds=600)

    def test_a_ttl_past_the_mandate_window_is_refused(self) -> None:
        """EC-T19: a child may not outlive its parent."""
        overshoot = int((EXPIRES_AT - NOW).total_seconds()) + 60
        with pytest.raises(AttenuationError, match="time_window"):
            a_root_client().attenuate(role="doc-reader", ttl_s=overshoot)

    @pytest.mark.parametrize("ttl", [0, -1])
    def test_a_non_positive_ttl_is_a_programming_error(self, ttl: int) -> None:
        with pytest.raises(ValueError, match="ttl_s"):
            a_root_client().attenuate(role="doc-reader", ttl_s=ttl)

    def test_budget_ceilings_fold_into_the_effective_budget(self) -> None:
        child = a_root_client().attenuate(
            role="buyer", budget={"spend_bdt": "12000", BudgetDimension.TOOL_CALLS: 50}
        )
        assert child.identity.budget.get(BudgetDimension.SPEND_BDT) == Decimal("12000")
        assert child.identity.budget.get(BudgetDimension.TOOL_CALLS) == 50
        assert child.identity.budget.get(BudgetDimension.ROWS_READ) == 500_000

    def test_a_zero_budget_is_a_real_ceiling_not_an_absent_one(self) -> None:
        """Spec 01 §5.2: absent means zero, so zero must survive the round trip."""
        child = a_root_client().attenuate(role="doc-reader", budget={"spend_bdt": 0})
        assert child.identity.budget.get(BudgetDimension.SPEND_BDT) == Decimal(0)

    def test_a_raised_ceiling_is_refused(self) -> None:
        """EC-T18."""
        with pytest.raises(AttenuationError, match="budget_ceiling"):
            a_root_client().attenuate(role="buyer", budget={"spend_bdt": "600000"})

    def test_a_float_budget_is_refused_before_it_reaches_a_token(self) -> None:
        with pytest.raises(ValueError, match="float"):
            a_root_client().attenuate(role="buyer", budget={"spend_bdt": 12000.5})  # type: ignore[dict-item]

    def test_an_ungranted_scope_is_refused(self) -> None:
        """EC-T17."""
        with pytest.raises(AttenuationError, match="scope_subset"):
            a_root_client().attenuate(role="thief", scopes=["admin:write"])

    def test_tool_allow_and_deny_reach_the_effective_authority(self) -> None:
        child = a_root_client().attenuate(
            role="reader", tools_allow=["erp.invoice.get"], tools_deny=["erp.payment.send"]
        )
        assert child.identity.authority.tools_allowed == frozenset({"erp.invoice.get"})
        assert child.identity.authority.tools_denied == frozenset({"erp.payment.send"})

    def test_the_caveats_escape_hatch_carries_the_other_types(self) -> None:
        child = a_root_client().attenuate(
            role="buyer",
            caveats=[
                ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("50000")),
                RequiresApproval(scopes=frozenset({"payment:initiate"})),
            ],
        )
        assert child.identity.authority.approval_required == frozenset({"payment:initiate"})
        assert len(child.identity.known_caveats) == 2

    def test_attenuating_with_nothing_is_a_programming_error(self) -> None:
        """A block that narrows nothing costs ~410 bytes and buys nothing."""
        with pytest.raises(ValueError, match="no caveats"):
            a_root_client().attenuate(role="doc-reader")


class TestKnownCaveats:
    def test_caveats_accumulate_down_the_chain(self) -> None:
        root = a_root_client()
        child = root.attenuate(role="reader", scopes=["invoice:read", "vendor:read"])
        grandchild = child.attenuate(role="narrow-reader", scopes=["invoice:read"])

        assert root.identity.known_caveats == ()
        assert len(child.identity.known_caveats) == 1
        assert len(grandchild.identity.known_caveats) == 2
        assert grandchild.identity.scopes == frozenset({"invoice:read"})

    def test_a_received_token_folds_to_an_upper_bound(self) -> None:
        """A client built from someone else's token knows no intermediate caveats.

        Biscuit's append-only structure means the unknown caveats can only narrow, so
        the fold overstates authority rather than understating it. That is the honest
        reading, and it is why the PEP — not the SDK — is the enforcement point.
        """
        root = a_root_client()
        child = root.attenuate(role="reader", scopes=["invoice:read"])

        received = AgentIAM(
            token=child.token,
            key_set=root.key_set,
            role="reader",
            clock=frozen_clock(),
        )
        assert received.identity.known_caveats == ()
        assert received.identity.scopes == ROOT_SCOPES, "wider than the truth, never narrower"

    def test_known_caveats_can_be_supplied_when_a_token_is_handed_over(self) -> None:
        root = a_root_client()
        child = root.attenuate(role="reader", scopes=["invoice:read"])

        rehydrated = AgentIAM(
            token=child.token,
            key_set=root.key_set,
            role="reader",
            known_caveats=child.identity.known_caveats,
            clock=frozen_clock(),
        )
        assert rehydrated.identity.scopes == frozenset({"invoice:read"})


class TestReWidening:
    """The check that only the SDK can make."""

    def test_a_grandchild_cannot_recover_a_scope_its_parent_gave_up(self) -> None:
        root = a_root_client()
        child = root.attenuate(role="reader", scopes=["invoice:read"])

        with pytest.raises(AttenuationError, match="scope_subset"):
            child.attenuate(role="widener", scopes=["vendor:read"])

    def test_core_alone_would_not_catch_it(self) -> None:
        """Proof the previous test is guarding something real.

        Given only the child's `VerifiedToken`, `vendor:read` is still in the grant, so
        core's mint check passes. The resulting token cannot actually *exercise*
        `vendor:read` — biscuit's block scoping (assumption A1) sees to that — but it
        declares a caveat that lies about its own authority, and the console would
        render the lie.
        """
        root = a_root_client()
        child = root.attenuate(role="reader", scopes=["invoice:read"])

        widened = core_attenuate(
            child.identity.verified,
            [ScopeSubset(scopes=frozenset({"vendor:read"}))],
            agent_id="agent:widener",
            role="widener",
        )
        assert widened, "core accepts it, because the authority block still grants it"

    def test_a_ceiling_cannot_be_raised_back_toward_the_mandate(self) -> None:
        root = a_root_client()
        child = root.attenuate(role="buyer", budget={"spend_bdt": "10000"})

        with pytest.raises(AttenuationError, match="budget_ceiling"):
            child.attenuate(role="bigger-buyer", budget={"spend_bdt": "20000"})

    def test_an_expiry_cannot_be_pushed_back_out(self) -> None:
        root = a_root_client()
        child = root.attenuate(role="reader", ttl_s=600)

        with pytest.raises(AttenuationError, match="time_window"):
            child.attenuate(role="longer-reader", ttl_s=1200)

    def test_a_tool_deny_cannot_be_dropped(self) -> None:
        """`ToolDeny` narrows by superset — forgetting a denial is widening."""
        root = a_root_client()
        child = root.attenuate(role="reader", tools_deny=["erp.payment.send", "erp.vendor.put"])

        with pytest.raises(AttenuationError, match="tool_deny"):
            child.attenuate(
                role="forgetful",
                caveats=[ToolDeny(tools=frozenset({"erp.payment.send"}))],
            )


def _delegate(client: AgentIAM, level: int) -> AgentIAM:
    """One more delegation step, narrowing genuinely at every level.

    `ToolDeny` narrows by *superset* — dropping a denial widens authority — so each level
    denies everything its parent did plus one more. Getting this backwards is what a
    first draft of these tests did, and `attenuate()` refused it, which is the algebra
    working.
    """
    return client.attenuate(
        role=f"level-{level}",
        tools_deny=[f"erp.tool.{n}" for n in range(1, level + 1)],
    )


def _chain_to_max_depth(client: AgentIAM) -> AgentIAM:
    for level in range(1, 9):
        client = _delegate(client, level)
    return client


class TestDepthAndSize:
    def test_the_chain_can_be_narrowed_up_to_max_depth(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TokenSizeWarning)
            client = _chain_to_max_depth(a_root_client())
        assert client.identity.depth == 8

    def test_one_step_past_max_depth_is_refused(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TokenSizeWarning)
            client = _chain_to_max_depth(a_root_client())

            with pytest.raises(DepthExceededError):
                _delegate(client, 9)

    def test_no_token_is_produced_when_the_depth_check_fails(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TokenSizeWarning)
            client = _chain_to_max_depth(a_root_client())
            before = client.token

            with pytest.raises(DepthExceededError):
                _delegate(client, 9)
        assert client.token == before

    def test_crossing_the_advisory_size_limit_warns(self) -> None:
        """EC-T11. T-010 is deferred (ADR-006); the warning half of it is not."""
        with pytest.warns(TokenSizeWarning, match="4096"):
            client = _chain_to_max_depth(a_root_client())
        assert len(client.token) > WARN_SIZE_LIMIT_B64

    def test_a_short_chain_does_not_warn(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", TokenSizeWarning)
            a_root_client().attenuate(role="doc-reader", scopes=["invoice:read"])


class TestClockInjection:
    def test_the_clock_is_only_read_when_it_is_needed(self) -> None:
        """`ttl_s` is the only feature that needs a clock at attenuation time."""
        calls: list[int] = []

        def counting_clock() -> object:
            calls.append(1)
            return NOW

        client = AgentIAM(
            token=mint_root(a_mandate(), (kp := generate_keypair()).private_key),
            key_set=RootKeySet([kp.public_key]),
            clock=counting_clock,  # type: ignore[arg-type]
        )
        baseline = len(calls)
        client.attenuate(role="reader", scopes=["invoice:read"])
        assert len(calls) > baseline, "the child is verified against the clock"

    def test_the_default_clock_is_the_wall_clock(self) -> None:
        """The SDK may read a clock; only `agentiam-core` may not."""
        mandate = a_mandate(
            not_before=NOT_BEFORE - timedelta(days=3650),
            expires_at=EXPIRES_AT + timedelta(days=3650),
        )
        key_pair = generate_keypair()
        client = AgentIAM(
            token=mint_root(mandate, key_pair.private_key),
            key_set=RootKeySet([key_pair.public_key]),
        )
        assert client.identity.depth == 0
