"""The caveat DSL: compilation to biscuit Datalog, and pure-Python evaluation.

Implements [`docs/specs/02-caveat-language.md`](../../../../docs/specs/02-caveat-language.md).

Two operations per caveat kind, and they MUST agree on allow/deny:

* `to_datalog(caveat)` — the clause biscuit will actually run
* `evaluate(caveat, ctx)` — the same decision in Python

The agreement is a **security property**, not a convenience. The PEP trusts Datalog for the
decision and `evaluate()` for the explanation, so a divergence produces a decision record
naming a caveat that never fired. The conformance test compiles each caveat into a real
biscuit block and compares verdicts, rather than re-implementing the comparison twice.

Two rules from the specs shape almost every line here:

**Clause form follows the fact, not the caveat** (ADR-007). A caveat over a fact the
verifier always supplies compiles to `check if`; one over a fact that may legitimately be
absent compiles to `reject if`, which is vacuous when the fact is missing. Getting this
backwards makes a caveat either deny unrelated calls or, worse, fail open.

**Checks constrain request context, never the token's own grant facts** (ADR-005). Biscuit
checks are existential, so a clause written against the grant passes whenever *any* granted
value matches — which is not the question being asked.

No I/O, no clock. `RequestContext.now` carries the instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, assert_never, cast

from agentiam_core.errors import CaveatError, ReasonCode
from agentiam_core.models import (
    BUDGET_SCALE,
    ArgOperator,
    ArgPredicate,
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    DepthLimit,
    IntentBound,
    Outcome,
    RequestContext,
    RequiresApproval,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)

_RFC3339: Final = "%Y-%m-%dT%H:%M:%SZ"

#: The negation of each operator. `ArgPredicate` compiles to `reject if <negated>`, so that
#: an absent argument leaves the clause vacuous rather than denying (spec 02 §3.2).
_NEGATED: Final[dict[ArgOperator, str]] = {
    ArgOperator.LE: ">",
    ArgOperator.LT: ">=",
    ArgOperator.GE: "<",
    ArgOperator.GT: "<=",
    ArgOperator.EQ: "!=",
    ArgOperator.NE: "==",
}


# ---------------------------------------------------------------------------
# Rendering primitives
# ---------------------------------------------------------------------------


def quote_string(value: str) -> str:
    """Render a Python string as a Datalog string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def datalog_date(value: datetime) -> str:
    """Render a datetime as a Datalog date literal, in UTC."""
    return value.astimezone(UTC).strftime(_RFC3339)


def scale(value: Decimal | int) -> int:
    """Scale a numeric term to integer units.

    Every numeric term in the language — budgets and numeric arguments alike — is scaled by
    the same factor, so one comparison rule covers all of them (spec 01 §3.1).

    Raises:
        CaveatError: If the value cannot be represented exactly at this scale.
    """
    scaled = Decimal(value) * BUDGET_SCALE
    if scaled != scaled.to_integral_value():
        raise CaveatError(f"{value} cannot be represented exactly at scale {BUDGET_SCALE}")
    return int(scaled)


def _term(value: Decimal | int | str) -> str:
    """Render a caveat or argument term: numbers scaled, strings quoted."""
    if isinstance(value, str):
        return quote_string(value)
    return str(scale(value))


