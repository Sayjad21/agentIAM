"""The Datalog→caveat recognizer — `agentiam_core.datalog`, TODO item 2, STATUS gap 2.

Two halves, and the split is deliberate.

**Round-trip tests go through a real biscuit.** `parse_block_source(to_datalog(c))` would
test this module against `caveats.py`'s output, and that is not the input it receives: the
text comes from `Biscuit.block_source(i)`, which normalizes whitespace, reorders facts ahead
of checks, and — measured here — renders string values back *unescaped*. A recognizer proved
against the compiler's output and not the renderer's would be proved against the wrong
grammar. So every round trip mints, attenuates, verifies and reads back.

**Hostile-input tests build the block text directly**, because the point of them is text a
compliant minter would never produce. `attenuate()` runs `validate_label` and would refuse
most of them at the door, which is the mitigation working — and precisely why the read side
needs its own tests: a token minted by someone else ran no such check.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from biscuit_auth import BlockBuilder

from agentiam_core.attenuation import attenuate, effective_bound, grant_caveats
from agentiam_core.caveats import _NEGATED, to_datalog
from agentiam_core.datalog import (
    _DENEGATED,
    MAX_STATEMENTS_PER_BLOCK,
    BlockIdentity,
    effective_authority,
    parse_block_source,
    parse_token,
    token_caveats,
    token_identity,
)
from agentiam_core.models import (
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
from agentiam_core.tokens import RootKeySet, VerifiedToken, generate_keypair, mint_root, verify

_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
_INTENT = "a" * 64
_KEYS = generate_keypair()
_KEY_SET = RootKeySet((_KEYS.public_key,))


def _root() -> VerifiedToken:
    mandate = Mandate(
        mandate_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        principal_id="kc:alice",
        intent_hash=_INTENT,
        scopes=frozenset({"invoice:read", "vendor:read", "payment:initiate"}),
        budget=Budget(spend_bdt=Decimal("500000")),
        max_depth=4,
        not_before=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )
    return verify(mint_root(mandate, _KEYS.private_key), _KEY_SET, now=_NOW)


def _child(*caveats: Caveat, agent_id: str = "agt-child", role: str = "worker") -> VerifiedToken:
    token = attenuate(_root(), list(caveats), agent_id=agent_id, role=role)
    return verify(token, _KEY_SET, now=_NOW)


def _clauses(caveats: object) -> list[str]:
    """Every caveat's Datalog, one clause per entry, sorted.

    Compared clause-wise rather than object-wise on purpose: a two-sided `TimeWindow`
    compiles to two clauses and comes back as two one-sided windows, which is the same
    restriction and the same split `attenuation.atoms()` already makes. Comparing objects
    would fail on a difference that does not exist, and comparing compiled text is the
    stronger claim anyway — it is what biscuit would enforce.
    """
    assert isinstance(caveats, (list, tuple))
    return sorted(line for c in caveats for line in to_datalog(c).splitlines())


# ---------------------------------------------------------------------------
# Round trips through a real token
# ---------------------------------------------------------------------------

_EVERY_KIND: tuple[Caveat, ...] = (
    ScopeSubset(scopes=frozenset({"invoice:read", "vendor:read"})),
    BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("1000.5000")),
    TimeWindow(not_before=_NOW, not_after=_NOW + timedelta(minutes=30)),
    ToolAllow(tools=frozenset({"invoice_api"})),
    ToolDeny(tools=frozenset({"payment_api"})),
    ArgPredicate(path="payment.amount", op=ArgOperator.LE, value=Decimal("500")),
    ArgPredicate(path="payment.count", op=ArgOperator.GT, value=Decimal("2")),
    ArgPredicate(path="a.b", op=ArgOperator.EQ, value="literal"),
    ArgPredicate(path="c.d", op=ArgOperator.NE, value="other"),
    ArgPredicate(
        path="email.domain", op=ArgOperator.IN, value=frozenset({"example.com", "corp.example"})
    ),
    ArgPredicate(path="x.y", op=ArgOperator.NOT_IN, value=frozenset({"blocked"})),
    DepthLimit(max_depth=3),
    IntentBound(intent_hash=_INTENT),
    RequiresApproval(scopes=frozenset({"payment:initiate"})),
)


def test_every_caveat_kind_survives_the_round_trip() -> None:
    """All nine kinds, through mint → attenuate → render → recognize, clause for clause."""
    recovered = token_caveats(_child(*_EVERY_KIND))
    assert _clauses(recovered) == _clauses(list(_EVERY_KIND))


@pytest.mark.parametrize("caveat", _EVERY_KIND, ids=lambda c: c.kind.value)
def test_each_kind_alone_round_trips(caveat: Caveat) -> None:
    """One at a time, so a failure names the kind rather than the whole set."""
    assert _clauses(token_caveats(_child(caveat))) == _clauses([caveat])


def test_nothing_in_a_minted_chain_is_unrecognized() -> None:
    """The authority block and the attenuation block both parse completely.

    This is the assertion that would fail first if biscuit changed its renderer, which is
    the change most likely to break this module silently.
    """
    for block in parse_token(_child(*_EVERY_KIND)):
        assert block.unrecognized == (), f"block {block.index}: {block.unrecognized}"
        assert block.complete


def test_the_authority_block_yields_its_checks_but_not_its_grant() -> None:
    """Block 0's budget/depth/window/intent checks are caveats; its grant facts are not.

    `check if operation($op), scope($op);` and `check if request_intent($h), intent($h);`
    name the grant rather than restricting the request, and the grant is already on
    `VerifiedToken`. They are recognized so they do not read as unknown restrictions, and
    discarded so they do not read as caveats.
    """
    authority = parse_token(_root())[0]

    kinds = sorted(c.kind.value for c in authority.caveats)
    assert kinds == [
        "budget_ceiling",
        "budget_ceiling",
        "budget_ceiling",
        "budget_ceiling",
        "budget_ceiling",
        "depth_limit",
        "time_window",
        "time_window",
    ]
    assert not any(isinstance(c, ScopeSubset) for c in authority.caveats)
    assert not any(isinstance(c, IntentBound) for c in authority.caveats)
    assert authority.complete


def test_token_caveats_skips_the_authority_block() -> None:
    """A root token has narrowed nothing, so it contributes no caveats.

    The ceilings in its own block are the *grant*, and `grant_caveats()` renders those.
    Counting both would report the mandate's ceiling as an attenuation nobody applied.
    """
    assert token_caveats(_root()) == ()


def test_a_two_level_chain_returns_both_blocks_caveats_root_first() -> None:
    """Order matters: spec 02 §6 fixes attribution as the first failing caveat in order."""
    first = _child(ScopeSubset(scopes=frozenset({"invoice:read", "vendor:read"})))
    grandchild = verify(
        attenuate(
            first,
            [ScopeSubset(scopes=frozenset({"invoice:read"}))],
            agent_id="agt-grandchild",
            role="reader",
        ),
        _KEY_SET,
        now=_NOW,
    )

    recovered = token_caveats(grandchild)
    assert [sorted(c.scopes) for c in recovered if isinstance(c, ScopeSubset)] == [
        ["invoice:read", "vendor:read"],
        ["invoice:read"],
    ]


def test_scaled_values_come_back_exact() -> None:
    """Money survives the 10⁴ scaling in both directions, as `Decimal` (rule 4)."""
    caveat = BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("1234.5678"))
    (recovered,) = [c for c in token_caveats(_child(caveat)) if isinstance(c, BudgetCeiling)]
    assert recovered.value == Decimal("1234.5678")
    assert isinstance(recovered.value, Decimal)


def test_an_empty_scope_set_round_trips_as_an_empty_set() -> None:
    """EC-T14: a token that may act on nothing is legal, and must not read as unrestricted.

    `[]` is the one set literal where "could not parse" and "parsed as empty" mean opposite
    things — the first would drop the restriction and show a live token.
    """
    (recovered,) = [
        c
        for c in token_caveats(_child(ScopeSubset(scopes=frozenset())))
        if isinstance(c, ScopeSubset)
    ]
    assert recovered.scopes == frozenset()
    assert effective_authority(_child(ScopeSubset(scopes=frozenset()))).bound.is_dead


def test_denegated_is_the_exact_inverse_of_the_compiler_s_negation() -> None:
    """The two tables must stay each other's inverse, or an operator reads back flipped.

    Asserted rather than transcribed: `<=` compiling to `>` and `>` reading back as
    something other than `<=` would invert a ceiling into a floor, silently.
    """
    assert {rendered: op for op, rendered in _NEGATED.items()} == _DENEGATED


def test_the_effective_bound_of_a_received_token_is_the_true_one() -> None:
    """Item 2's acceptance criterion, and the thing gap 2 said could not be done.

    The chain is built here the way a third party would build it and then handed over as
    nothing but base64 — no caveat list travels alongside it. What comes back is the fold of
    the grant and every block's caveats, which is narrower than the grant on every axis the
    chain narrowed.
    """
    depth_one = attenuate(
        _root(),
        [
            ScopeSubset(scopes=frozenset({"invoice:read", "vendor:read"})),
            BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("50000")),
        ],
        agent_id="agt-negotiator",
        role="worker",
    )
    wire = attenuate(
        verify(depth_one, _KEY_SET, now=_NOW),
        [
            ScopeSubset(scopes=frozenset({"invoice:read"})),
            BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=Decimal("2500")),
            TimeWindow(not_before=None, not_after=_NOW + timedelta(minutes=5)),
        ],
        agent_id="agt-subcontractor",
        role="reader",
    )

    # Everything below is derived from the wire format alone.
    received = verify(wire, _KEY_SET, now=_NOW)
    authority = effective_authority(received)

    assert authority.complete
    assert authority.bound.scopes == frozenset({"invoice:read"})
    assert authority.bound.budget[BudgetDimension.SPEND_BDT] == Decimal("2500")
    assert authority.bound.not_after == _NOW + timedelta(minutes=5)
    # The grant, which is all a `VerifiedToken` exposes on its own, is wider on all three.
    assert received.scopes == frozenset({"invoice:read", "vendor:read", "payment:initiate"})
    assert received.budget.get(BudgetDimension.SPEND_BDT) == Decimal("500000")
    assert received.expires_at == _NOW + timedelta(hours=1)


def test_the_effective_bound_matches_folding_the_caveats_by_hand() -> None:
    """`effective_authority()` is `effective_bound()` over grant plus parsed caveats.

    Stated as a test so the relationship cannot drift: a second, subtly different fold is
    exactly how a console ends up disagreeing with a decision.
    """
    token = _child(*_EVERY_KIND)
    by_hand = effective_bound([*grant_caveats(token), *token_caveats(token)])
    assert effective_authority(token).bound == by_hand


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_terminal_block_names_the_agent_and_its_role() -> None:
    """Item 4's foundation: `agt-depth-N` exists only because nothing read these."""
    identity = token_identity(_child(agent_id="agt-payer", role="payer"))
    assert identity == BlockIdentity(agent_id="agt-payer", role="payer", declared_depth=1)


