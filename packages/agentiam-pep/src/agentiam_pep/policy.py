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

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import cedarpy

from agentiam_core.bundles import PolicyBundle
from agentiam_core.decision import OracleUnavailable, PolicyVerdict
from agentiam_core.hashing import DECIMAL_PLACES
from agentiam_core.models import BudgetDimension
from agentiam_pep.errors import PepError

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

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
    "tool_catalogue",
]


class PolicyBundleError(PepError):
    """A bundle could not be loaded. Raised at construction, never at request time.

    Rejecting here is what keeps `Decision.NoDecision` an impossible state in production
    rather than merely a handled one (spec 05 §5).
    """


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
    *verifier* supplies about the call (ADR-005); `task_id` and `principal_id` come from the
    token's authority block, which `decide()` holds but does not pass to the policy engine.

    **`role` and `declared_role` are two different claims and must not be merged** (ADR-057).
    `role` is what the *organization* says this agent is, and it is a Cedar entity attribute
    — the corpus bundle both grants `invoice:write` on `principal.role == "senior"` and
    forbids critical resources unless it holds. `declared_role` is what the delegating parent
    wrote into the attenuation block, which spec 01 §6.1 assigns to "the console and audit"
    and nothing else. Sourcing `role` from the block would let any agent that can attenuate
    name its own child `"senior"` and pass both guards, which is the `declared_depth` mistake
    ADR-005 exists to prevent, one field over.

    So `declared_role` never becomes an entity attribute. `test_pep_policy.py` asserts the
    attribute set exactly, so adding it there fails a test rather than shipping.
    """

    agent_id: str
    role: str
    principal_id: str
    task_id: uuid.UUID
    #: The role the parent asserted at `attenuate()` time, for the decision record and the
    #: identity tree. Empty when the token declares none, or when the block said so
    #: ambiguously (`datalog.BlockIdentity`). Never an authorization input — see above.
    declared_role: str = ""


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

#: Every attribute a catalogue entry may carry, which is every field of `ToolFacts` bar the
#: id the entry is keyed by. Checked as a closed set rather than splatted into the
#: constructor: `ToolFacts(**entry)` would turn a bundle that misspells `sensitivty` into a
#: `TypeError` at boot, which is the right outcome by accident, but one that spells
#: `is_extrenal` into a *silently ignored* attribute — and an ignored `is_external` is the
#: difference between a permit that fires and one that does not.
_TOOL_ATTRIBUTES: frozenset[str] = frozenset({"tool_id", "server", "sensitivity", "is_external"})


def tool_catalogue(raw: Mapping[str, Mapping[str, Any]] | None) -> dict[str, ToolFacts]:
    """Parse a bundle's resource catalogue into `ToolFacts`, refusing anything malformed.

    A missing catalogue is `{}` — every resource then falls back to `_UNKNOWN_TOOL`, which
    is the safe end of every axis. A *malformed* one raises instead, because the alternative
    is a PEP that starts, looks like it is enforcing resource attributes, and is not. That is
    the failure this whole function exists to make impossible: for as long as the deployed
    service passed no catalogue at all, both resource-attribute rules in the shipped bundle
    were inert and nothing said so.

    Raises:
        PolicyBundleError: An entry is not a mapping, names an attribute `ToolFacts` does not
            have, or gives one the wrong type. Load-time, never request-time.
    """
    if raw is None:
        return {}

    catalogue: dict[str, ToolFacts] = {}
    for tool_id, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise PolicyBundleError(
                f"tool catalogue entry {tool_id!r} is {type(entry).__name__}, not an object"
            )
        unknown = sorted(set(entry) - _TOOL_ATTRIBUTES)
        if unknown:
            raise PolicyBundleError(
                f"tool catalogue entry {tool_id!r} names unknown attribute(s) "
                f"{', '.join(repr(name) for name in unknown)}; known attributes are "
                f"{', '.join(sorted(_TOOL_ATTRIBUTES))}"
            )
        server = entry.get("server", "")
        sensitivity = entry.get("sensitivity", "low")
        is_external = entry.get("is_external", False)
        # `is_external` first: `bool` is a subclass of `int`, and a JSON `0` reaching a Cedar
        # boolean attribute is a request-parse failure at the far end of the hot path.
        if not isinstance(is_external, bool):
            raise PolicyBundleError(
                f"tool catalogue entry {tool_id!r} has is_external="
                f"{is_external!r}, which is not a boolean"
            )
        if not isinstance(server, str) or not isinstance(sensitivity, str):
            raise PolicyBundleError(
                f"tool catalogue entry {tool_id!r} has a non-string server or sensitivity"
            )
        # The key wins over a disagreeing `tool_id`: the key is what `_facts_for` looks up, so
        # trusting the field would leave an entry nothing can ever reach.
        catalogue[tool_id] = ToolFacts(
            tool_id=str(entry.get("tool_id", tool_id)) or tool_id,
            server=server,
            sensitivity=sensitivity,
            is_external=is_external,
        )
    return catalogue


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
        # `is None`, not `or {}`: an explicit empty catalogue is a caller saying "no resource
        # attributes", and must not be silently replaced by the bundle's. Defaulting to the
        # bundle's own is what stops a deployment from having to remember to pass one — the
        # deployed PEP forgot for as long as this argument was the only route in.
        self.tools = dict(tools) if tools is not None else tool_catalogue(bundle.tools)
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
