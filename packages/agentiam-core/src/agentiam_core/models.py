"""Domain models: mandates, budgets, caveats, request context, decision records.

Every model here is frozen and validated at construction. Two consequences worth stating,
because they are choices rather than defaults:

**Money is `Decimal`, and a `float` is rejected rather than coerced.** Pydantic would
happily accept `500000.0`, and by the time the imprecision matters it is three services
downstream in a ledger that no longer balances. `int` and `str` are accepted because both
are exact; `float` never is.

**Behaviour lives elsewhere.** `to_datalog()` arrives with T-008 and `narrows()` with
T-009. These are the shapes those tickets attach behaviour to.

No I/O, no clock reads. The clock is injected — see `RequestContext.now`.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum, unique
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic import (
    Tag as PydanticTag,  # noqa: F401  (re-exported shape hint for T-008)
)

from agentiam_core.errors import CaveatError, ReasonCode, ScaleError

#: Scale factor for budget quantities in token facts (spec 01 §3.1).
#: Matches ``NUMERIC(20,4)``: one unit is 1/10000.
BUDGET_SCALE: Final = 10_000

#: Hard cap on delegation depth (spec 01 §9). Measured: a depth-8 chain is 4,940 base64
#: characters, over the 4 KB warning and 60% of the 8 KB hard limit.
MAX_DELEGATION_DEPTH: Final = 8

_SCOPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")
_HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: Longest free-text identifier that may become a Datalog string fact. Generous — a
#: Keycloak subject plus a prefix is well under it — and bounded, because these end up in
#: every request header.
MAX_LABEL_LENGTH: Final = 128

#: Characters forbidden in a free-text identifier that becomes a Datalog string fact.
#:
#: Measured in T-011, not assumed. `quote_string()` escapes correctly, so a crafted value
#: cannot forge a fact *inside a signed token*. But biscuit's `block_source()` renders the
#: string back **unescaped**: a role of ``x"); admin(true); //`` renders as a block whose
#: text re-parses into a genuine, separate ``admin(true)`` fact. Anything that displays or
#: re-parses block source — the console's caveat chain (T-045), the audit explorer
#: (T-048), the Datalog-to-caveat parser those need — is therefore reading attacker-shaped
#: text. Closing it here, at the only place these values enter a token, is cheaper than
#: escaping correctly in every consumer.
#:
#: Quote and backslash break the round trip; C0/C1 controls break the line structure; the
#: bidi overrides reorder rendered text without changing its bytes, which spoofs a role
#: name in the console without touching the signature. Everything else is permitted, so a
#: Bengali role name renders as itself.
_UNSAFE_LABEL_RE: Final = re.compile(
    r'["\\]'  # break out of the quoted string
    r"|[\x00-\x1f\x7f-\x9f]"  # C0 and C1 controls, including newline and tab
    # Bidi marks, embeddings, overrides and isolates. Written as escapes on purpose:
    # these are invisible, and a source file is the last place they should appear
    # literally (see tests/unit/test_source_encoding.py).
    "|[\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def validate_label(value: str, field: str) -> str:
    """Check a free-text identifier is safe to write into a Datalog string fact.

    Args:
        value: The identifier.
        field: Its name, for the error message.

    Returns:
        `value`, unchanged.

    Raises:
        ValueError: If it is empty, too long, or contains a forbidden character.
    """
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > MAX_LABEL_LENGTH:
        raise ValueError(f"{field} is {len(value)} characters, over the {MAX_LABEL_LENGTH} limit")
    found = _UNSAFE_LABEL_RE.search(value)
    if found is not None:
        raise ValueError(
            f"{field} contains {found.group()!r} (U+{ord(found.group()):04X}), which is "
            f"not safe in a Datalog string fact: quotes and backslashes break the "
            f"block-source round trip, and control or bidi characters misrender it"
        )
    return value


_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=False)


def _reject_float(value: object) -> object:
    """Reject floats in money positions.

    Not pedantry: `0.1 + 0.2` is the canonical example, and a budget that drifts by a
    fraction of a paisa per call fails reconciliation in a way that is very hard to trace
    back to a type choice made months earlier.
    """
    if isinstance(value, float):
        raise ValueError("float is not exact and must not carry money; use Decimal, int, or str")
    return value


def _to_scaled(value: Decimal | int) -> int:
    """Convert an exact quantity to scaled integer units, or raise."""
    scaled = Decimal(value) * BUDGET_SCALE
    if scaled != scaled.to_integral_value():
        raise ScaleError(f"{value} cannot be represented exactly at scale {BUDGET_SCALE}")
    return int(scaled)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


@unique
class BudgetDimension(StrEnum):
    """The quantitative dimensions a mandate can bound.

    Fixed set. A dimension absent from a mandate is a ceiling of zero, not unlimited
    (spec 01 §5.2), so adding one here is a protocol change, not a configuration change.
    """

    SPEND_BDT = "spend_bdt"
    TOOL_CALLS = "tool_calls"
    ROWS_READ = "rows_read"
    EXTERNAL_EMAILS = "external_emails"
    WALL_CLOCK_S = "wall_clock_s"


@unique
class CaveatKind(StrEnum):
    """The nine caveat types (spec 02 §4).

    Eight compile to a Datalog clause. `REQUIRES_APPROVAL` compiles to a fact, because
    Datalog has no `escalate` (ADR-008).
    """

    SCOPE_SUBSET = "scope_subset"
    BUDGET_CEILING = "budget_ceiling"
    TIME_WINDOW = "time_window"
    TOOL_ALLOW = "tool_allow"
    TOOL_DENY = "tool_deny"
    ARG_PREDICATE = "arg_predicate"
    DEPTH_LIMIT = "depth_limit"
    INTENT_BOUND = "intent_bound"
    REQUIRES_APPROVAL = "requires_approval"


@unique
class ArgOperator(StrEnum):
    """Comparison operators available to `ArgPredicate` (spec 02 §4.6)."""

    LE = "le"
    LT = "lt"
    GE = "ge"
    GT = "gt"
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"


#: Operators taking a collection rather than a scalar.
MEMBERSHIP_OPERATORS: Final = frozenset({ArgOperator.IN, ArgOperator.NOT_IN})


@unique
class Outcome(StrEnum):
    """What a decision concluded.

    `ESCALATE` is a first-class outcome, not a flavour of deny — raising to a human is the
    point of `RequiresApproval` and of drift detection (`PLAN.md` §6.6).
    """

    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"
    ALLOW_WITH_FLAG = "allow_with_flag"


@unique
class LeaseState(StrEnum):
    """A lease's position in the state machine (spec 04 §3).

    `ACTIVE` is the only non-terminal state. `RELEASED`, `EXPIRED`, and `REVOKED` are each
    reached one-way, and each decrements the budget's `leased` field by the lease's
    `outstanding` amount exactly once — spec 04 §3, §6.
    """

    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

Money = Annotated[Decimal, Field(ge=0, decimal_places=4, max_digits=20)]
Count = Annotated[int, Field(ge=0)]


class Budget(BaseModel):
    """A quantitative allowance across every dimension.

    Money is `Decimal`; the rest are counts. Token facts scale all of them uniformly by
    `BUDGET_SCALE` so one Datalog comparison rule covers every dimension (spec 01 §3.1).
    """

    model_config = _FROZEN

    spend_bdt: Money = Decimal(0)
    tool_calls: Count = 0
    rows_read: Count = 0
    external_emails: Count = 0
    wall_clock_s: Count = 0

    @field_validator("*", mode="before")
    @classmethod
    def _no_floats(cls, value: object) -> object:
        return _reject_float(value)

    def get(self, dimension: BudgetDimension) -> Decimal:
        """Return this budget's allowance for `dimension`."""
        return Decimal(getattr(self, dimension.value))

    def scaled(self, dimension: BudgetDimension) -> int:
        """Return `dimension`'s allowance as a scaled integer, for token encoding."""
        return _to_scaled(self.get(dimension))

    @classmethod
    def from_scaled(cls, values: dict[BudgetDimension, int]) -> Self:
        """Rebuild a budget from scaled integer units.

        Raises:
            ScaleError: If a count dimension does not divide evenly — a fractional tool
                call means the encoding is wrong, and silently flooring it would hide that.
        """
        kwargs: dict[str, Decimal | int] = {}
        for dimension, scaled in values.items():
            exact = Decimal(scaled) / BUDGET_SCALE
            if dimension is BudgetDimension.SPEND_BDT:
                kwargs[dimension.value] = exact
            else:
                if exact != exact.to_integral_value():
                    raise ScaleError(
                        f"{dimension.value}={scaled} is not a whole number of units "
                        f"at scale {BUDGET_SCALE}"
                    )
                kwargs[dimension.value] = int(exact)
        return cls(**kwargs)  # type: ignore[arg-type]

    def covers(self, other: Budget) -> bool:
        """True if this budget is at least as large as `other` in every dimension."""
        return all(self.get(d) >= other.get(d) for d in BudgetDimension)

    def min(self, other: Budget) -> Budget:
        """Per-dimension minimum — the effective bound when two budgets both apply."""
        return Budget.from_scaled(
            {d: min(self.scaled(d), other.scaled(d)) for d in BudgetDimension}
        )


