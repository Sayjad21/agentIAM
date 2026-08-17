"""Identity tree queries — T-045.

Builds the agent delegation tree entirely from existing tables: audit_records, budgets,
revocations, and escalations. No I/O inside the models; everything returns frozen Pydantic
structures for the JSON endpoint to serve.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentiam_controlplane.db.models import (
    AuditRecordRow,
    BudgetRow,
    EscalationRow,
    RevocationRow,
)
from agentiam_core.escalation import EscalationState

logger = logging.getLogger(__name__)


class TreeBudget(BaseModel):
    """Remaining budget for one dimension."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    total: Decimal
    available: Decimal


class TreeNode(BaseModel):
    """One node in the agent delegation tree."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    role: str
    depth: int
    task_id: str
    principal_id: str
    block_ids: list[str]
    scopes: list[str]
    budget: list[TreeBudget]
    revoked: bool
    revocation_reason: str | None
    has_pending_escalation: bool
    last_outcome: str
    last_reason_code: str
    last_seen: datetime


class TreeDiff(BaseModel):
    """Minimal diff for SSE animation."""

    model_config = ConfigDict(frozen=True)

    added: list[TreeNode]
    changed: list[TreeNode]
    removed: list[TreeNode]


async def build_tree(
    session: AsyncSession, *, task_id: uuid.UUID | str, now: datetime
) -> list[TreeNode]:
    """Reconstruct the identity tree for a task from its audit records.

    Groups records by `(agent_id, token_chain_ids)` to find the newest state of each unique
    agent token chain. Then fetches budgets, revocations, and escalations in three
    parallel-shaped queries.

    Args:
        session: An active database session.
        task_id: The task to visualize.
        now: Current time, for any state filtering (though this uses the DB state directly).

    Returns:
        A list of `TreeNode` objects sorted by `(depth, agent_id)`.
    """
    task_id_str = str(task_id)

    # 1. Fetch all audit records for this task, ordered newest first.
    # Group by (agent_id, block_ids_tuple) in Python since we just need the first seen.
    # We could do DISTINCT ON in Postgres, but this is simple and cross-database friendly
    # for testing, though Postgres is mandated.
    audit_stmt = (
        select(AuditRecordRow)
        .where(AuditRecordRow.record["task_id"].as_string() == task_id_str)
        .order_by(desc(AuditRecordRow.seq))
    )
    result = await session.execute(audit_stmt)
    records = result.scalars().all()

    if not records:
        return []

    # Map from (agent_id, block_ids_tuple) to the newest AuditRecordRow
    seen_chains: set[tuple[str, tuple[str, ...]]] = set()
    latest_records: list[AuditRecordRow] = []

    mandate_ids: set[str] = set()
    all_block_ids: set[str] = set()
    agent_ids: set[str] = set()

    for row in records:
        record_data = row.record
        agent = str(record_data.get("agent_id", ""))
        chain_list = record_data.get("token_chain_ids", [])
        if not isinstance(chain_list, list):
            continue
        chain_tuple = tuple(str(x) for x in chain_list)
        key = (agent, chain_tuple)

        if key not in seen_chains:
            seen_chains.add(key)
            latest_records.append(row)
            agent_ids.add(agent)
            all_block_ids.update(chain_tuple)
            m_id = record_data.get("mandate_id")
            if m_id:
                mandate_ids.add(str(m_id))

    # 2. Budgets
    agent_budgets: dict[str, list[TreeBudget]] = {a: [] for a in agent_ids}
    if mandate_ids:
        # Try converting mandate_ids to UUIDs. In tests it might be str or UUID.
        m_uuids: list[uuid.UUID] = []
        for m in mandate_ids:
            try:
                m_uuids.append(uuid.UUID(m))
            except ValueError:
                pass

        if m_uuids:
            budget_stmt = select(BudgetRow).where(BudgetRow.mandate_id.in_(m_uuids))
            budget_result = await session.execute(budget_stmt)

            # Find the root agent to assign pool budgets to
            root_agent_id = None
            for r in latest_records:
                if int(str(r.record.get("depth", 0) or 0)) == 0:
                    root_agent_id = str(r.record.get("agent_id", ""))
                    break

            for b_row in budget_result.scalars().all():
                target_agent = b_row.agent_id if b_row.agent_id else root_agent_id
                if target_agent and target_agent in agent_budgets:
                    # available = total - committed - leased - allocated
                    avail = b_row.total - b_row.committed - b_row.leased - b_row.allocated
                    tb = TreeBudget(dimension=b_row.dimension, total=b_row.total, available=avail)
                    agent_budgets[target_agent].append(tb)

    # 3. Revocations
    revoked_blocks: dict[str, str] = {}  # block_id -> reason
    if all_block_ids:
        rev_stmt = select(RevocationRow).where(RevocationRow.block_id.in_(all_block_ids))
        rev_result = await session.execute(rev_stmt)
        for r_row in rev_result.scalars().all():
            revoked_blocks[r_row.block_id] = r_row.reason

    # 4. Escalations
    pending_escalations: set[str] = set()
    if agent_ids:
        esc_stmt = select(EscalationRow.agent_id).where(
            EscalationRow.task_id == uuid.UUID(task_id_str),
            EscalationRow.state == EscalationState.PENDING.value,
            EscalationRow.agent_id.in_(agent_ids),
        )
        esc_result = await session.execute(esc_stmt)
        for a_id in esc_result.scalars().all():
            pending_escalations.add(a_id)

    # 5. Assemble TreeNode objects
    nodes: list[TreeNode] = []
    for row in latest_records:
        rec = row.record
        agent_id = str(rec.get("agent_id", ""))
        chain_list_obj = rec.get("token_chain_ids", [])
        block_ids = [str(x) for x in chain_list_obj] if isinstance(chain_list_obj, list) else []
        # Check revocation
        revoked = False
        revocation_reason = None
        for b_id in block_ids:
            if b_id in revoked_blocks:
                revoked = True
                revocation_reason = revoked_blocks[b_id]
                break

        # Scope is logged as string in decision, but token might have multiple scopes.
        # Tree displays the scopes of the agent. If record only has the requested 'scope', we
        # can infer it or we might extract 'scopes' from known caveats if we had them.
        # For now, we take 'scope' from the latest request, or empty list.
        # It's better if we can get scopes from record, but `scope` is the single requested scope.
        scope_val = rec.get("scope")
        scopes = [str(scope_val)] if scope_val else []

        # role extraction from context: not directly there, we can look at agent_id or
        # default to "unknown" as per PLAN.md
        role = "unknown"
        if "role" in rec:
            role = str(rec["role"])

        nodes.append(
            TreeNode(
                agent_id=agent_id,
                role=role,
                depth=int(str(rec.get("depth", 0) or 0)),
                task_id=task_id_str,
                principal_id=str(rec.get("principal_id", "")),
                block_ids=block_ids,
                scopes=scopes,
                budget=sorted(agent_budgets.get(agent_id, []), key=lambda b: b.dimension),
                revoked=revoked,
                revocation_reason=revocation_reason,
                has_pending_escalation=agent_id in pending_escalations,
                last_outcome=str(rec.get("outcome", "")),
                last_reason_code=str(rec.get("reason_code", "")),
                last_seen=row.created_at,
            )
        )

    # 6. Sort by (depth, agent_id)
    nodes.sort(key=lambda n: (n.depth, n.agent_id))
    return nodes


def build_tree_diff(old: list[TreeNode], new: list[TreeNode]) -> TreeDiff:
    """Compute minimal diff between two snapshots for SSE animation.

    Identity key is (agent_id, first_block_id). If block_ids is empty, just agent_id.
    """

    def key(n: TreeNode) -> tuple[str, str]:
        return (n.agent_id, n.block_ids[0] if n.block_ids else "")

    old_map = {key(n): n for n in old}
    new_map = {key(n): n for n in new}

    added = []
    changed = []
    removed = []

    for k, n in new_map.items():
        if k not in old_map:
            added.append(n)
        elif old_map[k] != n:
            changed.append(n)

    for k, n in old_map.items():
        if k not in new_map:
            removed.append(n)

    return TreeDiff(added=added, changed=changed, removed=removed)
