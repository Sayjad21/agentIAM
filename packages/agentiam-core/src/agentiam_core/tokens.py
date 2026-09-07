"""Biscuit token minting and verification.

Implements [`docs/specs/01-token-format.md`](../../../../docs/specs/01-token-format.md).

**`verify()` is not authorization.** It answers three questions: is this token authentic,
is it inside its validity window, and is it structurally sound? Whether a *particular call*
is permitted needs the request context and belongs to the decision pipeline (T-019).

That split is what makes precise reason codes possible. Biscuit reports only *that*
authorization failed, never which clause caused it, so telling `TOKEN_EXPIRED` from
`TOKEN_NOT_YET_VALID` has to happen here — against facts read back from the authority
block, not by interpreting an authorization failure.

Purity: no network, no database, no filesystem, no clock. `now` is a parameter.
`generate_keypair()` is the single nondeterministic function and draws OS entropy; it is
never called implicitly by mint or verify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from biscuit_auth import (
    AuthorizationError,
    Authorizer,
    AuthorizerBuilder,
    Biscuit,
    BiscuitBuilder,
    KeyPair,
    PrivateKey,
    PublicKey,
    Rule,
)

from agentiam_core.caveats import datalog_date, quote_string, request_context_datalog
from agentiam_core.errors import (
    DepthExceededError,
    InvalidSignatureError,
    MalformedTokenError,
    ReasonCode,
    TokenExpiredError,
    TokenNotYetValidError,
    TokenTooLargeError,
    VerificationLimitError,
)
from agentiam_core.models import Budget, BudgetDimension, Mandate, RequestContext

#: Base64 length past which a token is flagged. Measured: a depth-6 chain crosses this
#: (spec 01 §9.1), so the path is reachable and must stay tested (EC-T11).
WARN_SIZE_LIMIT_B64: Final = 4096

#: Base64 length past which a token is refused outright. Measured: a chain at the maximum
#: permitted depth of 8 reaches 4,892 characters, so this is never hit in normal operation.
HARD_SIZE_LIMIT_B64: Final = 8192


def generate_keypair() -> KeyPair:
    """Generate a fresh Ed25519 keypair.

    The only nondeterministic function in `agentiam_core`. It draws OS entropy, so it is
    never called implicitly — callers pass keys in explicitly, which is what keeps
    `mint_root` and `verify` testable against fixed keys.
    """
    return KeyPair()


@dataclass(frozen=True, slots=True)
class RootKeySet:
    """The root public keys a verifier will accept.

    More than one so a root key can be rotated without invalidating tokens already in
    flight (EC-T05). The first entry is the current key — the one new tokens are minted
    with. Retiring a key is removing it from this set; that is the whole mechanism.
    """

    keys: tuple[PublicKey, ...]

    def __init__(self, keys: list[PublicKey] | tuple[PublicKey, ...]) -> None:
        """Build the accepted set, newest first."""
        if not keys:
            raise ValueError("RootKeySet needs at least one key; an empty set rejects everything")
        object.__setattr__(self, "keys", tuple(keys))

    @property
    def current(self) -> PublicKey:
        """The key new tokens are minted against."""
        return self.keys[0]


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """A token whose signature and validity window have been checked.

    Holds the grant read back out of the authority block. It does **not** mean any
    particular action is authorized.
    """

    biscuit: Biscuit
    mandate_id: UUID
    task_id: UUID
    principal_id: str
    intent_hash: str
    scopes: frozenset[str]
    budget: Budget
    scaled_budget: dict[BudgetDimension, int]
    max_depth: int
    not_before: datetime
    expires_at: datetime
    depth: int
    revocation_ids: tuple[str, ...]
    size_b64: int
    size_warning: bool


def _as_uuid(value: object, field: str) -> UUID:
    """Parse an identifier fact back into a UUID.

    Raises:
        MalformedTokenError: If the fact is not a well-formed UUID.
    """
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MalformedTokenError(f"{field} is not a valid UUID: {value!r}") from exc


def mint_root(mandate: Mandate, private_key: PrivateKey) -> str:
    """Mint a root token for `mandate`, signed with the root key.

    Emits the authority block described in spec 01 §5: identity and binding facts, the
    grant, and its checks — grant membership, depth, intent, the validity window, and
    **one budget check per dimension**. Budgets are scaled integers so every dimension is
    compared the same way.

    The budget checks are per-dimension rather than one comparison over a ranging `$dim`,
    and spec 01 §2.3 has the measurement showing why: the ranging form is existential, so a
    dimension inside its ceiling silently rescued one that was over it.

    Args:
        mandate: The grant to encode.
        private_key: The root signing key.

    Returns:
        The token, base64-encoded, ready for an `Authorization: Bearer` header.

    Raises:
        TokenTooLargeError: If the resulting token exceeds the hard size limit.
    """
    lines: list[str] = [
        f"mandate({quote_string(str(mandate.mandate_id))});",
        f"task({quote_string(str(mandate.task_id))});",
        f"principal({quote_string(mandate.principal_id)});",
        f"intent({quote_string(mandate.intent_hash)});",
        f"issued_at({datalog_date(mandate.not_before)});",
        f"not_before({datalog_date(mandate.not_before)});",
        f"expires_at({datalog_date(mandate.expires_at)});",
        f"max_depth({mandate.max_depth});",
    ]
    lines += [f"scope({quote_string(s)});" for s in sorted(mandate.scopes)]
    lines += [
        f"budget({quote_string(d.value)}, {mandate.budget.scaled(d)});"
        for d in sorted(BudgetDimension)
    ]
    lines += [
        # The operation being attempted must be one this mandate granted.
        "check if operation($op), scope($op);",
        # Depth is supplied by the verifier from the block count, never by a block fact.
        f"check if current_depth($d), $d <= {mandate.max_depth};",
        # The request must be bound to the approved task.
        "check if request_intent($h), intent($h);",
    ]
    lines += [
        # One check per dimension, naming it literally — spec 01 §2.3.
        #
        # This was a single `check if requested($dim, $v), budget($dim, $max), $v <= $max;`
        # until it was measured. `check if` is satisfied by *some* binding, `$dim` ranges
        # over every dimension, and §2.2 requires the verifier to supply all of them — so
        # `requested("tool_calls", 0)` against its own ceiling satisfied the check on its
        # own and whatever `spend_bdt` asked for never decided anything. A dimension inside
        # its ceiling rescued one that was over it, on every request shaped the way §2.2
        # mandates. Omitting a dimension was allowed too, for the same reason, which is not
        # what §2.2 claims happens.
        #
        # Naming the dimension makes `$v` range over exactly one fact, so the check is
        # universal by construction and an absent fact still denies. It is also the form
        # `BudgetCeiling` has always compiled to, which is why the caveat ceilings were
        # never affected — only this one ranging check was.
        f"check if requested({quote_string(d.value)}, $v), $v <= {mandate.budget.scaled(d)};"
        for d in sorted(BudgetDimension)
    ]
    lines += [
        # not_before is inclusive; expires_at is EXCLUSIVE (EC-T06).
        f"check if time($t), $t >= {datalog_date(mandate.not_before)};",
        f"check if time($t), $t < {datalog_date(mandate.expires_at)};",
    ]

    token = BiscuitBuilder("\n".join(lines)).build(private_key).to_base64()
    if len(token) > HARD_SIZE_LIMIT_B64:
        raise TokenTooLargeError(
            f"minted token is {len(token)} base64 characters, over the "
            f"{HARD_SIZE_LIMIT_B64} limit; reduce the scope or budget set"
        )
    return token


def _parse(token: str, key_set: RootKeySet) -> Biscuit:
    """Parse and signature-check `token` against each accepted key.

    Raises:
        MalformedTokenError: If the token is absent or not parseable at all.
        InvalidSignatureError: If no accepted key verifies the chain.
    """
    if not token or not token.strip():
        raise MalformedTokenError("token is absent or empty")

    last: Exception | None = None
    for key in key_set.keys:
        try:
            return Biscuit.from_base64(token, key)
        except Exception as exc:
            last = exc
    raise InvalidSignatureError(
        f"token does not verify against any of the {len(key_set.keys)} accepted root key(s): {last}"
    )


#: Explicit Datalog evaluation limits, because biscuit's defaults are not survivable on a
#: loaded host.
#:
#: Measured: `AuthorizerBuilder(...).limits()` defaults to `max_facts=1000`,
#: `max_iterations=100`, and **`max_time=1 millisecond`**. That last one is wall clock, not
#: work — so a query that normally completes in microseconds raises
#: `AuthorizationError: Reached Datalog execution limits` whenever the process loses the
#: CPU for long enough. It surfaced as an intermittent property-test failure that only ever
#: appeared with other tests running alongside (`docs/STATUS.md` gap 13); the same
#: mechanism in production is a *legitimate request denied because of scheduling*.
#:
#: One millisecond is also, uncomfortably, the same order as NFR-1's entire decision
#: budget. A hot-path library whose internal timeout equals the system's latency target
#: will fire under exactly the load that target exists to describe.
#:
#: 250 ms is generous for work measured in microseconds and still bounded, so TM-14
#: (control-plane denial of service via expensive tokens) keeps a ceiling. The fact and
#: iteration caps are raised proportionally: a depth-8 chain with several caveats per block
#: sits well inside them, while the defaults leave little headroom for the chains spec 01
#: §9 permits.
MAX_DATALOG_FACTS: Final = 10_000
MAX_DATALOG_ITERATIONS: Final = 1_000
MAX_DATALOG_TIME: Final = timedelta(milliseconds=250)


def _authorizer(biscuit: Biscuit) -> Authorizer:
    """Build an authorizer used only to read facts back out of the authority block.

    `authorize()` is deliberately never called: this is fact extraction, not a decision.
    Authority-block facts are readable without it.

    The limits are set explicitly on every authorizer this module builds. `AuthorizerLimits`
    has no constructor in `biscuit-python`, so the builder's own object is fetched and
    mutated — which is the only supported route, not a shortcut.

    `limits()` and `set_limits()` are absent from the type stubs but present at runtime
    (measured against 0.4.0), hence the ignores. `test_tokens.py` asserts both the defaults
    and that ours are applied, so a stub or API change fails a test rather than passing
    silently with the limits unset.
    """
    builder = AuthorizerBuilder("allow if true;")
    limits = builder.limits()  # type: ignore[attr-defined]
    limits.max_facts = MAX_DATALOG_FACTS
    limits.max_iterations = MAX_DATALOG_ITERATIONS
    limits.max_time = MAX_DATALOG_TIME
    builder.set_limits(limits)  # type: ignore[attr-defined]
    return builder.build(biscuit)


def _query_one(authorizer: Authorizer, predicate: str) -> object | None:
    """Return the single term of a one-arity authority fact, or None if absent.

    An exceeded execution limit becomes a `VerificationLimitError` rather than escaping as
    a raw `biscuit_auth.AuthorizationError`. Letting the library's exception out would give
    the caller no reason code, which breaks the rule that every deny names one — TM-25's
    recorded residual, closed here.
    """
    try:
        facts = authorizer.query(Rule(f"d($x) <- {predicate}($x)"))
    except AuthorizationError as exc:
        raise VerificationLimitError(
            f"the Datalog engine exceeded its limits reading {predicate!r}: {exc}"
        ) from exc
    for fact in facts:
        term: object = fact.terms[0]
        return term
    return None


def _require(value: object | None, predicate: str) -> object:
    """Fail loudly when a mandatory authority fact is missing."""
    if value is None:
        raise MalformedTokenError(
            f"authority block is missing the mandatory fact {predicate!r}; "
            f"the token was not minted by this system"
        )
    return value


def verify(token: str, key_set: RootKeySet, *, now: datetime) -> VerifiedToken:
    """Verify a token's authenticity, validity window, and structure.

    Not authorization — see the module docstring. Checks, in order, so that the cheapest
    rejection happens first:

    1. size (before any parsing cost is incurred — A-08)
    2. signature chain against the accepted root keys
    3. the mandatory authority facts are present
    4. validity window, `not_before` inclusive and `expires_at` exclusive
    5. chain depth against the mandate's `max_depth`

    Args:
        token: Base64-encoded biscuit.
        key_set: Root public keys to accept.
        now: The verifier's current instant. Must be timezone-aware; the clock is
            injected because `agentiam_core` never reads one.

    Returns:
        The grant read back out of the authority block.

    Raises:
        TokenTooLargeError, MalformedTokenError, InvalidSignatureError,
        TokenNotYetValidError, TokenExpiredError, DepthExceededError: Each carrying its
            own reason code.
        ValueError: If `now` is naive.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware; agentiam_core never reads a clock")

    size = len(token)
    if size > HARD_SIZE_LIMIT_B64:
        raise TokenTooLargeError(
            f"token is {size} base64 characters, over the {HARD_SIZE_LIMIT_B64} limit"
        )

    biscuit = _parse(token, key_set)
    authorizer = _authorizer(biscuit)

    mandate_id = _require(_query_one(authorizer, "mandate"), "mandate")
    task_id = _require(_query_one(authorizer, "task"), "task")
    principal_id = _require(_query_one(authorizer, "principal"), "principal")
    intent_hash = _require(_query_one(authorizer, "intent"), "intent")
    max_depth = _require(_query_one(authorizer, "max_depth"), "max_depth")
    not_before = _require(_query_one(authorizer, "not_before"), "not_before")
    expires_at = _require(_query_one(authorizer, "expires_at"), "expires_at")

    if not isinstance(not_before, datetime) or not isinstance(expires_at, datetime):
        raise MalformedTokenError("validity window facts are not dates")
    if not isinstance(max_depth, int):
        raise MalformedTokenError("max_depth is not an integer")

    # Window, checked here rather than inferred from an authorization failure, so the two
    # sides produce distinct reason codes.
    if now < not_before:
        raise TokenNotYetValidError(f"token becomes valid at {not_before.isoformat()}")
    if now >= expires_at:
        raise TokenExpiredError(f"token expired at {expires_at.isoformat()} (boundary exclusive)")

    depth = biscuit.block_count() - 1
    if depth > max_depth:
        raise DepthExceededError(f"chain depth {depth} exceeds max_depth {max_depth}")

    scopes = frozenset(str(f.terms[0]) for f in authorizer.query(Rule("d($s) <- scope($s)")))
    scaled_budget = {
        BudgetDimension(str(f.terms[0])): int(f.terms[1])
        for f in authorizer.query(Rule("d($k, $v) <- budget($k, $v)"))
    }

    return VerifiedToken(
        biscuit=biscuit,
        mandate_id=_as_uuid(mandate_id, "mandate"),
        task_id=_as_uuid(task_id, "task"),
        principal_id=str(principal_id),
        intent_hash=str(intent_hash),
        scopes=scopes,
        budget=Budget.from_scaled(scaled_budget),
        scaled_budget=scaled_budget,
        max_depth=max_depth,
        not_before=not_before,
        expires_at=expires_at,
        depth=depth,
        revocation_ids=tuple(str(r) for r in biscuit.revocation_ids),
        size_b64=size,
        size_warning=size > WARN_SIZE_LIMIT_B64,
    )