def test_a_root_token_declares_no_identity() -> None:
    """No attenuation block, so no `agent()` fact — `None`, not a made-up default.

    A caller that cannot tell "not stated" from "stated as something" cannot fall back
    honestly, which is the whole reason these fields are optional.
    """
    assert token_identity(_root()) == BlockIdentity()


def test_identity_comes_from_the_terminal_block_not_an_ancestor() -> None:
    """Depth 2's identity is depth 2's, not the parent's it inherited authority from."""
    parent = _child(agent_id="agt-parent", role="worker")
    grandchild = verify(
        attenuate(parent, [], agent_id="agt-grandchild", role="reader"), _KEY_SET, now=_NOW
    )
    assert token_identity(grandchild).agent_id == "agt-grandchild"
    assert token_identity(grandchild).declared_depth == 2


def test_a_non_ascii_role_survives_intact() -> None:
    """Spec 01 §6.1: non-ASCII is unrestricted — a Bengali role name is valid.

    A recognizer that quietly ASCII-folded or refused one would make the constraint stricter
    than the spec, on the read side where nobody would look for it.
    """
    identity = token_identity(_child(agent_id="agt-১", role="প্রধান"))
    assert identity.role == "প্রধান"
    assert identity.agent_id == "agt-১"


# ---------------------------------------------------------------------------
# Untrusted input — TM-24
# ---------------------------------------------------------------------------


