"""Starting a task, and spawning agents under it — the console's demo driver.

Everything else in the console *inspects* a scenario a script already finished running.
This is the one place a human starts one: name the work, set the ceiling, then hand
progressively narrower authority to sub-agents and watch the enforcement point refuse the
ones that overreach.

**Why the mandate id is reused rather than minted fresh.** `LeasePool` binds one
`mandate_id` at construction and the PEP reads it from `AGENTIAM_PEP_MANDATE_ID` at boot
(`scripts/pep_service` §"The PEP is scoped to one mandate", recorded as gap 25). A task
minted under a *new* mandate would therefore have no lease pool behind it and every
budgeted call would fail. So a task varies the `task_id` — which is what the identity
tree, the custody query and the decision feed group by — and keeps the mandate the
running PEP already knows.

The ceiling the operator types is not lost by that choice. It goes into the minted
token's own `Budget`, where it is enforced by a check inside block 0 before the ledger is
consulted at all — measured: a task created with a 50,000 ceiling refuses an 80,000
payment with `BUDGET_EXHAUSTED_MANDATE`, and a sub-agent given 20,000 of it refuses
30,000 with `BUDGET_EXHAUSTED_CAVEAT`. The pool remains the mandate's, so committed spend
on `/budgets` accumulates across tasks rather than resetting per task.

**Tokens live in memory, not in the session.** Starlette's `SessionMiddleware` keeps
session state in the cookie, which browsers cap at about 4 KB; a root token is ~1.6 KB and
a depth-3 chain ~2.7 KB, so two agents would overflow it and the failure would look like a
mysteriously empty page rather than an error. A process-local registry also means no
token is ever handed to the browser. It is deliberately not persisted: this drives a demo,
and a restart should start clean rather than resurrect half a scenario.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from biscuit_auth import PrivateKey, PublicKey

__all__ = [
    "AgentRecord",
    "TaskRecord",
    "TaskRegistry",
    "TaskRegistryError",
]

#: The mandate every console-created task is minted under. See the module docstring: the
#: running PEP binds this at boot, so a task that used its own would have no lease behind it.
DEMO_MANDATE_ID = uuid.UUID("d0d0d0d0-0000-4000-8000-000000000001")

#: Scopes the operator may grant. A fixed set, not free text: a typo in a scope string
#: matches no route, so the request is refused as `MALFORMED_REQUEST` and the operator is
#: left debugging their own spelling instead of watching an authorization decision.
GRANTABLE_SCOPES: tuple[str, ...] = (
    "invoice:read",
    "vendor:read",
    "payment:initiate",
    "email:send",
)

#: How long a console-created task's root token lives. Long enough to demo, short enough
#: that a forgotten one expires rather than lingering.
DEFAULT_TTL = timedelta(hours=8)

#: Deepest chain a console-created task may build. Matches the demo mandate so the Cedar
#: rule that refuses `principal.depth > 2` is reachable, which is a beat worth showing.
DEFAULT_MAX_DEPTH = 4


class TaskRegistryError(RuntimeError):
    """The requested task or agent does not exist, or the request was malformed."""


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """One agent under a task, and the token that authorizes it."""

    agent_id: str
    role: str
    depth: int
    scopes: frozenset[str]
    #: `None` for the root: a mandate carries a budget, not a ceiling caveat.
    ceiling: Decimal | None
    token: str
    parent_id: str | None


@dataclass
class TaskRecord:
    """A task an operator started, and every agent minted under it."""

    task_id: uuid.UUID
    description: str
    budget: Decimal
    scopes: frozenset[str]
    principal_id: str
    created_at: datetime
    expires_at: datetime
    agents: dict[str, AgentRecord] = field(default_factory=dict)

    @property
    def root(self) -> AgentRecord:
        """The task's own root agent."""
        return self.agents["root"]

    def as_dict(self) -> dict[str, object]:
        """A JSON-safe view. Tokens are deliberately omitted — see the module docstring."""
        return {
            "task_id": str(self.task_id),
            "description": self.description,
            "budget": str(self.budget),
            "scopes": sorted(self.scopes),
            "principal_id": self.principal_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "role": a.role,
                    "depth": a.depth,
                    "scopes": sorted(a.scopes),
                    "ceiling": None if a.ceiling is None else str(a.ceiling),
                    "parent_id": a.parent_id,
                }
                for a in sorted(self.agents.values(), key=lambda a: (a.depth, a.agent_id))
            ],
        }


