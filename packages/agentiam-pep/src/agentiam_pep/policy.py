"""Org policy in the hot path — spec 05, step 5 of the pipeline, T-024.

Implements [`docs/specs/05-policy.md`](../../../../docs/specs/05-policy.md).

The token's Datalog answers *what did this chain of delegation permit?* Cedar answers *what
does the organization permit at all, regardless of token?* Both must pass, and neither can
widen the other.

Two things here were settled by measurement and are easy to undo by accident:

1. **`cedarpy.Decision` has three members**, not two: `Allow`, `Deny`, and `NoDecision` — the
   last returned when the policy set fails to parse. `decision == Deny` therefore lets a
   corrupt bundle through as *not denied*. Everything that is not `Allow` is a denial here,
   so a fourth member added upstream also fails closed (spec 05 §4).

2. **The policy set is parsed once, at construction.** Re-parsing per request measured
   167.7 µs against 80.1 µs pre-parsed — 17% of NFR-1's entire 1 ms budget versus 8%
   (spec 05 §6).

The engine is in-process and makes no network call, which is what `PLAN.md` §3.2 requires of
the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import cedarpy

from agentiam_core.decision import OracleUnavailable, PolicyVerdict
from agentiam_core.hashing import DECIMAL_PLACES
from agentiam_core.models import BudgetDimension
from agentiam_pep.errors import PepError

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from agentiam_core.models import RequestContext

#: One taka at the system's money precision, for `quantize`.
_QUANTUM = Decimal(1).scaleb(-DECIMAL_PLACES)

__all__ = [
    "AgentPrincipal",
    "BoundCedarEngine",
    "CedarEngine",
    "OpaEngine",
    "PolicyBundle",
    "PolicyBundleError",
    "ToolFacts",
]


class PolicyBundleError(PepError):
    """A bundle could not be loaded. Raised at construction, never at request time.

    Rejecting here is what keeps `Decision.NoDecision` an impossible state in production
    rather than merely a handled one (spec 05 §5).
    """


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """What the policy service publishes. Signing and staleness are T-025."""

    version: str
    cedar_source: str
    entity_schema: str | None = None


@dataclass(frozen=True, slots=True)
class ToolFacts:
    """The resource side of the entity model — a catalogue entry, not a per-request fact."""

    tool_id: str
    server: str = ""
    sensitivity: str = "low"
    is_external: bool = False


@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    """The principal side, read off the verified token.

    Separate from `RequestContext` because the context deliberately carries only what the
    *verifier* supplies about the call (ADR-005); `role`, `task_id` and `principal_id` come
    from the token, which `decide()` holds but does not pass to the policy engine.
    """

    agent_id: str
    role: str
    principal_id: str
    task_id: uuid.UUID


def _as_cedar_decimal(value: Decimal) -> dict[str, dict[str, str]]:
    """Render money as Cedar's decimal extension value, at exactly four places.

    Four is not a choice: measured, Cedar's decimal accepts `0.0001` and rejects `0.00001`
    with a request-parse failure. That is the same precision as `NUMERIC(20,4)` and
    `BUDGET_SCALE`, so money crosses into policy without a scale conversion anywhere.
    """
    return {"__extn": {"fn": "decimal", "arg": f"{value.quantize(_QUANTUM):f}"}}


#: A tool the catalogue has never heard of. Deliberately the *safe* end of every attribute:
#: an unknown tool must not accidentally satisfy a policy written about a sensitive one, and
#: `is_external=False` is the value that lets a permit apply rather than a forbid.
_UNKNOWN_TOOL = ToolFacts(tool_id="", server="", sensitivity="low", is_external=False)


class CedarEngine:
    """A loaded, parsed bundle plus a tool catalogue. Bind a principal to evaluate.

    Not itself a `PolicyEngine`: `evaluate()` needs the token's facts, so the protocol is
    satisfied by `BoundCedarEngine` from `bound()`.
    """

    def __init__(
        self,
        bundle: PolicyBundle,
        *,
        tools: Mapping[str, ToolFacts] | None = None,
        stale: bool = False,
        unavailable: str | None = None,
    ) -> None:
        """Parse the bundle. Raises rather than deferring a parse error to request time.

        Raises:
            PolicyBundleError: The source is not valid Cedar.
        """
        self.bundle = bundle
        self.tools = dict(tools or {})
        self.stale = stale
        self._unavailable = unavailable
        self.policy_set: cedarpy.PolicySet | None = None

        if unavailable is not None:
            return
        try:
            self.policy_set = cedarpy.PolicySet.from_str(bundle.cedar_source)
        except Exception as exc:
            raise PolicyBundleError(
                f"policy bundle {bundle.version!r} does not parse: {exc}"
            ) from exc

    @classmethod
    def unavailable(cls, why: str) -> CedarEngine:
        """An engine that cannot answer — no bundle fetched yet, for instance.

        `decide()` turns `OracleUnavailable` into
        `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`, which is the right answer: a policy layer
        that cannot be consulted must not be assumed permissive.
        """
        return cls(PolicyBundle(version="", cedar_source=""), unavailable=why)

    def bound(self, principal: AgentPrincipal) -> BoundCedarEngine:
        """A `PolicyEngine` for one agent. Shares the parsed policy set; parses nothing."""
        return BoundCedarEngine(self, principal)

    def _facts_for(self, tool: str | None) -> ToolFacts:
        if tool is None:
            return _UNKNOWN_TOOL
        return self.tools.get(tool, _UNKNOWN_TOOL)

    def _verdict_from(self, decision: object, reasons: Sequence[str]) -> PolicyVerdict:
        """Turn a Cedar decision into a verdict, failing closed on anything unrecognised.

        `is Allow` rather than `== Deny` — see the module docstring and spec 05 §4.
        """
        allowed = decision is cedarpy.Decision.Allow
        return PolicyVerdict(
            allowed=allowed,
            statement=reasons[0] if reasons else None,
            version=self.bundle.version,
            stale=self.stale,
        )


class BoundCedarEngine:
    """`CedarEngine` with a principal attached. This is the `PolicyEngine` `decide()` uses."""

    def __init__(self, engine: CedarEngine, principal: AgentPrincipal) -> None:
        """Hold the shared engine and the per-agent facts."""
        self._engine = engine
        self._principal = principal

    @property
    def policy_set(self) -> cedarpy.PolicySet | None:
        """The parsed bundle, shared with the engine that produced this binding."""
        return self._engine.policy_set

    def evaluate(self, context: RequestContext) -> PolicyVerdict:
        """Evaluate the bundle for one request.

        Raises:
            OracleUnavailable: No bundle is loaded. `decide()` fails closed on this.
        """
        engine = self._engine
        if engine._unavailable is not None:
            raise OracleUnavailable(engine._unavailable)

        tool = engine._facts_for(context.tool)
        principal = self._principal
        entities: list[dict[str, Any]] = [
            {
                "uid": {"type": "Agent", "id": principal.agent_id},
                "attrs": {
                    "role": principal.role,
                    "depth": context.current_depth,
                    "task_id": str(principal.task_id),
                    "principal_id": principal.principal_id,
                },
                "parents": [],
            },
            {
                "uid": {"type": "Tool", "id": context.tool or ""},
                "attrs": {
                    "tool_id": tool.tool_id,
                    "server": tool.server,
                    "sensitivity": tool.sensitivity,
                    "is_external": tool.is_external,
                },
                "parents": [],
            },
        ]

        request = {
            "principal": f'Agent::"{principal.agent_id}"',
            "action": f'Action::"{context.operation}"',
            "resource": f'Tool::"{context.tool or ""}"',
            "context": {
                # Cedar's `decimal` extension, not a scaled integer and never a float.
                # Measured: it holds exactly four decimal places — the same scale as
                # `NUMERIC(20,4)` everywhere else in this system — and a fifth place is
                # rejected as `NoDecision`, which `_verdict_from` turns into a denial. So a
                # policy reads `context.amount.lessThanOrEqual(decimal("500000.0"))` in
                # taka, which is what the NL compiler (T-029) will have to emit and what a
                # human reviewing a bundle has to be able to check (spec 05 §2.1).
                "amount": _as_cedar_decimal(
                    context.requested.get(BudgetDimension.SPEND_BDT, Decimal(0))
                ),
                "arg_digest": "",
                "elevated": False,
                "environment": "production",
            },
        }

        policy_set = engine.policy_set
        assert policy_set is not None  # noqa: S101 - the unavailable case returned above
        response = cedarpy.is_authorized(request, policy_set, entities)
        diagnostics = getattr(response, "diagnostics", None)
        reasons = list(getattr(diagnostics, "reasons", None) or [])
        return engine._verdict_from(response.decision, reasons)


class OpaEngine:
    """The other backend, deferred — `PLAN.md` §21.

    Present so the `PolicyEngine` seam is demonstrated rather than asserted. OPA is an
    out-of-process sidecar call, which is the shape the protocol has to accommodate; an
    abstraction with one in-process implementation proves nothing about that.
    """

    def __init__(self, *, endpoint: str) -> None:
        """Record where the sidecar would be."""
        self.endpoint = endpoint

    def evaluate(self, context: RequestContext) -> PolicyVerdict:
        """Always raises.

        Raises:
            NotImplementedError: Always. Full OPA support is deferred (`PLAN.md` §21).
        """
        raise NotImplementedError(
            f"the OPA backend is deferred (`PLAN.md` §21); {self.endpoint} is not called. "
            f"CedarEngine is the implementation T-024 ships"
        )