def test_a_role_that_breaks_out_of_its_literal_refuses_the_field() -> None:
    """TM-24's exact payload, as it renders: two `role` facts and a forged one.

    Measured against the installed library — `block_source()` escapes nothing, so
    ``role("x"); admin(true); role("y");`` is what a crafted role becomes. There is no sound
    way to pick between the two roles, so neither is returned, and `admin(true)` is reported
    as the unknown statement it is rather than ignored.
    """
    block = parse_block_source('agent("ok");\nrole("x"); admin(true); role("y");\n', index=1)

    assert block.identity.role is None
    assert "role" in block.duplicate_facts
    assert block.unrecognized == ("admin(true)",)
    assert not block.complete
    # The uncontested fact is still readable — refusing the whole block would throw away
    # good information because one field was attacked.
    assert block.identity.agent_id == "ok"


def test_a_duplicated_agent_fact_refuses_the_agent_id() -> None:
    """Same rule, the other identity field. Ambiguity is never resolved by picking one."""
    block = parse_block_source('agent("real");\nagent("forged");\nrole("worker");\n', index=1)
    assert block.identity.agent_id is None
    assert block.duplicate_facts == ("agent",)
    assert block.identity.role == "worker"


@pytest.mark.parametrize(
    ("label", "why"),
    [
        ("a" * 200, "over the 128-character limit"),
        ("has\x07bell", "a C0 control"),
        ("has\u202ebidi", "a bidi override"),
    ],
)
def test_a_label_that_would_not_pass_validation_is_refused_on_the_way_out(
    label: str, why: str
) -> None:
    """TM-24's mitigation, applied where a label *leaves* a token as well as where it enters.

    `attenuate()` refuses these at mint, so our own tokens never carry them. A token minted
    by someone else ran no such check, and handing the value to a console to render is the
    thing the mitigation exists to prevent.
    """
    block = parse_block_source(f'agent("{label}");\nrole("worker");\n', index=1)
    assert block.identity.agent_id is None, why
    assert "agent" in block.duplicate_facts