class TaskRegistry:
    """Process-local store of the tasks this console started.

    Not a database and not trying to be: it exists so one operator can drive one demo. A
    restart clears it, which is the intended behaviour — see the module docstring.
    """

    def __init__(
        self,
        *,
        root_private_key: PrivateKey,
        root_public_key: PublicKey,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind the signing key the control plane already holds for elevation."""
        self._private_key = root_private_key
        self._public_key = root_public_key
        self._now = now
        self._tasks: dict[uuid.UUID, TaskRecord] = {}

    # ---- reads ---------------------------------------------------------------

    def list_tasks(self) -> list[TaskRecord]:
        """Every task this process has started, newest first."""
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    def get(self, task_id: uuid.UUID) -> TaskRecord:
        """One task.

        Raises:
            TaskRegistryError: no such task in this process.
        """
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskRegistryError(f"no task {task_id} in this session") from exc

    def token_for(self, task_id: uuid.UUID, agent_id: str) -> str:
        """The bearer token for one agent.

        Raises:
            TaskRegistryError: no such task, or no such agent under it.
        """
        task = self.get(task_id)
        try:
            return task.agents[agent_id].token
        except KeyError as exc:
            raise TaskRegistryError(f"no agent {agent_id!r} under task {task_id}") from exc

    # ---- writes --------------------------------------------------------------

    def create_task(
        self,
        *,
        description: str,
        budget: Decimal,
        scopes: frozenset[str],
        principal_id: str,
        ttl: timedelta = DEFAULT_TTL,
    ) -> TaskRecord:
        """Mint a root token for a new task and remember it.

        Args:
            description: What the human approved. Hashed into the token as its intent, so
                a request claiming a different task is refused `INTENT_MISMATCH`.
            budget: The spend ceiling, in BDT. Enforced by the token itself.
            scopes: What the task may ever do. A child can only ever narrow this.
            principal_id: The human this is minted on behalf of.
            ttl: How long the root token lives.

        Raises:
            TaskRegistryError: the description is blank, the budget is not positive, or
                the scopes are empty or not grantable.
        """
        from agentiam_core.hashing import intent_hash
        from agentiam_core.models import Budget, Mandate
        from agentiam_core.tokens import mint_root

        description = description.strip()
        if not description:
            raise TaskRegistryError("a task needs a description — it becomes the intent hash")
        if budget <= 0:
            raise TaskRegistryError("the budget must be greater than zero")
        if not scopes:
            raise TaskRegistryError("grant at least one scope, or the task can do nothing")
        ungrantable = scopes - set(GRANTABLE_SCOPES)
        if ungrantable:
            raise TaskRegistryError(f"not a grantable scope: {', '.join(sorted(ungrantable))}")

        now = self._now()
        task_id = uuid.uuid4()
        mandate = Mandate(
            mandate_id=DEMO_MANDATE_ID,
            task_id=task_id,
            principal_id=principal_id,
            intent_hash=intent_hash(description),
            scopes=scopes,
            budget=Budget(spend_bdt=budget, tool_calls=1000, rows_read=100_000),
            max_depth=DEFAULT_MAX_DEPTH,
            # A minute of slack: the PEP and this process may disagree fractionally, and a
            # token that is not yet valid on arrival is a confusing way to start a demo.
            not_before=now - timedelta(minutes=1),
            expires_at=now + ttl,
        )
        token = mint_root(mandate, self._private_key)
        record = TaskRecord(
            task_id=task_id,
            description=description,
            budget=budget,
            scopes=scopes,
            principal_id=principal_id,
            created_at=now,
            expires_at=mandate.expires_at,
        )
        record.agents["root"] = AgentRecord(
            agent_id="root",
            role="principal",
            depth=0,
            scopes=scopes,
            ceiling=None,
            token=token,
            parent_id=None,
        )
        self._tasks[task_id] = record
        return record

    def spawn_agent(
        self,
        *,
        task_id: uuid.UUID,
        parent_id: str,
        agent_id: str,
        role: str,
        scopes: frozenset[str],
        ceiling: Decimal,
    ) -> AgentRecord:
        """Attenuate the parent's token into a strictly narrower child.

        The narrowing is checked by `attenuate` itself, which refuses to widen — so a
        request for a scope the parent does not hold, or a ceiling above the parent's,
        raises here rather than producing a token the PEP would later reject.

        Raises:
            TaskRegistryError: the task or parent is unknown, the name collides, or the
                requested authority is not a subset of the parent's.
        """
        from agentiam_core.attenuation import attenuate
        from agentiam_core.models import BudgetCeiling, BudgetDimension, ScopeSubset
        from agentiam_core.tokens import RootKeySet, verify

        task = self.get(task_id)
        agent_id = agent_id.strip()
        if not agent_id:
            raise TaskRegistryError("the agent needs a name")
        if agent_id in task.agents:
            raise TaskRegistryError(f"an agent called {agent_id!r} already exists on this task")
        if parent_id not in task.agents:
            raise TaskRegistryError(f"no parent {parent_id!r} on this task")
        if not scopes:
            raise TaskRegistryError("grant at least one scope, or the agent can do nothing")
        if ceiling < 0:
            raise TaskRegistryError("a ceiling cannot be negative")

        parent = task.agents[parent_id]
        verified = verify(parent.token, RootKeySet([self._public_key]), now=self._now())
        try:
            token = attenuate(
                verified,
                [
                    ScopeSubset(scopes=scopes),
                    BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=ceiling),
                ],
                agent_id=agent_id,
                role=role,
            )
        except Exception as exc:
            # `attenuate` refuses a widening. Surfacing its own words is more useful than
            # a generic failure: it names which dimension was widened.
            raise TaskRegistryError(str(exc)) from exc

        record = AgentRecord(
            agent_id=agent_id,
            role=role,
            depth=parent.depth + 1,
            scopes=scopes,
            ceiling=ceiling,
            token=token,
            parent_id=parent_id,
        )
        task.agents[agent_id] = record
        return record

    def forget(self, task_id: uuid.UUID) -> None:
        """Drop a task and every token under it."""
        self._tasks.pop(task_id, None)


def registry_from_settings(
    *,
    root_private_key: PrivateKey,
    root_public_key: PublicKey,
    now: Callable[[], datetime] | None = None,
) -> TaskRegistry:
    """Build the registry from the keys the control plane already loads."""
    kwargs: Mapping[str, object] = {} if now is None else {"now": now}
    return TaskRegistry(
        root_private_key=root_private_key,
        root_public_key=root_public_key,
        **kwargs,  # type: ignore[arg-type]
    )
