"""Free-text identifiers cannot reshape a token's Datalog (`models.validate_label`).

Found while probing T-011, not reasoned about in advance.

`quote_string()` escapes correctly, so a crafted `role` cannot forge a fact inside a
signed token — the minted block holds one string, and `admin(true)` is not in it.
`TestTheTokenItselfWasNeverForgeable` proves that, and it is the part that matters for
authority.

What is *not* safe is the rendering. Biscuit's `block_source()` writes the string back
with its escaping dropped, so a role of `x"); admin(true); //` renders as block text
containing a genuine second fact — and that text re-parses. Every planned consumer of
block source is a display or parsing path: the console's caveat chain (T-045), the audit
explorer (T-048), and the Datalog-to-caveat parser both need (`STATUS.md` §3, gap 2).
Escaping correctly in each of them is three chances to get it wrong; refusing the
character at the one place these values enter a token is one.

`TestTheRenderingWasTheRealProblem` demonstrates the hazard against the library directly,
so the guard is not folklore.
"""

from __future__ import annotations

import pytest
from biscuit_auth import AuthorizerBuilder, Biscuit, BlockBuilder, Rule

from agentiam_core.attenuation import attenuate
from agentiam_core.caveats import quote_string
from agentiam_core.models import MAX_LABEL_LENGTH, Mandate, ScopeSubset, validate_label
from agentiam_core.tokens import generate_keypair, mint_root, verify
from tests.fixtures.tokens import NOW, ROOT_SCOPES, a_mandate, a_root_client

pytestmark = pytest.mark.security

#: A quote closes the string; the rest becomes free-standing Datalog when re-parsed.
BREAKOUT = 'x"); admin(true); //'

UNSAFE_LABELS = [
    pytest.param(BREAKOUT, id="quote-breakout"),
    pytest.param('a"b', id="bare-quote"),
    pytest.param("a\\b", id="backslash"),
    pytest.param("a\nb", id="newline"),
    pytest.param("a\tb", id="tab"),
    pytest.param("a\x00b", id="nul"),
    pytest.param("a\x1bb", id="escape"),
    pytest.param("a\x9db", id="c1-control"),
    pytest.param("a‮b", id="bidi-override"),
    pytest.param("a‏b", id="rtl-mark"),
    pytest.param("a⁦b", id="bidi-isolate"),
    pytest.param("x" * (MAX_LABEL_LENGTH + 1), id="over-length"),
    pytest.param("", id="empty"),
]

SAFE_LABELS = [
    pytest.param("doc-reader", id="plain"),
    pytest.param("agent:9f2c1e40-7a3b-4d21-9c88-1b2e5f0a4d77:3", id="colon-separated"),
    pytest.param("kc:sub@example.com", id="at-sign"),
    pytest.param("নথি-পাঠক", id="bengali"),
    pytest.param("negotiator (procurement)", id="spaces-and-parens"),
    pytest.param("x" * MAX_LABEL_LENGTH, id="at-the-limit"),
]


class TestValidateLabel:
    @pytest.mark.parametrize("label", UNSAFE_LABELS)
    def test_unsafe_labels_are_refused(self, label: str) -> None:
        with pytest.raises(ValueError, match="role"):
            validate_label(label, "role")

    @pytest.mark.parametrize("label", SAFE_LABELS)
    def test_safe_labels_pass_through_unchanged(self, label: str) -> None:
        """Including Bengali — the rule bans dangerous characters, not non-ASCII ones."""
        assert validate_label(label, "role") == label


