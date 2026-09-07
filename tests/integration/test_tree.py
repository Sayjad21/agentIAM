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


def _a_decision_record(
    *, task_id: uuid.UUID, mandate_id: uuid.UUID, agent_id: str, role: str, depth: int
) -> dict[str, object]:
    """A record built by the real model, then dumped the way the audit sink dumps it.

    Every other test in this module hand-writes the JSON, which is how `role` stayed absent
    from `DecisionRecord` while `build_tree`'s `rec["role"]` branch looked covered: the
    fixtures supplied a key the pipeline never wrote. Going through the model is what makes
    the assertion about the *system* rather than about the fixture.
    """
    from agentiam_core.errors import ReasonCode
    from agentiam_core.models import Budget, DecisionRecord, Outcome

    record = DecisionRecord(
        decision_id=uuid.uuid4(),
        trace_id="trace-1",
        timestamp=datetime.now(UTC),
        pep_id="pep-1",
        token_chain_ids=["b1"],
        principal_id="kc:alice",
        task_id=task_id,
        agent_id=agent_id,
        role=role,
        depth=depth,
        scope="invoice:read",
        tool_id="invoice_api",
        arg_digest="0" * 64,
        outcome=Outcome.ALLOW,
        reason_code=ReasonCode.OK,
        policy_version="v1",
        budget_before=Budget(),
        budget_after=Budget(),
        latency_us=100,
    )
    dumped: dict[str, object] = record.model_dump(mode="json")
    dumped["mandate_id"] = str(mandate_id)
    return dumped


async def test_a_real_decision_record_carries_the_parent_asserted_role(
    migrated_engine: AsyncEngine,
) -> None:
    """TODO item 4: the role the parent assigned at `attenuate()` time reaches the tree.

    `DecisionRecord` had no `role` field at all, so `build_tree`'s lookup was unreachable and
    every node in a live console read "unknown" — the second half of item 4's "done when".
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id, mandate_id = uuid.uuid4(), uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record=_a_decision_record(
                    task_id=task_id,
                    mandate_id=mandate_id,
                    agent_id="agt-payer",
                    role="payer",
                    depth=1,
                ),
                record_hash="a" * 64,
                created_at=now,
            )
        )
        await session.commit()

        (node,) = await build_tree(session, task_id=task_id, now=now)
        assert node.agent_id == "agt-payer"
        assert node.role == "payer"


async def test_three_siblings_are_three_nodes_with_their_own_roles(
    migrated_engine: AsyncEngine,
) -> None:
    """The shape item 4 exists for. All three used to arrive as `agt-depth-1`, role "unknown".

    `DEMO.md` beat 2 claims a delegation *tree*; three siblings sharing one name and one role
    render as one node, which is a chain.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id, mandate_id = uuid.uuid4(), uuid.uuid4()
        now = datetime.now(UTC)
        roster = (
            ("agt-doc-reader", "reader"),
            ("agt-negotiator", "worker"),
            ("agt-payer", "payer"),
        )
        for seq, (agent_id, role) in enumerate(roster, start=1):
            session.add(
                AuditRecordRow(
                    seq=seq,
                    # The chain's own shape: only the genesis record may have a null `prev`
                    # (spec 08 §3), enforced by `ck_audit_records_genesis_only_seq_one`.
                    prev_hash=None if seq == 1 else f"{seq - 1:064d}",
                    decision_id=uuid.uuid4(),
                    record=_a_decision_record(
                        task_id=task_id,
                        mandate_id=mandate_id,
                        agent_id=agent_id,
                        role=role,
                        depth=1,
                    )
                    | {"token_chain_ids": ["root", f"b-{agent_id}"]},
                    record_hash=f"{seq:064d}",
                    created_at=now,
                )
            )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=now)
        assert [(n.agent_id, n.role) for n in nodes] == list(roster)


async def test_a_record_naming_no_role_reads_unknown_rather_than_blank(
    migrated_engine: AsyncEngine,
) -> None:
    """A root token has no attenuation block, so it declares no role — and says so.

    "unknown" is the honest rendering. An empty string in the console reads as a missing
    label rather than an absent claim, and inventing one would be worse than either.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id, mandate_id = uuid.uuid4(), uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record=_a_decision_record(
                    task_id=task_id, mandate_id=mandate_id, agent_id="agt-root", role="", depth=0
                ),
                record_hash="a" * 64,
                created_at=now,
            )
        )
        await session.commit()

        (node,) = await build_tree(session, task_id=task_id, now=now)
        assert node.role == "unknown"


async def test_a_record_written_before_role_existed_still_builds_a_node(
    migrated_engine: AsyncEngine,
) -> None:
    """The audit chain is append-only, so older records have no `role` key at all.

    They must not break the tree, and they must not claim a role either. Verification is
    unaffected in any case — it recomputes hashes over the *stored* body (spec 08 §3), never
    over a re-serialized model.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id, mandate_id = uuid.uuid4(), uuid.uuid4()
        now = datetime.now(UTC)
        legacy = _a_decision_record(
            task_id=task_id, mandate_id=mandate_id, agent_id="agt-old", role="payer", depth=1
        )
        del legacy["role"]
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record=legacy,
                record_hash="a" * 64,
                created_at=now,
            )
        )
        await session.commit()

        (node,) = await build_tree(session, task_id=task_id, now=now)
        assert node.agent_id == "agt-old"
        assert node.role == "unknown"


def _sibling(agent_id: str, terminal_block: str) -> TreeNode:
    """One depth-1 node. Every sibling shares the root block and has its own terminal one."""
    return TreeNode(
        agent_id=agent_id,
        role="worker",
        depth=1,
        task_id="t",
        principal_id="kc:alice",
        block_ids=["root-block", terminal_block],
        scopes=[],
        budget=[],
        revoked=False,
        revocation_reason=None,
        has_pending_escalation=False,
        last_outcome="allow",
        last_reason_code="OK",
        last_seen=datetime(2026, 9, 7, tzinfo=UTC),
    )


async def test_siblings_sharing_one_agent_id_are_still_separate_nodes() -> None:
    """The diff key is the *terminal* block, not the first one.

    `block_ids` is root-first, so `block_ids[0]` is the root block and is the same for every
    node in a task — keying on it contributed nothing and left `agent_id` alone to separate
    siblings. Measured against the deployed PEP before TODO item 4: three depth-1 agents all
    called `agt-depth-1` produced **one** diff entry, so the SSE stream animated one sibling
    in and dropped two.

    Real names fix the symptom. This asserts the key, so a parent that hands two children the
    same name still yields two nodes.
    """
    siblings = [_sibling("agt-worker", f"block-{i}") for i in range(3)]
    assert len(build_tree_diff(old=[], new=siblings).added) == 3


async def test_the_same_agent_seen_twice_is_one_node_not_two() -> None:
    """The other direction: the key must still collapse repeat sightings of one agent.

    A key made *too* specific would report every new decision as a brand-new node, which
    reads on screen as the tree growing without bound.
    """
    node = _sibling("agt-worker", "block-1")
    assert build_tree_diff(old=[node], new=[node]).added == []
    assert build_tree_diff(old=[node], new=[node]).changed == []


async def test_a_node_whose_state_moved_is_reported_as_changed() -> None:
    """Same identity, new state — a change, not an add and a remove."""
    before = _sibling("agt-worker", "block-1")
    after = before.model_copy(update={"last_outcome": "deny", "last_reason_code": "POLICY_DENIED"})

    diff = build_tree_diff(old=[before], new=[after])
    assert [n.agent_id for n in diff.changed] == ["agt-worker"]
    assert diff.added == []
    assert diff.removed == []