#: Which fact a failed check quantifies over, and therefore what it means. Every `check if`
#: and `reject if` this library emits — the authority block's six (spec 01 §5) and the nine
#: caveat types (spec 02 §3) — references exactly one of these, so the reason code is
#: recoverable from the check source biscuit hands back without parsing the block.
#:
#: **For an attenuation block.** Two of these mean something different in the authority
#: block, which is what `_AUTHORITY_FACT_REASONS` is for.
_FACT_REASONS: Final[tuple[tuple[str, ReasonCode], ...]] = (
    ("operation(", ReasonCode.SCOPE_ATTENUATED_AWAY),
    ("requested(", ReasonCode.BUDGET_EXHAUSTED_CAVEAT),
    ("tool(", ReasonCode.TOOL_DENIED),
    ("arg(", ReasonCode.ARG_PREDICATE_FAILED),
    ("current_depth(", ReasonCode.DEPTH_EXCEEDED),
    ("request_intent(", ReasonCode.INTENT_MISMATCH),
    ("time(", ReasonCode.TOKEN_EXPIRED),
)

#: The same map for **block 0**, where the two budget/scope codes invert. Spec 02 §7 fixes
#: the distinction and this is the only place it can be applied: the fact a check quantifies
#: over says *which dimension* was refused, and the block it sits in says *who set the
#: bound*. Both are needed, and only the second separates these two pairs.
#:
#: * `requested(` in block 0 is the **mandate's own per-request ceiling** (spec 01 §5.2),
#:   not an attenuation — `BUDGET_EXHAUSTED_MANDATE`. Both codes are 429, so no client sees
#:   a different status; what changes is what an operator reads, and the two have different
#:   fixes. "The agent narrowed itself, re-mint without the caveat" versus "the mandate
#:   never granted this, raise the mandate" is not a distinction to leave to guesswork.
#: * `operation(` in block 0 is the grant-membership check, so a miss is
#:   `SCOPE_NOT_GRANTED` — never in the mandate at all — rather than granted-and-then-removed.
#:
#: The other four are the same claim wherever they sit: a depth limit, an intent binding and
#: a validity window mean one thing whoever wrote them.
#:
#: **Reachability, measured rather than assumed.** Five of block 0's six checks are shadowed
#: on the `decide()` path by Python re-implementations that run first (spec 09 §2's steps 2
#: and 4), so they refuse before biscuit is asked. The budget ceiling is the exception —
#: nothing re-implements it, because the ledger bounds the *pool* across requests and this
#: bounds a *single* request (spec 02 §4.2) — which is why it was the one that showed up
#: mislabelled in a live decision record. The mapping is applied by block rather than by
#: caller anyway: `authorize_request` is public, and the codes must be right for anything
#: that calls it, not only for the path that currently happens to shadow five of them.
_AUTHORITY_FACT_REASONS: Final[tuple[tuple[str, ReasonCode], ...]] = (
    ("operation(", ReasonCode.SCOPE_NOT_GRANTED),
    ("requested(", ReasonCode.BUDGET_EXHAUSTED_MANDATE),
    ("tool(", ReasonCode.TOOL_DENIED),
    ("arg(", ReasonCode.ARG_PREDICATE_FAILED),
    ("current_depth(", ReasonCode.DEPTH_EXCEEDED),
    ("request_intent(", ReasonCode.INTENT_MISMATCH),
    ("time(", ReasonCode.TOKEN_EXPIRED),
)