def _set_literal(values: frozenset[str]) -> str:
    """Render a set of strings, sorted so output is stable."""
    return "[" + ", ".join(quote_string(v) for v in sorted(values)) + "]"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaveatResult:
    """The verdict of one caveat against one request."""

    outcome: Outcome
    reason_code: ReasonCode
    detail: str = ""

    @property
    def allowed(self) -> bool:
        """True only for a plain allow. Escalation is not permission."""
        return self.outcome is Outcome.ALLOW

    @classmethod
    def allow(cls) -> CaveatResult:
        """The caveat is satisfied."""
        return cls(Outcome.ALLOW, ReasonCode.OK)

    @classmethod
    def deny(cls, reason_code: ReasonCode, detail: str) -> CaveatResult:
        """The caveat is violated.

        `detail` MUST name the caveat, never the argument value — decision records are
        replicated into the audit ledger and the console (NFR-5).
        """
        return cls(Outcome.DENY, reason_code, detail)

    @classmethod
    def escalate(cls, detail: str) -> CaveatResult:
        """The caveat requires human approval. Not a deny (ADR-008)."""
        return cls(Outcome.ESCALATE, ReasonCode.APPROVAL_REQUIRED, detail)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def to_datalog(caveat: Caveat) -> str:
    """Compile `caveat` to biscuit Datalog.

    Returns one or more clauses, newline-separated. `RequiresApproval` returns *facts*
    rather than clauses, because Datalog cannot express escalation (ADR-008).

    Raises:
        CaveatError: If `caveat` is not one of the nine caveat types.
    """
    match caveat:
        case ScopeSubset():
            return f"check if operation($op), {_set_literal(caveat.scopes)}.contains($op);"

        case BudgetCeiling():
            dim = quote_string(caveat.dimension.value)
            return f"check if requested({dim}, $v), $v <= {scale(caveat.value)};"

        case TimeWindow():
            clauses: list[str] = []
            if caveat.not_before is not None:
                clauses.append(f"check if time($t), $t >= {datalog_date(caveat.not_before)};")
            if caveat.not_after is not None:
                # Strict `<`: the expiry boundary is exclusive (EC-T06).
                clauses.append(f"check if time($t), $t < {datalog_date(caveat.not_after)};")
            return "\n".join(clauses)

        case ToolAllow():
            # `check if`, deliberately, even though `tool` is optional: a call with no tool
            # identity must not satisfy an allow-list (spec 02 §4.4).
            return f"check if tool($t), {_set_literal(caveat.tools)}.contains($t);"

        case ToolDeny():
            return f"reject if tool($t), {_set_literal(caveat.tools)}.contains($t);"

        case ArgPredicate():
            # `ArgPredicate` validates at construction that membership operators carry a
            # set and comparison operators carry a scalar, so these casts restate a
            # guarantee rather than assuming one. See `models.ArgPredicate`.
            path = quote_string(caveat.path)
            if caveat.op is ArgOperator.IN:
                members = cast("frozenset[str]", caveat.value)
                return f"reject if arg({path}, $v), !{_set_literal(members)}.contains($v);"
            if caveat.op is ArgOperator.NOT_IN:
                members = cast("frozenset[str]", caveat.value)
                return f"reject if arg({path}, $v), {_set_literal(members)}.contains($v);"
            scalar = cast("Decimal | int | str", caveat.value)
            return f"reject if arg({path}, $v), $v {_NEGATED[caveat.op]} {_term(scalar)};"

        case DepthLimit():
            return f"check if current_depth($d), $d <= {caveat.max_depth};"

        case IntentBound():
            return f"check if request_intent($h), $h == {quote_string(caveat.intent_hash)};"

        case RequiresApproval():
            # A fact, not a clause. The PEP reads it back via block_source() and evaluates
            # it in Python, because a biscuit authorizer has no third answer (ADR-008).
            return "\n".join(
                f"requires_approval({quote_string(s)});" for s in sorted(caveat.scopes)
            )

        case _:
            raise CaveatError(f"not a caveat: {type(caveat).__name__}")


