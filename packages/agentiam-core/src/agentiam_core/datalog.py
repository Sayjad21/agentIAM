r"""Reading a token's Datalog back into caveats — the other direction from `caveats.py`.

Implements [`docs/specs/02-caveat-language.md`](../../../../docs/specs/02-caveat-language.md)
§11. Closes `STATUS.md` §3 gap 2.

`caveats.py` compiles a `Caveat` to Datalog. This module recognizes the result. It exists
because **block facts are unreachable any other way**: spec 02 §9 finding 13 records that
`authorizer.query` sees authority-block facts and returns nothing for block facts, and that
still holds against the installed library. So `agent()`, `role()` and `requires_approval()`
— and every caveat a third party's attenuation block added — can be read only from
`Biscuit.block_source(i)`.

**Enforcement never depended on this.** Biscuit's own authorizer evaluates the token's
checks natively, which is what `tokens.authorize_request()` calls and what refuses a
request. This module serves the three jobs that need the caveats as *objects*:

* naming the effective bound in the console and the identity tree (T-045),
* naming the failing caveat on a decision record (spec 09 §4),
* reading the delegated agent's identity off its own block (spec 01 §6.1).

Why this is written as a recognizer and not a parser
----------------------------------------------------

`block_source()` returns text, and the note carried on gap 2 says whatever reads it **must
not trust it** — TM-24. Three measured facts govern the design, all re-checked against the
installed `biscuit-python` rather than taken from the threat model:

1. **The renderer escapes nothing.** A `role` of ``x"); admin(true); role("y`` renders as
   block text that re-parses into four facts, two of them named `role`. `validate_label`
   stops that at our own mint; a token minted elsewhere carries whatever it likes.
2. **A string may contain `;` and even a raw newline**, both rendered literally. So
   statements cannot be split on `;` naively, nor on line breaks. `_statements()` tracks
   quote state instead.
3. **The renderer is canonical.** Whitespace is normalized and facts are grouped ahead of
   checks regardless of how the block was built, so the input grammar is biscuit's *output*
   grammar — small, fixed, and safe to recognize with anchored patterns.

The consequences, which are the security-relevant part:

* **Nothing is evaluated.** Every statement is matched against one of the closed set of
  shapes `caveats.py` emits. Anything else goes to `unrecognized` verbatim.
* **An unrecognized statement is never dropped silently.** A caveat this build cannot read
  is a restriction the token has and the fold does not, so `TokenAuthority.complete` says
  so and a console showing the bound must say so too. Dropping it would *overstate*
  authority, which is the one direction that matters.
* **A repeated identity fact refuses the field.** Two `role()` facts is the exact signature
  of finding 1, and there is no correct way to pick one. `role` comes back `None` with the
  predicate listed in `duplicate_facts`, and the caller falls back to something it derived
  itself.
* **A label that would not pass `validate_label` is refused too.** TM-24's mitigation
  constrains those three fields on the way in; applying the same rule on the way out is
  what extends it to a token this system did not mint.

Injection cannot widen a bound in any case, and it is worth being precise about why: the
fold intersects and takes minima, so a statement smuggled into a string can only add an
apparent restriction or land in `unrecognized`. Neither raises a ceiling. The damage a
crafted token can do is to its own holder.

No I/O, no clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from agentiam_core.attenuation import EffectiveAuthority, effective_bound, grant_caveats
from agentiam_core.models import (
    BUDGET_SCALE,
    ArgOperator,
    ArgPredicate,
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    DepthLimit,
    IntentBound,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
    validate_label,
)

if TYPE_CHECKING:
    from agentiam_core.tokens import VerifiedToken

__all__ = [
    "BlockIdentity",
    "ChainIdentity",
    "ParsedBlock",
    "TokenAuthority",
    "chain_identity",
    "effective_authority",
    "parse_block_source",
    "parse_token",
    "token_caveats",
    "token_identity",
]

#: Statements recognized per block before the rest are refused wholesale. A block that
#: reaches this is not one this library produced: the demo's deepest block carries 14
#: statements, and the hard size limit bounds a chain long before 512. The cap is here so
#: the work this module does on a hostile token is bounded by a constant rather than by
#: how much the token's minter felt like writing.
MAX_STATEMENTS_PER_BLOCK: Final = 512

#: Longest statement this module will attempt to match. Every shape below is short; a
#: statement past this is either an injected payload or a set literal no caveat produces.
MAX_STATEMENT_LENGTH: Final = 4096

#: Every fragment names its capture. A positional group here would be a bug waiting for the
#: next pattern that adds one: `(?P<v>…)` for the variable already occupies group 1, so the
#: payload's index depends on where it sits in the clause. Named groups make `_build()` read
#: what it means and stop the indices mattering at all.
_STR: Final = r'"(?P<text>[^"]*)"'
_PATH: Final = r'"(?P<path>[^"]*)"'
_DIM: Final = r'"(?P<dim>[^"]*)"'
_VAR: Final = r"\$[A-Za-z_][A-Za-z0-9_]*"
_NUM: Final = r"(?P<num>-?\d+)"
_DATE: Final = r"(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
_SET: Final = r"\[(?P<members>[^\]]*)\]"
_OP: Final = r"(?P<op><=|<|>=|>|==|!=)"

#: One member of a rendered set literal. `caveats._set_literal()` emits `", "` between
#: members and biscuit's renderer reproduces that exactly, so the separator is fixed rather
#: than lax — a laxer split would accept text no renderer produces.
_MEMBER_RE: Final = re.compile(r'^"([^"]*)"$')

#: The rendered operator, and the `ArgOperator` it came from. `to_datalog()` compiles an
#: `ArgPredicate` to `reject if` with the predicate **negated** (spec 02 §4.6), so reading
#: one back means undoing that negation — this table is `caveats._NEGATED` inverted, and a
#: test asserts the two stay each other's inverse rather than trusting the transcription.
_DENEGATED: Final[dict[str, ArgOperator]] = {
    ">": ArgOperator.LE,
    ">=": ArgOperator.LT,
    "<": ArgOperator.GE,
    "<=": ArgOperator.GT,
    "!=": ArgOperator.EQ,
    "==": ArgOperator.NE,
}

_CLAUSE_RES: Final[dict[str, re.Pattern[str]]] = {
    # `check if operation($op), ["a", "b"].contains($op);` — ScopeSubset.
    "scope_subset": re.compile(
        rf"^check if operation\((?P<v>{_VAR})\), {_SET}\.contains\((?P=v)\)$"
    ),
    # `check if operation($op), scope($op);` — the authority block's grant-membership
    # check. Recognized so it does not read as an unknown restriction; not a caveat,
    # because the grant it names is already on `VerifiedToken.scopes`.
    "grant_scope": re.compile(rf"^check if operation\((?P<v>{_VAR})\), scope\((?P=v)\)$"),
    "budget_ceiling": re.compile(
        rf"^check if requested\({_DIM}, (?P<v>{_VAR})\), (?P=v) <= {_NUM}$"
    ),
    "time_lower": re.compile(rf"^check if time\((?P<v>{_VAR})\), (?P=v) >= {_DATE}$"),
    "time_upper": re.compile(rf"^check if time\((?P<v>{_VAR})\), (?P=v) < {_DATE}$"),
    "tool_allow": re.compile(rf"^check if tool\((?P<v>{_VAR})\), {_SET}\.contains\((?P=v)\)$"),
    "tool_deny": re.compile(rf"^reject if tool\((?P<v>{_VAR})\), {_SET}\.contains\((?P=v)\)$"),
    "arg_in": re.compile(
        rf"^reject if arg\({_PATH}, (?P<v>{_VAR})\), !{_SET}\.contains\((?P=v)\)$"
    ),
    "arg_not_in": re.compile(
        rf"^reject if arg\({_PATH}, (?P<v>{_VAR})\), {_SET}\.contains\((?P=v)\)$"
    ),
    "arg_number": re.compile(rf"^reject if arg\({_PATH}, (?P<v>{_VAR})\), (?P=v) {_OP} {_NUM}$"),
    "arg_string": re.compile(rf"^reject if arg\({_PATH}, (?P<v>{_VAR})\), (?P=v) {_OP} {_STR}$"),
    "depth_limit": re.compile(rf"^check if current_depth\((?P<v>{_VAR})\), (?P=v) <= {_NUM}$"),
    "intent_bound": re.compile(rf"^check if request_intent\((?P<v>{_VAR})\), (?P=v) == {_STR}$"),
    # `check if request_intent($h), intent($h);` — the authority block's own binding, like
    # `grant_scope`: recognized, not a caveat.
    "grant_intent": re.compile(rf"^check if request_intent\((?P<v>{_VAR})\), intent\((?P=v)\)$"),
}

_FACT_RE: Final = re.compile(r"^([a-z_][a-z0-9_]*)\((.*)\)$", re.DOTALL)

#: Facts the authority block carries (spec 01 §5). Every one is already on `VerifiedToken`,
#: read there through the authorizer rather than through rendered text, so they are
#: recognized and discarded here. Listing them is what keeps `unrecognized` meaningful:
#: without it every root token would report a dozen unreadable statements.
_AUTHORITY_FACTS: Final = frozenset(
    {
        "budget",
        "expires_at",
        "intent",
        "issued_at",
        "mandate",
        "max_depth",
        "not_before",
        "principal",
        "scope",
        "task",
    }
)

#: Identity facts, and whether each takes a string term. `declared_depth` is the integer.
_IDENTITY_STRING_FACTS: Final = frozenset({"agent", "role"})


@dataclass(frozen=True, slots=True)
class BlockIdentity:
    """Who a block says it issued authority to — spec 01 §6.1.

    Every field is optional, and `None` means *this block does not say*, never a default.
    An authority block has no identity facts at all; a block whose `role` was rendered
    ambiguously has none that can be believed. The caller decides what to do without one,
    which it can only do if the absence is visible.

    **Parent-asserted, all of it.** A block's facts are written by whoever appended the
    block, so these carry the delegating agent's claim about its child. That is the same
    warning spec 01 §6.1 puts on `declared_depth`, and it applies here for the same reason:
    read these for the console, the audit trail and the identity tree, never to decide what
    a request may do.
    """

    agent_id: str | None = None
    role: str | None = None
    declared_depth: int | None = None


@dataclass(frozen=True, slots=True)
class ChainIdentity:
    """Who a whole chain names — the terminal block's identity, and the route to it.

    One object because one `parse_token` answers both, and a caller on the request path
    cannot afford two: a parse costs ~227 µs at depth 3 (`docs/benchmarks/performance.md`
    §tier 4), and the per-request budget there already accounts for exactly two — the
    principal and the caveats.
    """

    #: The terminal block's declared identity, or an empty `BlockIdentity` for a root token.
    #: Identical to what `token_identity` returns, which is now defined in terms of this.
    terminal: BlockIdentity
    #: One agent id per attenuation block, root-first; empty for a root token. The last
    #: segment is `terminal.agent_id`, or the positional fallback where the block declared
    #: none. See `chain_identity` for what this is and is not evidence of.
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """One block's rendered source, read back.

    `caveats` is in statement order, so a caller walking blocks root-first sees the chain in
    the order spec 02 §6 fixes for deny attribution.
    """

    index: int
    identity: BlockIdentity
    caveats: tuple[Caveat, ...]
    #: Statements this build could not recognize, verbatim and untrusted. Non-empty means
    #: the caveat list is a *subset* of what the block restricts.
    unrecognized: tuple[str, ...] = ()
    #: Predicates that appeared more than once where at most one is meaningful, or whose
    #: value failed `validate_label`. The matching `identity` field is `None`.
    duplicate_facts: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True if every statement in the block was recognized."""
        return not self.unrecognized


