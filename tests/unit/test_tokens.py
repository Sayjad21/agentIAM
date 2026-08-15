"""Biscuit mint and verify (`agentiam_core.tokens`).

`verify()` is deliberately *not* authorization. It answers: is this token authentic, is it
within its validity window, and is it structurally sound? Deciding whether a particular
call is permitted needs the request context and belongs to the decision pipeline (T-019).

Splitting them is what lets `verify()` return a precise reason code. Biscuit reports only
that authorization failed, never which clause caused it, so distinguishing an expired
token from a not-yet-valid one has to happen here, against facts read back from the
authority block.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from biscuit_auth import AuthorizationError, AuthorizerBuilder, KeyPair

from agentiam_core.errors import (
    DepthExceededError,
    InvalidSignatureError,
    MalformedTokenError,
    ReasonCode,
    TokenError,
    TokenExpiredError,
    TokenNotYetValidError,
    TokenTooLargeError,
    VerificationLimitError,
)
from agentiam_core.models import Budget, BudgetDimension, Mandate
from agentiam_core.tokens import (
    HARD_SIZE_LIMIT_B64,
    MAX_DATALOG_FACTS,
    MAX_DATALOG_ITERATIONS,
    MAX_DATALOG_TIME,
    WARN_SIZE_LIMIT_B64,
    RootKeySet,
    generate_keypair,
    mint_root,
    verify,
)

NOT_BEFORE = datetime(2026, 8, 14, 11, 45, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
MID_WINDOW = datetime(2026, 8, 14, 11, 50, tzinfo=UTC)
INTENT = "0dc20e2931968ffffd73e255ac7f6984d9677d5ac1508af1a94eacd962537b70"


@pytest.fixture(scope="module")
def root_key() -> KeyPair:
    return generate_keypair()


@pytest.fixture(scope="module")
def key_set(root_key: KeyPair) -> RootKeySet:
    return RootKeySet([root_key.public_key])


def a_mandate(**kw: object) -> Mandate:
    base: dict[str, object] = {
        "mandate_id": uuid4(),
        "task_id": uuid4(),
        "principal_id": "kc:9f2c1e40-7a3b-4d21-9c88-1b2e5f0a4d77",
        "intent_hash": INTENT,
        "scopes": frozenset({"invoice:read", "vendor:read", "payment:initiate"}),
        "budget": Budget(
            spend_bdt=Decimal("500000"),
            tool_calls=2000,
            rows_read=500_000,
            external_emails=50,
            wall_clock_s=3600,
        ),
        "max_depth": 8,
        "not_before": NOT_BEFORE,
        "expires_at": EXPIRES_AT,
    }
    return Mandate(**(base | kw))  # type: ignore[arg-type]


class TestRoundTrip:
    def test_mint_then_verify(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        mandate = a_mandate()
        token = mint_root(mandate, root_key.private_key)
        verified = verify(token, key_set, now=MID_WINDOW)

        assert verified.mandate_id == mandate.mandate_id
        assert verified.task_id == mandate.task_id
        assert verified.principal_id == mandate.principal_id
        assert verified.intent_hash == mandate.intent_hash
        assert verified.scopes == mandate.scopes
        assert verified.max_depth == mandate.max_depth
        assert verified.not_before == mandate.not_before
        assert verified.expires_at == mandate.expires_at

    def test_budget_survives_the_round_trip_exactly(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """Scaled-integer encoding must be lossless in both directions (spec 01 §3.1)."""
        budget = Budget(
            spend_bdt=Decimal("123.4567"),
            tool_calls=7,
            rows_read=13,
            external_emails=1,
            wall_clock_s=99,
        )
        token = mint_root(a_mandate(budget=budget), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).budget == budget

    def test_root_token_has_depth_zero(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        token = mint_root(a_mandate(), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).depth == 0

    def test_empty_scope_set_round_trips(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        """EC-T14: a token that grants nothing is still a valid identity."""
        token = mint_root(a_mandate(scopes=frozenset()), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).scopes == frozenset()

    def test_revocation_ids_one_per_block(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        verified = verify(mint_root(a_mandate(), root_key.private_key), key_set, now=MID_WINDOW)
        assert len(verified.revocation_ids) == 1
        assert len(verified.revocation_ids[0]) == 128

    def test_bengali_principal_and_intent_survive(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """EC-T16: non-ASCII must survive the whole chain."""
        mandate = a_mandate(principal_id="kc:ঢাকা-প্যাকেজিং-৪২")
        verified = verify(mint_root(mandate, root_key.private_key), key_set, now=MID_WINDOW)
        assert verified.principal_id == "kc:ঢাকা-প্যাকেজিং-৪২"

    def test_minting_is_not_byte_deterministic(self, root_key: KeyPair) -> None:
        """Biscuit embeds a fresh next-block key, so two mints differ.

        Documented because it is a reasonable thing to assume and it is false: no test
        may assert byte equality between two mints of the same mandate.
        """
        mandate = a_mandate()
        assert mint_root(mandate, root_key.private_key) != mint_root(mandate, root_key.private_key)


class TestValidityWindow:
    @pytest.mark.parametrize(
        ("now", "valid"),
        [
            (NOT_BEFORE - timedelta(seconds=1), False),
            (NOT_BEFORE, True),
            (MID_WINDOW, True),
            (EXPIRES_AT - timedelta(seconds=1), True),
            (EXPIRES_AT, False),
            (EXPIRES_AT + timedelta(seconds=1), False),
        ],
    )
    def test_window_boundaries(
        self, root_key: KeyPair, key_set: RootKeySet, now: datetime, valid: bool
    ) -> None:
        """EC-T06 / EC-T07. `not_before` inclusive, `expires_at` exclusive."""
        token = mint_root(a_mandate(), root_key.private_key)
        if valid:
            assert verify(token, key_set, now=now).depth == 0
        else:
            with pytest.raises((TokenExpiredError, TokenNotYetValidError)):
                verify(token, key_set, now=now)

    def test_expired_reports_token_expired(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        token = mint_root(a_mandate(), root_key.private_key)
        with pytest.raises(TokenExpiredError) as exc:
            verify(token, key_set, now=EXPIRES_AT)
        assert exc.value.reason_code is ReasonCode.TOKEN_EXPIRED

    def test_not_yet_valid_reports_its_own_code(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """The two window failures must be distinguishable, not both 'invalid'."""
        token = mint_root(a_mandate(), root_key.private_key)
        with pytest.raises(TokenNotYetValidError) as exc:
            verify(token, key_set, now=NOT_BEFORE - timedelta(minutes=1))
        assert exc.value.reason_code is ReasonCode.TOKEN_NOT_YET_VALID

    def test_naive_now_is_rejected(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        token = mint_root(a_mandate(), root_key.private_key)
        with pytest.raises(ValueError, match="timezone-aware"):
            verify(token, key_set, now=datetime(2026, 8, 14, 11, 50))

    def test_verification_honours_the_injected_clock(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """The clock is a parameter. Core never reads it."""
        token = mint_root(a_mandate(), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).depth == 0
        with pytest.raises(TokenExpiredError):
            verify(token, key_set, now=MID_WINDOW + timedelta(days=365))


class TestAuthenticity:
    def test_wrong_public_key_fails(self, root_key: KeyPair) -> None:
        """EC-T04."""
        token = mint_root(a_mandate(), root_key.private_key)
        other = RootKeySet([generate_keypair().public_key])
        with pytest.raises(InvalidSignatureError) as exc:
            verify(token, other, now=MID_WINDOW)
        assert exc.value.reason_code is ReasonCode.TOKEN_INVALID_SIGNATURE

    def test_single_flipped_bit_fails(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        """EC-T03."""
        token = mint_root(a_mandate(), root_key.private_key)
        raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        raw[len(raw) // 2] ^= 0x01
        tampered = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
        with pytest.raises(InvalidSignatureError):
            verify(tampered, key_set, now=MID_WINDOW)

    def test_truncated_token_fails_without_crashing(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """EC-T02."""
        token = mint_root(a_mandate(), root_key.private_key)
        with pytest.raises((InvalidSignatureError, MalformedTokenError)):
            verify(token[:-12], key_set, now=MID_WINDOW)

    @pytest.mark.parametrize("bad", ["", "   ", "not-base64!!", "YWJj"])
    def test_garbage_input_is_malformed_not_a_crash(self, key_set: RootKeySet, bad: str) -> None:
        """EC-T01."""
        with pytest.raises((MalformedTokenError, InvalidSignatureError)):
            verify(bad, key_set, now=MID_WINDOW)


class TestKeyRotation:
    """EC-T05: a rotated key is accepted only while it remains in the set."""

    def test_token_from_an_old_key_verifies_while_the_key_is_accepted(
        self, root_key: KeyPair
    ) -> None:
        old = generate_keypair()
        token = mint_root(a_mandate(), old.private_key)
        rotated = RootKeySet([root_key.public_key, old.public_key])
        assert verify(token, rotated, now=MID_WINDOW).depth == 0

    def test_token_from_a_retired_key_fails(self, root_key: KeyPair) -> None:
        old = generate_keypair()
        token = mint_root(a_mandate(), old.private_key)
        with pytest.raises(InvalidSignatureError):
            verify(token, RootKeySet([root_key.public_key]), now=MID_WINDOW)

    def test_key_set_must_not_be_empty(self) -> None:
        """An empty accepted set would silently reject everything."""
        with pytest.raises(ValueError, match="at least one"):
            RootKeySet([])

    def test_current_key_is_the_first(self, root_key: KeyPair) -> None:
        old = generate_keypair()
        key_set = RootKeySet([root_key.public_key, old.public_key])
        # `.public_key` hands back a fresh object each access, so compare by value.
        assert str(key_set.current) == str(root_key.public_key)


class TestSizeGuard:
    def test_a_normal_token_is_well_under_the_warning(self, root_key: KeyPair) -> None:
        """Measured in spec 01 §9.1: a realistic root token is ~1,400 base64 chars."""
        token = mint_root(a_mandate(), root_key.private_key)
        assert len(token) < WARN_SIZE_LIMIT_B64

    def test_size_is_reported(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        token = mint_root(a_mandate(), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).size_b64 == len(token)

    def test_oversized_token_is_rejected_before_parsing(self, key_set: RootKeySet) -> None:
        """A-08: an oversized token must not cost parse time to reject."""
        with pytest.raises(TokenTooLargeError) as exc:
            verify("A" * (HARD_SIZE_LIMIT_B64 + 1), key_set, now=MID_WINDOW)
        assert exc.value.reason_code is ReasonCode.TOKEN_TOO_LARGE

    def test_mint_rejects_a_mandate_that_would_overflow(self, root_key: KeyPair) -> None:
        """Scopes are unbounded in principle; the size limit is not."""
        huge = frozenset(f"resource{i}:read" for i in range(2000))
        with pytest.raises(TokenTooLargeError):
            mint_root(a_mandate(scopes=huge), root_key.private_key)

    def test_warning_flag_is_set_past_the_soft_limit(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """EC-T11: the warn path must be reachable, so it must be tested."""
        scopes = frozenset(f"resource{i}:read" for i in range(120))
        token = mint_root(a_mandate(scopes=scopes), root_key.private_key)
        assert len(token) > WARN_SIZE_LIMIT_B64
        assert verify(token, key_set, now=MID_WINDOW).size_warning is True

    def test_no_warning_flag_below_the_soft_limit(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        token = mint_root(a_mandate(), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).size_warning is False


class TestDepth:
    def test_depth_within_max_is_accepted(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        """EC-T09."""
        token = mint_root(a_mandate(max_depth=1), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).depth == 0

    def test_chain_deeper_than_max_depth_is_rejected(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """EC-T10 / INV-6. Depth comes from the block count, never a block fact."""
        from biscuit_auth import Biscuit, BlockBuilder

        token = mint_root(a_mandate(max_depth=1), root_key.private_key)
        chain = Biscuit.from_base64(token, key_set.current)
        for i in range(3):
            chain = chain.append(BlockBuilder(f'agent("a{i}");'))
        with pytest.raises(DepthExceededError) as exc:
            verify(chain.to_base64(), key_set, now=MID_WINDOW)
        assert exc.value.reason_code is ReasonCode.DEPTH_EXCEEDED


class TestMalformedAuthorityBlock:
    """A token can carry a valid signature and still not be ours.

    Anything holding the root private key can mint an arbitrary biscuit. Verification must
    reject a structurally wrong authority block with `MALFORMED_REQUEST` rather than raise
    an unhandled `KeyError` or return a half-populated `VerifiedToken` — which is what an
    attacker with a leaked key, or simply a version skew, would produce.
    """

    @staticmethod
    def _mint_raw(source: str, root_key: KeyPair) -> str:
        from biscuit_auth import BiscuitBuilder

        return BiscuitBuilder(source).build(root_key.private_key).to_base64()

    WINDOW = "not_before(2026-08-14T11:45:00Z);\nexpires_at(2026-08-14T12:00:00Z);\nmax_depth(8);\n"
    IDENTITY = (
        'mandate("9ac25bcb-b04f-428b-bfd3-be01317318da");\n'
        'task("f155ff50-28a4-4c98-939f-55a1085b7db6");\n'
        'principal("kc:x");\n'
        f'intent("{INTENT}");\n'
    )

    @pytest.mark.parametrize(
        "omit", ["mandate", "task", "principal", "intent", "max_depth", "not_before", "expires_at"]
    )
    def test_missing_mandatory_fact_is_malformed(
        self, root_key: KeyPair, key_set: RootKeySet, omit: str
    ) -> None:
        source = self.IDENTITY + self.WINDOW
        source = "\n".join(line for line in source.splitlines() if not line.startswith(f"{omit}("))
        with pytest.raises(MalformedTokenError, match=omit):
            verify(self._mint_raw(source, root_key), key_set, now=MID_WINDOW)

    def test_non_uuid_mandate_is_malformed(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        source = (
            self.IDENTITY.replace(
                '"9ac25bcb-b04f-428b-bfd3-be01317318da"', '"definitely-not-a-uuid"'
            )
            + self.WINDOW
        )
        with pytest.raises(MalformedTokenError, match="UUID"):
            verify(self._mint_raw(source, root_key), key_set, now=MID_WINDOW)

    def test_window_facts_that_are_not_dates_are_malformed(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        source = self.IDENTITY + 'not_before("soon");\nexpires_at("later");\nmax_depth(8);\n'
        with pytest.raises(MalformedTokenError, match="not dates"):
            verify(self._mint_raw(source, root_key), key_set, now=MID_WINDOW)

    def test_non_integer_max_depth_is_malformed(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        source = self.IDENTITY + (
            "not_before(2026-08-14T11:45:00Z);\n"
            "expires_at(2026-08-14T12:00:00Z);\n"
            'max_depth("eight");\n'
        )
        with pytest.raises(MalformedTokenError, match="max_depth"):
            verify(self._mint_raw(source, root_key), key_set, now=MID_WINDOW)


class TestMintedStructure:
    def test_budgets_are_scaled_integers(self, root_key: KeyPair, key_set: RootKeySet) -> None:
        """Spec 01 §3.1: 500,000 taka encodes as 5_000_000_000."""
        token = mint_root(a_mandate(), root_key.private_key)
        verified = verify(token, key_set, now=MID_WINDOW)
        assert verified.scaled_budget[BudgetDimension.SPEND_BDT] == 5_000_000_000
        assert verified.scaled_budget[BudgetDimension.TOOL_CALLS] == 20_000_000

    def test_every_dimension_is_present_in_the_token(
        self, root_key: KeyPair, key_set: RootKeySet
    ) -> None:
        """A missing dimension means a ceiling of zero, so it must never be omitted."""
        token = mint_root(a_mandate(budget=Budget()), root_key.private_key)
        verified = verify(token, key_set, now=MID_WINDOW)
        assert set(verified.scaled_budget) == set(BudgetDimension)


class TestDatalogExecutionLimits:
    """TM-25 / ADR-021 — the library's default timeout is shorter than the work it guards.

    `biscuit-python`'s authorizer defaults to a **wall-clock** budget of one millisecond, so
    a query that takes microseconds fails whenever the process loses the CPU for long enough.
    Under a parallel test run that surfaced as a bogus INV-1 violation (`STATUS.md` gap 13).
    In production the same mechanism denies a legitimate request for want of scheduling — and
    one millisecond is the whole of NFR-1's decision budget.
    """

    def test_the_library_default_is_one_millisecond_of_wall_clock(self) -> None:
        """Pin the measured defaults so a `biscuit-python` upgrade that moves them shows up here."""
        default = AuthorizerBuilder("allow if true;").limits()  # type: ignore[attr-defined]

        assert default.max_time == timedelta(milliseconds=1)
        assert default.max_facts == 1000
        assert default.max_iterations == 100

    def test_this_module_raises_every_default(self) -> None:
        default = AuthorizerBuilder("allow if true;").limits()  # type: ignore[attr-defined]

        assert MAX_DATALOG_TIME > default.max_time
        assert MAX_DATALOG_FACTS > default.max_facts
        assert MAX_DATALOG_ITERATIONS > default.max_iterations

    def test_the_time_limit_is_load_bearing_in_verify(
        self, root_key: KeyPair, key_set: RootKeySet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the constant to zero and `verify()` must fail — otherwise it is not applied.

        A constant that no code path reads would satisfy the two assertions above and protect
        nothing. `timedelta(0)` exceeds deterministically, so this needs no timing luck.
        """
        token = mint_root(a_mandate(), root_key.private_key)
        assert verify(token, key_set, now=MID_WINDOW).max_depth == 8

        monkeypatch.setattr("agentiam_core.tokens.MAX_DATALOG_TIME", timedelta(0))

        with pytest.raises(VerificationLimitError, match="exceeded its limits") as caught:
            verify(token, key_set, now=MID_WINDOW)
        assert caught.value.reason_code is ReasonCode.VERIFICATION_LIMIT_EXCEEDED

    def test_a_limit_failure_carries_a_reason_code_not_a_library_exception(
        self, root_key: KeyPair, key_set: RootKeySet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TM-25's residual: a caller catching TokenError must still get a reason code.

        Before this, an exceeded limit escaped as a raw `biscuit_auth.AuthorizationError`,
        which no caller catches and which carries no code — so the PEP would have turned a
        transient CPU stall into an unexplained 500 rather than a named deny.
        """
        token = mint_root(a_mandate(), root_key.private_key)
        monkeypatch.setattr("agentiam_core.tokens.MAX_DATALOG_TIME", timedelta(0))

        with pytest.raises(TokenError) as caught:
            verify(token, key_set, now=MID_WINDOW)
        assert not isinstance(caught.value, AuthorizationError)
        assert caught.value.reason_code is ReasonCode.VERIFICATION_LIMIT_EXCEEDED