def test_an_unknown_statement_marks_the_fold_incomplete_rather_than_being_dropped() -> None:
    """The direction that matters: a restriction this build cannot read must not vanish.

    Dropping it would show a *wider* bound than the token actually permits — the one error a
    display path must never make. Reporting it leaves the console able to say so.
    """
    block = parse_block_source(
        'agent("a");\nrole("r");\ncheck if quantum_flux($q), $q <= 3;\n', index=1
    )
    assert block.caveats == ()
    assert block.unrecognized == ("check if quantum_flux($q), $q <= 3",)
    assert not block.complete


def test_an_injected_restriction_cannot_widen_the_bound() -> None:
    """Injection can only narrow, and the fold is what guarantees it.

    A smuggled clause is intersected like any other, so a higher ceiling loses to the
    minimum and a wider scope set loses to the intersection. The worst a crafted token can
    do is restrict its own holder.
    """
    honest = parse_block_source(
        'check if requested("spend_bdt", $v), $v <= 10000;\n'
        'check if operation($op), ["invoice:read"].contains($op);\n'
    )
    attacked = parse_block_source(
        'check if requested("spend_bdt", $v), $v <= 10000;\n'
        'check if operation($op), ["invoice:read"].contains($op);\n'
        'check if requested("spend_bdt", $v), $v <= 999999999999;\n'
        'check if operation($op), ["invoice:read", "payment:initiate"].contains($op);\n'
    )

    honest_bound = effective_bound(list(honest.caveats))
    attacked_bound = effective_bound(list(attacked.caveats))
    assert attacked_bound.budget == honest_bound.budget
    assert attacked_bound.scopes == honest_bound.scopes == frozenset({"invoice:read"})


def test_a_semicolon_inside_a_string_does_not_split_the_statement() -> None:
    """Measured: the renderer emits `;` inside a string literally, so splitting must not.

    A naive split on `;` would cut `role("senior;lead")` in half and lose a legitimate role
    — a false negative on a token nobody attacked.
    """
    block = parse_block_source('agent("agt-1");\nrole("senior;lead");\n', index=1)
    assert block.identity.role == "senior;lead"
    assert block.complete


