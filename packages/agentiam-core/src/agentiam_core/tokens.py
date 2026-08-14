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

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from biscuit_auth import (
    Authorizer,
    AuthorizerBuilder,
    Biscuit,
    BiscuitBuilder,
    KeyPair,
    PrivateKey,
    PublicKey,
    Rule,
)

from agentiam_core.errors import (
    DepthExceededError,
    InvalidSignatureError,
    MalformedTokenError,
    TokenExpiredError,
    TokenNotYetValidError,
    TokenTooLargeError,
)
from agentiam_core.models import Budget, BudgetDimension, Mandate

#: Base64 length past which a token is flagged. Measured: a depth-6 chain crosses this
#: (spec 01 §9.1), so the path is reachable and must stay tested (EC-T11).
WARN_SIZE_LIMIT_B64: Final = 4096

#: Base64 length past which a token is refused outright. Measured: a chain at the maximum
#: permitted depth of 8 reaches 4,940 characters, so this is never hit in normal operation.
HARD_SIZE_LIMIT_B64: Final = 8192

_RFC3339: Final = "%Y-%m-%dT%H:%M:%SZ"


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


def _datalog_time(value: datetime) -> str:
    """Render a datetime as a biscuit date literal, in UTC."""
    return value.astimezone(UTC).strftime(_RFC3339)


def _as_uuid(value: object, field: str) -> UUID:
    """Parse an identifier fact back into a UUID.

    Raises:
        MalformedTokenError: If the fact is not a well-formed UUID.
    """
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MalformedTokenError(f"{field} is not a valid UUID: {value!r}") from exc


def _quote(value: str) -> str:
    """Quote a string for Datalog, escaping what would otherwise break the literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def mint_root(mandate: Mandate, private_key: PrivateKey) -> str:
    """Mint a root token for `mandate`, signed with the root key.

    Emits the authority block described in spec 01 §5: identity and binding facts, the
    grant, and the six checks. Budgets are scaled integers so a single Datalog comparison
    covers every dimension.

    Args:
        mandate: The grant to encode.
        private_key: The root signing key.

    Returns:
        The token, base64-encoded, ready for an `Authorization: Bearer` header.

    Raises:
        TokenTooLargeError: If the resulting token exceeds the hard size limit.
    """
    lines: list[str] = [
        f"mandate({_quote(str(mandate.mandate_id))});",
        f"task({_quote(str(mandate.task_id))});",
        f"principal({_quote(mandate.principal_id)});",
        f"intent({_quote(mandate.intent_hash)});",
        f"issued_at({_datalog_time(mandate.not_before)});",
        f"not_before({_datalog_time(mandate.not_before)});",
        f"expires_at({_datalog_time(mandate.expires_at)});",
        f"max_depth({mandate.max_depth});",
    ]
    lines += [f"scope({_quote(s)});" for s in sorted(mandate.scopes)]
    lines += [
        f"budget({_quote(d.value)}, {mandate.budget.scaled(d)});" for d in sorted(BudgetDimension)
    ]
    lines += [
        # The operation being attempted must be one this mandate granted.
        "check if operation($op), scope($op);",
        # Depth is supplied by the verifier from the block count, never by a block fact.
        f"check if current_depth($d), $d <= {mandate.max_depth};",
        # The request must be bound to the approved task.
        "check if request_intent($h), intent($h);",
        # Per-dimension ceiling from the mandate.
        "check if requested($dim, $v), budget($dim, $max), $v <= $max;",
        # not_before is inclusive; expires_at is EXCLUSIVE (EC-T06).
        f"check if time($t), $t >= {_datalog_time(mandate.not_before)};",
        f"check if time($t), $t < {_datalog_time(mandate.expires_at)};",
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


def _authorizer(biscuit: Biscuit) -> Authorizer:
    """Build an authorizer used only to read facts back out of the authority block.

    `authorize()` is deliberately never called: this is fact extraction, not a decision.
    Authority-block facts are readable without it.
    """
    return AuthorizerBuilder("allow if true;").build(biscuit)


def _query_one(authorizer: Authorizer, predicate: str) -> object | None:
    """Return the single term of a one-arity authority fact, or None if absent."""
    facts = authorizer.query(Rule(f"d($x) <- {predicate}($x)"))
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