class TestTheMintPathsAreGuarded:
    @pytest.mark.parametrize("label", UNSAFE_LABELS)
    def test_role_is_refused_at_attenuation(self, label: str) -> None:
        client = a_root_client()
        with pytest.raises(ValueError, match="role"):
            attenuate(
                client.identity.verified,
                [ScopeSubset(scopes=frozenset({"invoice:read"}))],
                agent_id="agent:1",
                role=label,
            )

    @pytest.mark.parametrize("label", UNSAFE_LABELS)
    def test_agent_id_is_refused_at_attenuation(self, label: str) -> None:
        client = a_root_client()
        with pytest.raises(ValueError, match="agent_id"):
            attenuate(
                client.identity.verified,
                [ScopeSubset(scopes=frozenset({"invoice:read"}))],
                agent_id=label,
                role="reader",
            )

    @pytest.mark.parametrize("label", UNSAFE_LABELS)
    def test_principal_id_is_refused_at_mandate_construction(self, label: str) -> None:
        with pytest.raises(ValueError, match="principal_id"):
            a_mandate(principal_id=label)

    def test_the_sdk_refuses_it_too(self) -> None:
        """The SDK adds no check of its own; it inherits core's."""
        with pytest.raises(ValueError, match="role"):
            a_root_client().attenuate(role=BREAKOUT, scopes=["invoice:read"])

    def test_a_bengali_role_still_mints(self) -> None:
        child = a_root_client().attenuate(role="নথি-পাঠক", scopes=["invoice:read"])
        assert child.identity.role == "নথি-পাঠক"


class TestTheTokenItselfWasNeverForgeable:
    """Scoping the blast radius, so the guard is not oversold.

    This is a display and re-parsing bug, not a privilege escalation, and saying which is
    part of `threat-model.md` being worth anything.
    """

    def test_quote_string_escapes_the_breakout(self) -> None:
        quoted = quote_string(BREAKOUT)
        assert quoted.startswith('"') and quoted.endswith('"')
        assert '\\"' in quoted, "the inner quote must be escaped, not passed through"

    def test_even_a_genuinely_injected_block_fact_is_invisible_to_the_authorizer(
        self,
    ) -> None:
        """Assumption A1 is what bounds the damage.

        The block here carries a real, unescaped `admin(true)` — the worst case, worse
        than anything the rendering bug could produce. It is still not visible to an
        authorizer query, because a block's facts are scoped to that block and cannot
        widen what the chain authorizes.
        """
        client = a_root_client()
        biscuit = client.identity.verified.biscuit.append(
            BlockBuilder('role("reader"); admin(true);')
        )
        authorizer = AuthorizerBuilder("allow if true;").build(biscuit)

        assert authorizer.query(Rule("a($v) <- admin($v)")) == []

    def test_the_grant_read_back_is_unchanged(self) -> None:
        """The scopes, budget and window all still come from the authority block."""
        client = a_root_client()
        biscuit = client.identity.verified.biscuit.append(
            BlockBuilder('role("reader"); admin(true); scope("admin:write");')
        )
        verified = verify(biscuit.to_base64(), client.key_set, now=NOW)

        assert verified.scopes == ROOT_SCOPES, "an appended block cannot add a scope"


class TestTheRenderingWasTheRealProblem:
    """Why the guard exists. Remove it and this is what reaches the console."""

    def test_block_source_drops_the_escaping(self) -> None:
        client = a_root_client()
        # Mint the way `attenuate()` would have, bypassing the new guard.
        biscuit = client.identity.verified.biscuit.append(
            BlockBuilder(f"role({quote_string(BREAKOUT)});")
        )
        rendered = biscuit.block_source(1)

        assert 'role("x"); admin(true); //");' in rendered, (
            "block_source renders the string unescaped — this is the finding"
        )

    def test_the_rendered_source_re_parses_into_a_separate_fact(self) -> None:
        """The consequence: rendered block text is not the block.

        Anything that round-trips block source — a Datalog-to-caveat parser, a console
        that re-renders a chain — sees a fact the signed token never carried.
        """
        client = a_root_client()
        biscuit = client.identity.verified.biscuit.append(
            BlockBuilder(f"role({quote_string(BREAKOUT)});")
        )
        reparsed = str(BlockBuilder(biscuit.block_source(1)))

        assert "admin(true)" in reparsed

    def test_the_same_hazard_exists_in_the_authority_block(self) -> None:
        """Not specific to attenuation — `principal_id` takes the same path.

        Constructed by bypassing `Mandate` validation, since that is now the guard.
        """
        mandate = a_mandate()
        fields = dict(mandate.__dict__)
        fields["principal_id"] = 'kc:1"); admin(true); //'
        forged = Mandate.model_construct(None, **fields)

        key_pair = generate_keypair()
        token = mint_root(forged, key_pair.private_key)
        source = Biscuit.from_base64(token, key_pair.public_key).block_source(0)

        assert "admin(true)" in str(BlockBuilder(source))