def test_a_newline_inside_a_string_does_not_split_the_statement() -> None:
    r"""Also measured: `\n` in a Datalog literal renders as a real newline.

    So statements cannot be recovered line by line either. `validate_label` keeps our own
    tokens clear of this; a third party's need not be.
    """
    block = parse_block_source('agent("agt-1");\nrole("two\nlines");\n', index=1)
    # The label is refused as a control character, but the *statement* was read as one unit —
    # the alternative is two fragments, neither of which parses, and a lost `agent` fact.
    assert block.identity.agent_id == "agt-1"
    assert "role" in block.duplicate_facts


def test_a_block_of_junk_produces_no_caveats_and_no_crash() -> None:
    """Nothing is evaluated, so unparseable text is inert rather than dangerous."""
    block = parse_block_source("!!! not datalog at all ((( ;;; \x00\n", index=1)
    assert block.caveats == ()
    assert block.identity == BlockIdentity()


def test_statement_count_is_bounded_on_a_hostile_block() -> None:
    """Work stays bounded by a constant, not by how much the minter chose to write."""
    source = 'agent("a");\n' + "noise(1);\n" * (MAX_STATEMENTS_PER_BLOCK * 3)
    block = parse_block_source(source, index=1)
    assert len(block.unrecognized) < MAX_STATEMENTS_PER_BLOCK
    # The cap truncates, it does not discard: facts read before it still come back.
    assert block.identity.agent_id == "a"


def test_an_over_long_statement_is_refused_rather_than_matched() -> None:
    """A set literal no caveat produces is refused before the regex sees all of it."""
    huge = ", ".join(f'"scope-{i}"' for i in range(5000))
    block = parse_block_source(f"check if operation($op), [{huge}].contains($op);\n", index=1)
    assert block.caveats == ()
    assert not block.complete


@pytest.mark.parametrize(
    "statement",
    [
        # The variable must be the same on both sides, or the clause means something else.
        'check if operation($a), ["x"].contains($b)',
        # An unknown budget dimension is refused rather than approximated.
        'check if requested("galleons", $v), $v <= 5',
        # A malformed date is refused rather than defaulted.
        "check if time($t), $t >= 2026-13-45T99:99:99Z",
        # An ordering comparison against a string is not a shape `to_datalog()` can emit.
        'reject if arg("p", $v), $v > "text"',
        # A negative depth limit is not a restriction any minter produces.
        "check if current_depth($d), $d <= -1",
        # A set literal whose members are not string literals.
        "check if tool($t), [bare, words].contains($t)",
    ],
)
def test_a_shape_that_almost_matches_is_refused(statement: str) -> None:
    """Near-misses land in `unrecognized`, never in a caveat built from a guess."""
    block = parse_block_source(statement + ";\n", index=1)
    assert block.caveats == ()
    assert block.unrecognized == (statement,)


def test_effective_authority_reports_incompleteness_from_any_block() -> None:
    """`complete` covers the whole chain, not just the terminal block.

    A restriction the parser missed three blocks up is exactly as missing from the fold as
    one it missed in the last block, so the flag has to be an `and` over every block. The
    unreadable statement goes into a block appended the way a third party would append one —
    `attenuate()` cannot produce it, which is the point.
    """
    middle = attenuate(
        _root(),
        [ScopeSubset(scopes=frozenset({"invoice:read", "vendor:read"}))],
        agent_id="agt-middle",
        role="worker",
    )
    with_unknown = (
        verify(middle, _KEY_SET, now=_NOW)
        .biscuit.append(BlockBuilder('agent("agt-leaf");\nrole("reader");\nfuture_check(1);\n'))
        .to_base64()
    )
    token = verify(with_unknown, _KEY_SET, now=_NOW)

    authority = effective_authority(token)
    assert not authority.complete
    assert authority.unrecognized == ("future_check(1)",)
    # The bound still folds everything it *could* read — an upper bound, not nothing.
    assert authority.bound.scopes == frozenset({"invoice:read", "vendor:read"})


def test_a_chain_this_build_fully_understands_reports_complete() -> None:
    """The counterpart, so `complete` is not quietly false for every token."""
    assert effective_authority(_child(*_EVERY_KIND)).complete