#: Block 0 is the authority block; 1..n are attenuation blocks (spec 01 §4).
AUTHORITY_BLOCK: Final = 0

#: `Check n°0 in block n°1: <source>`. The degree sign is matched as `.` rather than
#: literally, because it reaches us through a Rust error string and this module should not
#: depend on how that round-trips through the FFI on any given platform.
_FAILED_CHECK_RE: Final = re.compile(r"Check n.(\d+) in block n.(\d+): ")


@dataclass(frozen=True, slots=True)
class AuthorityFailure:
    """The token's own Datalog refused the request, and which check did it."""

    block: int
    """Which block carried the failing check. 0 is the authority block; 1+ is attenuation."""

    source: str
    """The check as written in the token, for the decision record's explanation."""

    reason_code: ReasonCode


def authorize_request(token: VerifiedToken, context: RequestContext) -> AuthorityFailure | None:
    """Evaluate the token's *own* checks against this request. `None` means it authorized.

    This is the enforcement half of a biscuit, and the half `verify()` deliberately does not
    do: `verify()` reads the authority block's facts back out and never calls `authorize()`,
    so every `check if` and `reject if` in every block — the mandate's grant and every
    attenuation caveat added since — binds only if something calls this.

    Nothing did. `Pipeline` defaults its `caveats_for` hook to a function returning no
    caveats, and `decide()`'s own docstring justified that as safe on the grounds that
    "biscuit's own authorizer enforces the chain regardless". It would have, had anything
    invoked it. Measured against a live PEP before this existed: a child token restricted to
    `{invoice:read, vendor:read}` with a spend ceiling of zero successfully initiated a
    payment.

    Unlike the `Caveat` list `decide()` also accepts, this needs no Datalog→caveat parser
    (`STATUS.md` gap 2) and no cooperation from the holder: the checks are already inside the
    token, cryptographically bound to it, and this evaluates them where they are. A token
    received from a third party is enforced exactly as well as one this process minted.

    Args:
        token: The verified token. Its parsed `biscuit` carries every block's checks.
        context: What the verifier says about this call. Rendered to the facts the checks
            quantify over — every budget dimension present, because a check whose fact is
            absent *fails* (ADR-007).

    Returns:
        `None` if every check passed, otherwise the first failure, with the block it came
        from and the check's own source.

    Raises:
        VerificationLimitError: The Datalog engine hit a fact, iteration or time limit.
            **Never reported as a denial** — the request was not refused, it was not
            decided, and the two must not share a reason code. `decide()` fails closed on
            it with `VERIFICATION_LIMIT_EXCEEDED`.
    """
    source = request_context_datalog(context) + "\nallow if true;"
    try:
        _build_request_authorizer(source, token.biscuit).authorize()
    except AuthorizationError as exc:
        detail = str(exc)
        if "execution limits" in detail:
            raise VerificationLimitError(detail) from exc
        return _first_failure(detail)
    return None


