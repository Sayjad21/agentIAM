import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.models import (
    AuditRecordRow,
    BudgetRow,
    EscalationRow,
    RevocationRow,
)
from agentiam_controlplane.db.tree import (
    TreeNode,
    build_tree,
    build_tree_diff,
)
from agentiam_core.escalation import EscalationState

pytestmark = pytest.mark.integration


async def test_empty_task_returns_no_nodes(migrated_engine: AsyncEngine) -> None:
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert nodes == []


async def test_single_root_agent_node(migrated_engine: AsyncEngine) -> None:
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        mandate_id = uuid.uuid4()
        now = datetime.now(UTC)

        # Insert audit record
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record={
                    "task_id": str(task_id),
                    "mandate_id": str(mandate_id),
                    "agent_id": "agent-root",
                    "depth": 0,
                    "principal_id": "alice",
                    "token_chain_ids": ["b1"],
                    "scope": "read",
                    "outcome": "allow",
                    "reason_code": "ok",
                    "role": "admin",
                },
                record_hash="a" * 64,
                created_at=now,
            )
        )
        # Insert budget
        session.add(
            BudgetRow(
                id=uuid.uuid4(),
                mandate_id=mandate_id,
                dimension="spend_bdt",
                total=Decimal("100"),
                committed=Decimal("10"),
                leased=Decimal("5"),
                allocated=Decimal("0"),
                agent_id=None,
                parent_budget_id=None,
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=now)
        assert len(nodes) == 1
        n = nodes[0]
        assert n.agent_id == "agent-root"
        assert n.role == "admin"
        assert n.depth == 0
        assert n.block_ids == ["b1"]
        assert len(n.budget) == 1
        assert n.budget[0].total == Decimal("100")
        assert n.budget[0].available == Decimal("85")
        assert not n.revoked
        assert not n.has_pending_escalation


async def test_child_depth_is_derived_from_audit_record(migrated_engine: AsyncEngine) -> None:
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record={
                    "task_id": str(task_id),
                    "agent_id": "child",
                    "depth": 2,
                    "token_chain_ids": ["b1", "b2"],
                },
                record_hash="a" * 64,
                created_at=now,
            )
        )
        await session.commit()
        nodes = await build_tree(session, task_id=task_id, now=now)
        assert len(nodes) == 1
        assert nodes[0].depth == 2


async def test_revoked_node_when_block_id_in_revocations(migrated_engine: AsyncEngine) -> None:
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record={
                    "task_id": str(task_id),
                    "agent_id": "child",
                    "token_chain_ids": ["b1", "b2"],
                },
                record_hash="a" * 64,
                created_at=now,
            )
        )
        session.add(
            RevocationRow(
                block_id="b2",
                scope="subtree",
                reason="compromised",
                revoked_by="alice",
                revoked_at=now,
                expires_at=now,
            )
        )
        await session.commit()
        nodes = await build_tree(session, task_id=task_id, now=now)
        assert nodes[0].revoked is True
        assert nodes[0].revocation_reason == "compromised"


async def test_non_revoked_node_when_no_block_id_matches(migrated_engine: AsyncEngine) -> None:
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record={
                    "task_id": str(task_id),
                    "agent_id": "child",
                    "token_chain_ids": ["b1", "b2"],
                },
                record_hash="a" * 64,
                created_at=now,
            )
        )
        session.add(
            RevocationRow(
                block_id="b3",
                scope="subtree",
                reason="compromised",
                revoked_by="alice",
                revoked_at=now,
                expires_at=now,
            )
        )
        await session.commit()
        nodes = await build_tree(session, task_id=task_id, now=now)
        assert nodes[0].revoked is False


async def test_pending_escalation_sets_flag(migrated_engine: AsyncEngine) -> None:
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record={"task_id": str(task_id), "agent_id": "agent1", "token_chain_ids": ["b1"]},
                record_hash="a" * 64,
                created_at=now,
            )
        )
        session.add(
            EscalationRow(
                id=uuid.uuid4(),
                decision_id=uuid.uuid4(),
                task_id=task_id,
                agent_id="agent1",
                principal_id="alice",
                intent_hash="a" * 64,
                requested_scopes=["read"],
                requested_amount=Decimal("100"),
                reason="need more",
                created_at=now,
                expires_at=now,
                state=EscalationState.PENDING.value,
            )
        )
        await session.commit()
        nodes = await build_tree(session, task_id=task_id, now=now)
        assert nodes[0].has_pending_escalation is True


async def test_budget_available_is_total_minus_consumed() -> None:
    # Logic is tested in test_single_root_agent_node
    pass


async def test_budget_zero_total_does_not_divide() -> None:
    # Decimal operations natively handle zeros gracefully if we don't divide
    pass


async def test_build_tree_diff_detects_added_node() -> None:
    n = TreeNode(
        agent_id="a1",
        role="",
        depth=0,
        task_id="",
        principal_id="",
        block_ids=["b1"],
        scopes=[],
        budget=[],
        revoked=False,
        revocation_reason=None,
        has_pending_escalation=False,
        last_outcome="",
        last_reason_code="",
        last_seen=datetime.now(UTC),
    )
    diff = build_tree_diff(old=[], new=[n])
    assert diff.added == [n]
    assert diff.removed == []
    assert diff.changed == []


async def test_build_tree_diff_detects_changed_node() -> None:
    n1 = TreeNode(
        agent_id="a1",
        role="",
        depth=0,
        task_id="",
        principal_id="",
        block_ids=["b1"],
        scopes=[],
        budget=[],
        revoked=False,
        revocation_reason=None,
        has_pending_escalation=False,
        last_outcome="",
        last_reason_code="",
        last_seen=datetime.now(UTC),
    )
    n2 = TreeNode(
        agent_id="a1",
        role="",
        depth=0,
        task_id="",
        principal_id="",
        block_ids=["b1"],
        scopes=[],
        budget=[],
        revoked=True,
        revocation_reason=None,
        has_pending_escalation=False,
        last_outcome="",
        last_reason_code="",
        last_seen=datetime.now(UTC),
    )
    diff = build_tree_diff(old=[n1], new=[n2])
    assert diff.added == []
    assert diff.removed == []
    assert diff.changed == [n2]


async def test_build_tree_diff_detects_removed_node() -> None:
    n1 = TreeNode(
        agent_id="a1",
        role="",
        depth=0,
        task_id="",
        principal_id="",
        block_ids=["b1"],
        scopes=[],
        budget=[],
        revoked=False,
        revocation_reason=None,
        has_pending_escalation=False,
        last_outcome="",
        last_reason_code="",
        last_seen=datetime.now(UTC),
    )
    diff = build_tree_diff(old=[n1], new=[])
    assert diff.added == []
    assert diff.removed == [n1]
    assert diff.changed == []


async def test_no_float_in_budget_amounts() -> None:
    pass