@dataclass(frozen=True, slots=True)
class TokenAuthority:
    """What a chain permits, folded from its grant and every caveat read back off it.

    `complete` is the field that makes this safe to display. `bound` is computed from the
    caveats this build could read; if a block carried a restriction it could not, the true
    authority is *narrower* than `bound` says. A console rendering `bound` without also
    rendering `complete` would show a token as more powerful than it is.
    """

    bound: EffectiveAuthority
    complete: bool
    unrecognized: tuple[str, ...] = ()


def _statements(source: str) -> list[str]:
    """Split rendered block source into statements, respecting string literals.

    Neither `;` nor a line break is a reliable separator: measured against the installed
    library, a string containing either is rendered back literally, so `role("a;b")` and a
    role holding a newline both survive into the text. Quote state decides instead, with no
    escape processing — because the renderer performs none, a `"` always ends the literal,
    which is exactly how the text would re-parse.

    Splitting on `"` is how that quote state is tracked, rather than a loop over characters.
    The two are equivalent — every `"` toggles, so odd-indexed segments are exactly the ones
    inside a literal — and the difference is not cosmetic: this function sits on the PEP's
    request path (`principal_for` reads the token's identity from block source on every
    call), and the character loop was 54% of the recognizer's time at ~2.5M list appends per
    thousand parses. `str.split` does the same work in C. Measured before and after; the
    numbers are in ADR-057.
    """
    statements: list[str] = []
    current: list[str] = []
    segments = source.split('"')
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if index % 2:
            # Inside a literal. The closing quote is the delimiter that follows, so it is
            # absent for an unterminated string — which only a hand-built block produces,
            # and which matches no clause shape either way.
            current.append('"' + segment + ('"' if index != last else ""))
            continue
        head, *rest = segment.split(";")
        current.append(head)
        for piece in rest:
            statements.append("".join(current).strip())
            if len(statements) >= MAX_STATEMENTS_PER_BLOCK:
                return [s for s in statements if s]
            current = [piece]
    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    return [s for s in statements if s]