def _build_request_authorizer(source: str, biscuit: Biscuit) -> Authorizer:
    """An authorizer carrying the request facts, under this module's Datalog limits.

    Separate from `_authorizer` because that one exists to read facts back and is documented
    as never authorizing. Same limits, applied the same way and for the same reason: an
    unbounded Datalog evaluation on the hot path is a denial-of-service (A-08).
    """
    builder = AuthorizerBuilder(source)
    limits = builder.limits()  # type: ignore[attr-defined]
    limits.max_facts = MAX_DATALOG_FACTS
    limits.max_iterations = MAX_DATALOG_ITERATIONS
    limits.max_time = MAX_DATALOG_TIME
    builder.set_limits(limits)  # type: ignore[attr-defined]
    return builder.build(biscuit)


def _first_failure(detail: str) -> AuthorityFailure:
    """Read the first failed check out of biscuit's error message.

    The message lists every failed check as `Check n°<i> in block n°<j>: <source>`. Only the
    first is reported: a decision record names *the* reason a request was refused, and a
    list of every check that happened to fail alongside it is noise for the operator reading
    it (spec 09 §4).
    """
    matches = list(_FAILED_CHECK_RE.finditer(detail))
    if not matches:
        # Authorization failed for a reason the message does not attribute to a check —
        # no allow policy matched, say. Fail closed and hand the operator what was said,
        # rather than inventing a block number to look precise.
        return AuthorityFailure(block=0, source=detail, reason_code=ReasonCode.MALFORMED_REQUEST)

    first = matches[0]
    end = matches[1].start() if len(matches) > 1 else len(detail)
    check_source = detail[first.end() : end].rstrip(", ")
    block = int(first.group(2))

    # Which fact says *what* was refused; which block says *who set the bound*. Two of the
    # seven codes turn on the second, and reading only the first reported the mandate's own
    # ceiling as an attenuation the agent had applied to itself.
    reasons = _AUTHORITY_FACT_REASONS if block == AUTHORITY_BLOCK else _FACT_REASONS
    for fact, code in reasons:
        if fact in check_source:
            return AuthorityFailure(block=block, source=check_source, reason_code=code)

    # A check over a fact this library never emits, so the token was built by something
    # else. It still refused, and the source says how — group it as an attenuation refusal,
    # since the detail is what an operator actually reads.
    return AuthorityFailure(
        block=block, source=check_source, reason_code=ReasonCode.SCOPE_ATTENUATED_AWAY
    )