def request_context_datalog(ctx: RequestContext) -> str:
    """Render the verifier-supplied request context as Datalog facts (spec 01 §7).

    Every budget dimension is emitted, defaulting to zero. That is not tidiness: a check
    whose fact is absent *fails*, so an omitted dimension would deny the request rather
    than leave it unconstrained (ADR-007).

    `tool` and `arg` are emitted only when present — they are the facts whose absence must
    stay vacuous, which is why the caveats over them compile to `reject if`.
    """
    lines = [
        f"operation({quote_string(ctx.operation)});",
        f"current_depth({ctx.current_depth});",
        f"request_intent({quote_string(ctx.request_intent)});",
        f"time({datalog_date(ctx.now)});",
    ]
    lines += [
        f"requested({quote_string(d.value)}, {scale(ctx.requested[d])});" for d in BudgetDimension
    ]
    if ctx.tool is not None:
        lines.append(f"tool({quote_string(ctx.tool)});")
    lines += [
        f"arg({quote_string(path)}, {_term(value)});" for path, value in sorted(ctx.args.items())
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _compare(left: Decimal | int | str, op: ArgOperator, right: Decimal | int | str) -> bool:
    """Apply a scalar comparison, matching the Datalog semantics exactly."""
    if op is ArgOperator.EQ:
        return left == right
    if op is ArgOperator.NE:
        return left != right
    # Ordering comparisons are numeric only; the model rejects a set here at construction.
    lhs, rhs = Decimal(str(left)), Decimal(str(right))
    match op:
        case ArgOperator.LE:
            return lhs <= rhs
        case ArgOperator.LT:
            return lhs < rhs
        case ArgOperator.GE:
            return lhs >= rhs
        case ArgOperator.GT:
            return lhs > rhs
        case _:  # pragma: no cover - membership handled by the caller
            raise CaveatError(f"unexpected operator {op}")


def evaluate(caveat: Caveat, ctx: RequestContext) -> CaveatResult:
    """Evaluate `caveat` against `ctx`.

    Agrees with `to_datalog()` on allow/deny for every input; `RequiresApproval` is the one
    exception, returning `escalate` where Datalog can only allow.

    Raises:
        CaveatError: If `caveat` is not one of the nine caveat types.
    """
    match caveat:
        case ScopeSubset():
            if ctx.operation in caveat.scopes:
                return CaveatResult.allow()
            return CaveatResult.deny(
                ReasonCode.SCOPE_ATTENUATED_AWAY,
                f"operation {ctx.operation!r} is not in this token's scope set",
            )

        case BudgetCeiling():
            if ctx.requested[caveat.dimension] <= caveat.value:
                return CaveatResult.allow()
            return CaveatResult.deny(
                ReasonCode.BUDGET_EXHAUSTED_CAVEAT,
                f"request exceeds the {caveat.dimension.value} ceiling of {caveat.value}",
            )

        case TimeWindow():
            if caveat.not_before is not None and ctx.now < caveat.not_before:
                return CaveatResult.deny(
                    ReasonCode.TOKEN_NOT_YET_VALID,
                    f"caveat is not valid before {caveat.not_before.isoformat()}",
                )
            # Exclusive at the upper bound (EC-T06).
            if caveat.not_after is not None and ctx.now >= caveat.not_after:
                return CaveatResult.deny(
                    ReasonCode.TOKEN_EXPIRED,
                    f"caveat expired at {caveat.not_after.isoformat()}",
                )
            return CaveatResult.allow()

        case ToolAllow():
            if ctx.tool is not None and ctx.tool in caveat.tools:
                return CaveatResult.allow()
            return CaveatResult.deny(
                ReasonCode.TOOL_DENIED,
                "tool is absent or not in this token's allow-list",
            )

        case ToolDeny():
            if ctx.tool is None or ctx.tool not in caveat.tools:
                return CaveatResult.allow()
            return CaveatResult.deny(
                ReasonCode.TOOL_DENIED, f"tool {ctx.tool!r} is on this token's deny-list"
            )

        case ArgPredicate():
            if caveat.path not in ctx.args:
                # Absent argument: vacuous, matching `reject if` (spec 02 §3.2).
                return CaveatResult.allow()
            actual = ctx.args[caveat.path]
            if caveat.op in {ArgOperator.IN, ArgOperator.NOT_IN}:
                member = actual in cast("frozenset[str]", caveat.value)
                satisfied = member if caveat.op is ArgOperator.IN else not member
            else:
                satisfied = _compare(actual, caveat.op, cast("Decimal | int | str", caveat.value))
            if satisfied:
                return CaveatResult.allow()
            return CaveatResult.deny(
                ReasonCode.ARG_PREDICATE_FAILED,
                f"argument {caveat.path!r} fails the {caveat.op.value} predicate",
            )

        case DepthLimit():
            if ctx.current_depth <= caveat.max_depth:
                return CaveatResult.allow()
            return CaveatResult.deny(
                ReasonCode.DEPTH_EXCEEDED,
                f"depth {ctx.current_depth} exceeds the caveat limit of {caveat.max_depth}",
            )

        case IntentBound():
            if ctx.request_intent == caveat.intent_hash:
                return CaveatResult.allow()
            return CaveatResult.deny(
                ReasonCode.INTENT_MISMATCH,
                "request is bound to a different task intent",
            )

        case RequiresApproval():
            if ctx.operation in caveat.scopes:
                return CaveatResult.escalate(f"operation {ctx.operation!r} requires human approval")
            return CaveatResult.allow()

        case _:
            raise CaveatError(f"not a caveat: {type(caveat).__name__}")


def block_datalog(caveats: list[Caveat]) -> str:
    """Compile a caveat set into the body of one attenuation block."""
    return "\n".join(to_datalog(c) for c in caveats if to_datalog(c))


def _exhaustive(caveat: Caveat) -> None:  # pragma: no cover - type-checking aid only
    """Fail type checking if a caveat kind is added without handling it here."""
    match caveat:
        case (
            ScopeSubset()
            | BudgetCeiling()
            | TimeWindow()
            | ToolAllow()
            | ToolDeny()
            | ArgPredicate()
            | DepthLimit()
            | IntentBound()
            | RequiresApproval()
        ):
            return
        case _:
            assert_never(caveat)