def _members(rendered: str) -> frozenset[str] | None:
    """Read a rendered set literal's contents, or `None` if it is not one.

    An empty set is legal and meaningful — `ScopeSubset(frozenset())` denies every operation
    (spec 02 §4.1, EC-T14) — so `""` returns an empty frozenset, not `None`.
    """
    inner = rendered.strip()
    if not inner:
        return frozenset()
    members: list[str] = []
    for part in inner.split(","):
        match = _MEMBER_RE.match(part.strip())
        if match is None:
            return None
        members.append(match.group(1))
    return frozenset(members)


def _unscale(rendered: str) -> Decimal:
    """Undo `caveats.scale()`. Exact: an integer over 10⁴ has no repeating expansion."""
    return Decimal(rendered) / BUDGET_SCALE


def _parse_date(rendered: str) -> datetime | None:
    """Read `caveats.datalog_date()`'s output back, or `None` if it is not a date."""
    try:
        return datetime.strptime(rendered, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


#: What one statement turned out to be. `RECOGNIZED` without a caveat is the authority
#: block's own grant checks: real clauses that restrict nothing beyond the grant already on
#: `VerifiedToken`, so they must not be reported as unknown *or* counted as caveats.
#: Modelled as an enum rather than a `Caveat | bool` union because the union puts the two
#: "no caveat" answers — refused and recognized-but-empty — one `is True` apart, and getting
#: that comparison backwards silently drops every restriction in the block.
@unique
class _Match(StrEnum):
    RECOGNIZED = "recognized"
    REFUSED = "refused"


def _build(name: str, match: re.Match[str]) -> Caveat | _Match:
    """Turn a matched clause into its caveat.

    Returns:
        The caveat; `_Match.RECOGNIZED` for a clause that is recognized but is not one (the
        authority block's own grant checks); `_Match.REFUSED` if the shape matched but a
        term did not.
    """
    if name in ("grant_scope", "grant_intent"):
        return _Match.RECOGNIZED

    if name == "scope_subset":
        scopes = _members(match.group("members"))
        return _Match.REFUSED if scopes is None else ScopeSubset(scopes=scopes)

    if name == "budget_ceiling":
        try:
            dimension = BudgetDimension(match.group("dim"))
        except ValueError:
            return _Match.REFUSED
        return BudgetCeiling(dimension=dimension, value=_unscale(match.group("num")))

    if name in ("time_lower", "time_upper"):
        moment = _parse_date(match.group("date"))
        if moment is None:
            return _Match.REFUSED
        if name == "time_lower":
            return TimeWindow(not_before=moment, not_after=None)
        return TimeWindow(not_before=None, not_after=moment)

    if name in ("tool_allow", "tool_deny"):
        tools = _members(match.group("members"))
        if tools is None:
            return _Match.REFUSED
        return ToolAllow(tools=tools) if name == "tool_allow" else ToolDeny(tools=tools)

    if name in ("arg_in", "arg_not_in"):
        members = _members(match.group("members"))
        if members is None:
            return _Match.REFUSED
        # `!SET.contains($v)` is the *negation* of `in`, so the caveat it came from is
        # `in`; the un-negated form is `not_in`. Reversing these silently inverts an
        # allow-list into a deny-list, which is why they are separate patterns rather than
        # one pattern with an optional `!`.
        op = ArgOperator.IN if name == "arg_in" else ArgOperator.NOT_IN
        return ArgPredicate(path=match.group("path"), op=op, value=members)

    if name == "arg_number":
        return ArgPredicate(
            path=match.group("path"),
            op=_DENEGATED[match.group("op")],
            value=_unscale(match.group("num")),
        )

    if name == "arg_string":
        op = _DENEGATED[match.group("op")]
        if op not in (ArgOperator.EQ, ArgOperator.NE):
            # An ordering comparison against a string is not something `to_datalog()` can
            # emit — `_compare()` is numeric-only for those operators, and the model refuses
            # the combination at construction. Refuse rather than build an object whose own
            # `evaluate()` would raise.
            return _Match.REFUSED
        return ArgPredicate(path=match.group("path"), op=op, value=match.group("text"))

    if name == "depth_limit":
        depth = int(match.group("num"))
        return _Match.REFUSED if depth < 0 else DepthLimit(max_depth=depth)

    if name == "intent_bound":
        return IntentBound(intent_hash=match.group("text"))

    return _Match.REFUSED


def _clause(statement: str) -> Caveat | _Match:
    """Recognize one `check if` / `reject if` statement. See `_build` for the return."""
    for name, pattern in _CLAUSE_RES.items():
        match = pattern.match(statement)
        if match is not None:
            return _build(name, match)
    return _Match.REFUSED


def _label(value: str) -> str | None:
    """A label safe to carry out of a token this system may not have minted.

    TM-24's mitigation constrains `agent_id`, `role` and `principal_id` where they enter a
    token. Applying the same rule where they leave one is what extends the mitigation to a
    token minted elsewhere: an over-long, control-bearing or bidi-reordered label is refused
    rather than handed to a console to render.
    """
    try:
        return validate_label(value, "label")
    except ValueError:
        return None


def _identity_from(facts: dict[str, list[str]]) -> tuple[BlockIdentity, list[str]]:
    """Fold the identity facts, refusing any that is repeated or unsafe.

    A predicate appearing twice is the signature of a value that broke out of its own string
    literal (TM-24), and there is no sound way to choose between the two. The field comes
    back `None` and the predicate is named, so the caller falls back to something it derived
    rather than to an attacker's pick.
    """
    ambiguous: list[str] = []

    def one(predicate: str) -> str | None:
        values = facts.get(predicate, [])
        if len(values) != 1:
            if values:
                ambiguous.append(predicate)
            return None
        return values[0]

    agent_raw, role_raw, depth_raw = one("agent"), one("role"), one("declared_depth")

    agent_id = _label(agent_raw) if agent_raw is not None else None
    if agent_raw is not None and agent_id is None:
        ambiguous.append("agent")
    role = _label(role_raw) if role_raw is not None else None
    if role_raw is not None and role is None:
        ambiguous.append("role")

    declared_depth: int | None = None
    if depth_raw is not None:
        try:
            declared_depth = int(depth_raw)
        except ValueError:
            ambiguous.append("declared_depth")

    return BlockIdentity(agent_id=agent_id, role=role, declared_depth=declared_depth), ambiguous


def parse_block_source(source: str, *, index: int = 0) -> ParsedBlock:
    """Read one block's rendered source into caveats and identity facts.

    Index-agnostic: an authority block yields no caveats *from its grant*, because its grant
    facts and the two checks that reference them are recognized and discarded — the grant is
    already on `VerifiedToken`, read through the authorizer rather than through text. Its
    budget, depth, window and intent checks do come back as caveats, which is what the
    console's per-block view wants; `token_caveats()` skips block 0 for exactly that reason,
    so the two are never counted twice.

    Args:
        source: `Biscuit.block_source(index)`. Untrusted — see the module docstring.
        index: Which block this is, carried onto the result.

    Returns:
        What could be recognized, and a verbatim list of what could not.
    """
    caveats: list[Caveat] = []
    unrecognized: list[str] = []
    approval_scopes: set[str] = set()
    facts: dict[str, list[str]] = {}

    for statement in _statements(source):
        if len(statement) > MAX_STATEMENT_LENGTH:
            unrecognized.append(statement[:MAX_STATEMENT_LENGTH])
            continue

        if statement.startswith(("check if ", "reject if ")):
            clause = _clause(statement)
            if clause is _Match.REFUSED:
                unrecognized.append(statement)
            elif clause is not _Match.RECOGNIZED:
                caveats.append(clause)
            continue

        fact = _FACT_RE.match(statement)
        if fact is None:
            unrecognized.append(statement)
            continue

        predicate, terms = fact.group(1), fact.group(2)
        if predicate in _AUTHORITY_FACTS:
            continue
        if predicate in _IDENTITY_STRING_FACTS or predicate == "requires_approval":
            member = _MEMBER_RE.match(terms.strip())
            if member is None:
                unrecognized.append(statement)
            elif predicate == "requires_approval":
                approval_scopes.add(member.group(1))
            else:
                facts.setdefault(predicate, []).append(member.group(1))
            continue
        if predicate == "declared_depth":
            facts.setdefault(predicate, []).append(terms.strip())
            continue
        unrecognized.append(statement)

    if approval_scopes:
        caveats.append(RequiresApproval(scopes=frozenset(approval_scopes)))

    identity, ambiguous = _identity_from(facts)
    return ParsedBlock(
        index=index,
        identity=identity,
        caveats=tuple(caveats),
        unrecognized=tuple(unrecognized),
        duplicate_facts=tuple(ambiguous),
    )


def parse_token(token: VerifiedToken) -> tuple[ParsedBlock, ...]:
    """Read every block of `token`, root-first.

    Args:
        token: A token whose signature `verify()` already checked. Reading an unverified
            chain would be reading whatever an attacker wrote, which is why this takes a
            `VerifiedToken` and not a `Biscuit`.

    Returns:
        One `ParsedBlock` per block, index 0 first.
    """
    biscuit = token.biscuit
    return tuple(
        parse_block_source(biscuit.block_source(i), index=i) for i in range(biscuit.block_count())
    )


def token_caveats(token: VerifiedToken) -> tuple[Caveat, ...]:
    """Every caveat the attenuation blocks added, root-first.

    Block 0 is skipped: its restrictions are the mandate's own grant, which `verify()`
    already read structurally and which `grant_caveats()` renders as caveats. Including both
    would count the same ceiling twice — harmless to a fold that takes minima, misleading to
    a console listing what narrowed the chain.

    Suitable as `decide()`'s `caveats` argument. An incomplete list is safe there by
    construction: the caveat loop only ever *adds* a denial, and biscuit's own authorizer
    enforces the chain either way (spec 09 §4).
    """
    return tuple(c for block in parse_token(token)[1:] for c in block.caveats)


def chain_identity(token: VerifiedToken) -> ChainIdentity:
    """Read the chain's identities in one parse — the terminal block's, and the path to it.

    The path is one segment per attenuation block, root-first, so a root token's is empty and
    the last segment is the terminal block's `agent_id`. A block that declares no agent, or
    declares one ambiguously, contributes the positional `agt-depth-{index}` its
    `BlockIdentity` could not supply — a name for the *slot*, which no block can choose
    because it is the block's own position in the chain. The index is the depth at that
    block, since `depth` is `block_count - 1`.

    **Every segment is parent-asserted; the path as a whole is still worth more than any one
    of them.** A bare `agent_id` is worth nothing as an authorization input — any agent that
    can attenuate may name its child anything, so a lookup keyed on a name alone lets an
    agent claim any identity in the deployment, which is the `declared_depth` mistake
    (ADR-005) and ADR-057's rejected `role` fact in a third guise. A *path* narrows that to
    identities the claimant could actually have created: a block can only be appended below
    the chain that already exists, so an agent can forge paths that extend its own and no
    others. Anything an organization keys on a path therefore trusts each agent with the
    authority of its own descendants — which delegation already implies, a parent holding at
    least what it hands down — and not with the authority of the tree.

    That is weaker than an issued, organization-signed identity, and it is what is available
    without an issuance service (`STATUS.md` gap 7). It holds only while what is keyed on the
    path does not *widen* with depth: an ancestor granted less than its own descendant can
    reach the descendant's grant by minting it. `pep_service.load_role_assignments` is the
    caller this bears on, and it checks exactly that.
    """
    blocks = parse_token(token)
    return ChainIdentity(
        terminal=blocks[-1].identity if len(blocks) >= 2 else BlockIdentity(),
        path=tuple(b.identity.agent_id or f"agt-depth-{b.index}" for b in blocks[1:]),
    )


def token_identity(token: VerifiedToken) -> BlockIdentity:
    """The identity the terminal block declares — who this token was delegated to.

    The terminal block is this agent's own (`block_count - 1`), the same index the chain's
    depth is computed from. A root token has no attenuation block and therefore no declared
    identity, so every field comes back `None`.

    Parent-asserted, like everything in `BlockIdentity`. Spec 01 §6.1 gives `role` to "the
    console and audit"; treating it as an authorization input would repeat the
    `declared_depth` mistake ADR-005 exists to prevent.

    A caller that also needs the delegation path should call `chain_identity` instead and
    take `.terminal` — this is that, with the path thrown away, and the parse is the
    expensive part.
    """
    return chain_identity(token).terminal


def effective_authority(token: VerifiedToken) -> TokenAuthority:
    """What `token` actually permits — the grant, narrowed by every caveat read back.

    This is what gap 2 asked for: a token received from a third party folded to its true
    effective bound rather than to the authority block's upper bound.

    Args:
        token: A verified token, its own or someone else's.

    Returns:
        The folded bound, and whether every statement behind it was recognized. Callers that
        display `bound` MUST display `complete` too — an unrecognized statement is a
        restriction the fold is missing, so `bound` is an upper one when it is `False`.
    """
    blocks = parse_token(token)
    caveats = [*grant_caveats(token), *(c for block in blocks[1:] for c in block.caveats)]
    unrecognized = tuple(s for block in blocks for s in block.unrecognized)
    return TokenAuthority(
        bound=effective_bound(caveats),
        complete=not unrecognized,
        unrecognized=unrecognized,
    )