# ---------------------------------------------------------------------------
# Mandate
# ---------------------------------------------------------------------------


class Mandate(BaseModel):
    """The root grant for a task: scopes, budgets, validity window, delegation cap."""

    model_config = _FROZEN

    mandate_id: UUID
    task_id: UUID
    principal_id: str = Field(min_length=1)
    intent_hash: str
    scopes: frozenset[str]
    budget: Budget
    max_depth: int = Field(ge=1, le=MAX_DELEGATION_DEPTH)
    not_before: datetime
    expires_at: datetime

    @field_validator("intent_hash")
    @classmethod
    def _hex64(cls, value: str) -> str:
        if not _HEX64_RE.match(value):
            raise ValueError("intent_hash must be 64 lowercase hex characters (SHA-256)")
        return value

    @field_validator("principal_id")
    @classmethod
    def _safe_principal(cls, value: str) -> str:
        """`principal_id` becomes a Datalog string fact in the authority block."""
        return validate_label(value, "principal_id")

    @field_validator("scopes")
    @classmethod
    def _well_formed_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        for scope in value:
            if not _SCOPE_RE.match(scope):
                raise ValueError(
                    f"malformed scope {scope!r}: expected lowercase 'resource:verb', "
                    f"no wildcards (EC-T15)"
                )
        return value

    @field_validator("not_before", "expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _non_empty_window(self) -> Self:
        if self.expires_at <= self.not_before:
            raise ValueError(
                "expires_at must be strictly after not_before; an empty window is a "
                "token that can never be used"
            )
        return self


# ---------------------------------------------------------------------------
# Caveats
# ---------------------------------------------------------------------------


class _CaveatBase(BaseModel):
    """Shared configuration for every caveat."""

    model_config = _FROZEN


class ScopeSubset(_CaveatBase):
    """Limits which operations the holder may perform. Empty means none (EC-T14)."""

    kind: Literal[CaveatKind.SCOPE_SUBSET] = CaveatKind.SCOPE_SUBSET
    scopes: frozenset[str]

    @field_validator("scopes")
    @classmethod
    def _well_formed(cls, value: frozenset[str]) -> frozenset[str]:
        for scope in value:
            if not _SCOPE_RE.match(scope):
                raise ValueError(f"malformed scope {scope!r}")
        return value


class BudgetCeiling(_CaveatBase):
    """Caps one dimension for a single request. Zero is valid and means 'none'."""

    kind: Literal[CaveatKind.BUDGET_CEILING] = CaveatKind.BUDGET_CEILING
    dimension: BudgetDimension
    value: Money

    @field_validator("value", mode="before")
    @classmethod
    def _no_floats(cls, value: object) -> object:
        return _reject_float(value)


class TimeWindow(_CaveatBase):
    """Restricts validity to an interval. At least one bound is required."""

    kind: Literal[CaveatKind.TIME_WINDOW] = CaveatKind.TIME_WINDOW
    not_before: datetime | None = None
    not_after: datetime | None = None

    @field_validator("not_before", "not_after")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _sane_interval(self) -> Self:
        if self.not_before is None and self.not_after is None:
            raise ValueError("a TimeWindow with no bounds restricts nothing")
        if (
            self.not_before is not None
            and self.not_after is not None
            and self.not_after <= self.not_before
        ):
            raise ValueError("not_after must be strictly after not_before")
        return self


class ToolAllow(_CaveatBase):
    """Allow-list of tool ids. Uses `check if`, so a call with no tool fails closed."""

    kind: Literal[CaveatKind.TOOL_ALLOW] = CaveatKind.TOOL_ALLOW
    tools: frozenset[str]


class ToolDeny(_CaveatBase):
    """Deny-list of tool ids. Deny wins over allow (INV-8, spec 02 §4.5)."""

    kind: Literal[CaveatKind.TOOL_DENY] = CaveatKind.TOOL_DENY
    tools: frozenset[str]


class ArgPredicate(_CaveatBase):
    """Constrains one argument of the call, by dotted path."""

    kind: Literal[CaveatKind.ARG_PREDICATE] = CaveatKind.ARG_PREDICATE
    path: str = Field(min_length=1)
    op: ArgOperator
    value: Decimal | int | str | frozenset[str]

    @field_validator("value", mode="before")
    @classmethod
    def _no_floats(cls, value: object) -> object:
        return _reject_float(value)

    @model_validator(mode="after")
    def _operator_matches_value(self) -> Self:
        is_collection = isinstance(self.value, frozenset)
        if self.op in MEMBERSHIP_OPERATORS and not is_collection:
            raise ValueError(f"{self.op} needs a set of values, got {type(self.value).__name__}")
        if self.op not in MEMBERSHIP_OPERATORS and is_collection:
            raise ValueError(f"{self.op} needs a scalar value, got a set")
        return self


class DepthLimit(_CaveatBase):
    """Caps further delegation. Compared against verifier-computed depth, never a fact."""

    kind: Literal[CaveatKind.DEPTH_LIMIT] = CaveatKind.DEPTH_LIMIT
    max_depth: int = Field(ge=0, le=MAX_DELEGATION_DEPTH)


class IntentBound(_CaveatBase):
    """Pins the token to one approved task. Narrows only by equality (INV-7)."""

    kind: Literal[CaveatKind.INTENT_BOUND] = CaveatKind.INTENT_BOUND
    intent_hash: str

    @field_validator("intent_hash")
    @classmethod
    def _hex64(cls, value: str) -> str:
        if not _HEX64_RE.match(value):
            raise ValueError("intent_hash must be 64 lowercase hex characters (SHA-256)")
        return value


class RequiresApproval(_CaveatBase):
    """Forces the escalation path for a set of scopes.

    The one caveat that is not a Datalog clause: it compiles to a fact and is evaluated in
    Python, because Datalog cannot express `escalate` (ADR-008).
    """

    kind: Literal[CaveatKind.REQUIRES_APPROVAL] = CaveatKind.REQUIRES_APPROVAL
    scopes: frozenset[str]


Caveat = Annotated[
    ScopeSubset
    | BudgetCeiling
    | TimeWindow
    | ToolAllow
    | ToolDeny
    | ArgPredicate
    | DepthLimit
    | IntentBound
    | RequiresApproval,
    Field(discriminator="kind"),
]


class CaveatRef(_CaveatBase):
    """Points at one caveat in a token chain, for decision records and custody queries."""

    block_index: int = Field(ge=0)
    kind: CaveatKind
    detail: str | None = None


def caveat_kind_of(caveat: object) -> CaveatKind:
    """Return a caveat's kind, or raise if it is not a caveat.

    Raises:
        CaveatError: If `caveat` is not one of the nine caveat types.
    """
    kind = getattr(caveat, "kind", None)
    if not isinstance(kind, CaveatKind):
        raise CaveatError(f"not a caveat: {type(caveat).__name__}")
    return kind


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


class DriftMode(str, Enum):
    """How drift scoring affects the decision (T-036)."""
    OFF = "off"
    LOG_ONLY = "log_only"
    STRICT = "strict"


class RequestContext(BaseModel):
    """Facts the verifier supplies about the call in progress (spec 01 §7).

    None of this comes from the token or the agent. `requested` MUST carry every budget
    dimension, defaulting to zero: a check whose fact is absent *fails*, so an omitted
    dimension denies the request rather than leaving it unconstrained (ADR-007). The
    validator below enforces that, because getting it wrong produces a confusing denial
    far from its cause.
    """

    model_config = _FROZEN

    operation: str
    requested: dict[BudgetDimension, Money]
    current_depth: int = Field(ge=0)
    request_intent: str
    now: datetime
    tool: str | None = None
    args: dict[str, Decimal | int | str] = Field(default_factory=dict)
    task_intent_text: str | None = None
    action_intent_text: str | None = None
    drift_mode: DriftMode = DriftMode.STRICT

    @field_validator("requested", "args", mode="before")
    @classmethod
    def _no_floats(cls, value: object) -> object:
        if isinstance(value, dict):
            for item in value.values():
                _reject_float(item)
        return value

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _every_dimension_present(self) -> Self:
        missing = set(BudgetDimension) - set(self.requested)
        if missing:
            raise ValueError(
                f"requested is missing dimension(s) {sorted(d.value for d in missing)}; "
                f"every dimension must be present, defaulting to 0 (ADR-007)"
            )
        return self


# ---------------------------------------------------------------------------
# Decision record
# ---------------------------------------------------------------------------


class DecisionRecord(BaseModel):
    """The immutable record of one authorization decision (`PLAN.md` §6.9).

    Carries `arg_digest`, never the arguments: call arguments may contain PII, and a
    decision record is replicated into the audit ledger, the console, and the evidence
    pack (NFR-5, rule 10).
    """

    model_config = _FROZEN

    decision_id: UUID
    trace_id: str = Field(min_length=1)
    timestamp: datetime
    pep_id: str = Field(min_length=1)
    token_chain_ids: list[str]
    principal_id: str = Field(min_length=1)
    task_id: UUID
    agent_id: str = Field(min_length=1)
    depth: int = Field(ge=0)
    scope: str
    tool_id: str
    arg_digest: str
    outcome: Outcome
    reason_code: ReasonCode
    reason_detail: str = ""
    failing_caveat: CaveatRef | None = None
    policy_version: str
    budget_before: Budget
    budget_after: Budget
    reservation_id: UUID | None = None
    drift_score: Decimal | None = None
    latency_us: int = Field(ge=0)
    elevated_by: str | None = None

    @field_validator("arg_digest")
    @classmethod
    def _digest_not_payload(cls, value: str) -> str:
        if not _HEX64_RE.match(value):
            raise ValueError(
                "arg_digest must be a 64-char SHA-256 hex digest, never the arguments "
                "themselves (NFR-5)"
            )
        return value

    @field_validator("drift_score", mode="before")
    @classmethod
    def _no_float_score(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _outcome_matches_reason(self) -> Self:
        """Every deny names a cause; every allow claims none (P-18)."""
        if self.outcome is Outcome.ALLOW and self.reason_code is not ReasonCode.OK:
            raise ValueError(f"reason_code must be OK for an allow, got {self.reason_code}")
        if self.outcome is not Outcome.ALLOW and self.reason_code is ReasonCode.OK:
            raise ValueError(
                f"reason_code must not be OK for outcome {self.outcome}; "
                f"a deny without a named cause is a bug"
            )
        return self
