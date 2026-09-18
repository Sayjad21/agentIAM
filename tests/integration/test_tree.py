import uuid
from datetime import UTC, datetime, timedelta
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
                # Strictly in the future: an escalation whose TTL runs out exactly now is
                # already past being actionable, which `test_an_expired_escalation_*` pins.
                expires_at=now + timedelta(minutes=15),
                state=EscalationState.PENDING.value,
            )
        )
        await session.commit()
        nodes = await build_tree(session, task_id=task_id, now=now)
        assert nodes[0].has_pending_escalation is True


async def test_an_expired_escalation_does_not_flag_the_node(
    migrated_engine: AsyncEngine,
) -> None:
    """Nothing sweeps `state` when a TTL lapses, so the column alone says `'pending'` forever.

    `escalations.list_by_state` already excludes those from the queue. The tree did not, so
    the console showed an `ESCALATING` badge on agents whose request had expired days
    earlier while `/v1/escalations` reported an empty queue — two screens disagreeing.
    """
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
                reason="expired an hour ago, never swept",
                created_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
                # The point of the test: still literally 'pending' in the column.
                state=EscalationState.PENDING.value,
            )
        )
        await session.commit()
        nodes = await build_tree(session, task_id=task_id, now=now)
        assert nodes[0].has_pending_escalation is False


def _authority_record(
    *,
    task_id: uuid.UUID,
    seq: int,
    agent_id: str,
    scopes: list[str],
    ceilings: dict[str, str],
    requested_scope: str,
    outcome: str = "allow",
    spend_before: str = "0",
    spend_after: str = "0",
) -> AuditRecordRow:
    """One decision record shaped the way the deployed PEP writes them.

    The shape matters more than the values: `scope` is the single scope this request asked
    for, while `authority` carries what the token actually permits, and the two disagree on
    every refusal. Taken from a real record off the demo stack rather than invented.
    """
    return AuditRecordRow(
        seq=seq,
        decision_id=uuid.uuid4(),
        record={
            "task_id": str(task_id),
            "agent_id": agent_id,
            "depth": 1,
            "principal_id": "kc:1111",
            "token_chain_ids": ["b1", f"b-{agent_id}"],
            "scope": requested_scope,
            "outcome": outcome,
            "reason_code": "OK" if outcome == "allow" else "SCOPE_ATTENUATED_AWAY",
            "role": "reader",
            "authority": {"scopes": scopes, "ceilings": ceilings},
            "budget_before": {"spend_bdt": spend_before},
            "budget_after": {"spend_bdt": spend_after},
        },
        # Only the genesis record may carry no predecessor — `audit_records` has a check
        # constraint saying so, because a later record with a NULL `prev_hash` would verify
        # as a fresh genesis and hide every record before it.
        prev_hash=None if seq == 1 else f"{seq - 1:064d}",
        record_hash=f"{seq:064d}",
        created_at=datetime.now(UTC),
    )


async def test_scopes_are_the_authority_not_the_scope_that_was_refused(
    migrated_engine: AsyncEngine,
) -> None:
    """The defect this pins put `payment:initiate` on the one agent that may never pay.

    `agt-doc-reader` holds reads only, and its last call on the demo stack was the payment
    it was refused. Reading `scope` off that record advertised the refused scope as held —
    the node claimed authority the token exists to deny, which is the worst direction for
    this screen to be wrong in.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        session.add(
            _authority_record(
                task_id=task_id,
                seq=1,
                agent_id="agt-doc-reader",
                scopes=["invoice:read", "vendor:read"],
                ceilings={"spend_bdt": "0"},
                requested_scope="payment:initiate",
                outcome="deny",
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert nodes[0].scopes == ["invoice:read", "vendor:read"]
        assert "payment:initiate" not in nodes[0].scopes


async def test_a_record_carrying_no_authority_reads_no_scopes(
    migrated_engine: AsyncEngine,
) -> None:
    """Empty, not the requested scope — the same rule `_authority_view` follows.

    A record written before the PEP folded authority cannot say what the agent held. An
    empty list reads as *not measured*; the requested scope would read as a held scope.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        session.add(
            AuditRecordRow(
                seq=1,
                decision_id=uuid.uuid4(),
                record={
                    "task_id": str(task_id),
                    "agent_id": "legacy",
                    "depth": 1,
                    "token_chain_ids": ["b1"],
                    "scope": "payment:initiate",
                },
                record_hash="c" * 64,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert nodes[0].scopes == []


async def test_budget_available_is_total_minus_consumed(migrated_engine: AsyncEngine) -> None:
    """Each node carries its own ceiling, less what its own records show it spent.

    Two calls, because spend sums over the whole chain rather than being read off the
    newest record: 900 then 250 against a 25,000 ceiling leaves 23,850.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        session.add(
            _authority_record(
                task_id=task_id,
                seq=1,
                agent_id="agt-settlement",
                scopes=["payment:initiate"],
                ceilings={"spend_bdt": "25000"},
                requested_scope="payment:initiate",
                spend_before="5000.0000",
                spend_after="4100.0000",
            )
        )
        session.add(
            _authority_record(
                task_id=task_id,
                seq=2,
                agent_id="agt-settlement",
                scopes=["payment:initiate"],
                ceilings={"spend_bdt": "25000"},
                requested_scope="payment:initiate",
                spend_before="4100.0000",
                spend_after="3850.0000",
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert len(nodes[0].budget) == 1
        assert nodes[0].budget[0].dimension == "spend_bdt"
        assert nodes[0].budget[0].total == Decimal("25000")
        assert nodes[0].budget[0].available == Decimal("23850.0000")


async def test_a_refusal_consumes_nothing(migrated_engine: AsyncEngine) -> None:
    """A denied request reserves no budget, so it must not move the node's remainder."""
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        session.add(
            _authority_record(
                task_id=task_id,
                seq=1,
                agent_id="agt-settlement",
                scopes=["payment:initiate"],
                ceilings={"spend_bdt": "25000"},
                requested_scope="payment:initiate",
                outcome="deny",
                spend_before="5000.0000",
                spend_after="5000.0000",
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert nodes[0].budget[0].available == Decimal("25000")


async def test_budget_zero_total_does_not_divide(migrated_engine: AsyncEngine) -> None:
    """A ceiling of zero is a real answer, not a missing one.

    `agt-doc-reader` is granted exactly zero spend, and that is the point of it. The node
    still carries the dimension so the console can draw an empty bar rather than none; the
    console's own guard is `total <= 0`, so nothing here divides by it.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        session.add(
            _authority_record(
                task_id=task_id,
                seq=1,
                agent_id="agt-doc-reader",
                scopes=["invoice:read"],
                ceilings={"spend_bdt": "0"},
                requested_scope="invoice:read",
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert nodes[0].budget[0].total == Decimal("0")
        assert nodes[0].budget[0].available == Decimal("0")


async def test_money_leads_the_budget_list(migrated_engine: AsyncEngine) -> None:
    """The console draws each node's bar from `budget[0]`, so the order is load bearing.

    Sorted by name alone, `external_emails` leads with a ceiling of zero and every bar on
    the tree reads empty.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        session.add(
            _authority_record(
                task_id=task_id,
                seq=1,
                agent_id="agt-payer",
                scopes=["payment:initiate"],
                ceilings={
                    "external_emails": "0",
                    "rows_read": "100000",
                    "spend_bdt": "200000",
                    "tool_calls": "1000",
                    "wall_clock_s": "0",
                },
                requested_scope="payment:initiate",
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert nodes[0].budget[0].dimension == "spend_bdt"
        rest = [b.dimension for b in nodes[0].budget[1:]]
        assert rest == sorted(rest)


async def test_a_ledger_row_naming_the_agent_overrides_its_token_ceiling(
    migrated_engine: AsyncEngine,
) -> None:
    """An allocated sub-pool is the ledger's own number, and the ledger owns budget.

    The token says what the agent may spend; a budget row addressed to that agent says what
    it has actually been allocated. Where both speak, the ledger wins.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        mandate_id = uuid.uuid4()
        record = _authority_record(
            task_id=task_id,
            seq=1,
            agent_id="agt-payer",
            scopes=["payment:initiate"],
            ceilings={"spend_bdt": "200000"},
            requested_scope="payment:initiate",
        )
        record.record = {**record.record, "mandate_id": str(mandate_id)}
        session.add(record)
        # A pool and an allocation drawn from it. Both halves are required: `budgets` has a
        # check constraint that a row is one or the other and never half of each, because a
        # row with an agent but no parent is a pool wearing a name tag.
        pool_id = uuid.uuid4()
        session.add(
            BudgetRow(
                id=pool_id,
                mandate_id=mandate_id,
                dimension="spend_bdt",
                total=Decimal("200000"),
                committed=Decimal("0"),
                leased=Decimal("0"),
                allocated=Decimal("40"),
                agent_id=None,
                parent_budget_id=None,
            )
        )
        session.add(
            BudgetRow(
                id=uuid.uuid4(),
                mandate_id=mandate_id,
                dimension="spend_bdt",
                total=Decimal("40"),
                committed=Decimal("10"),
                leased=Decimal("0"),
                allocated=Decimal("0"),
                agent_id="agt-payer",
                parent_budget_id=pool_id,
            )
        )
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=datetime.now(UTC))
        assert len(nodes[0].budget) == 1
        assert nodes[0].budget[0].total == Decimal("40")
        assert nodes[0].budget[0].available == Decimal("30")


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


async def test_the_depth_zero_node_is_marked_as_the_principal(
    migrated_engine: AsyncEngine,
) -> None:
    """TODO item 18. The root is the mandate holder acting directly, not a nameless agent.

    A root token carries no attenuation block, so it declares no `agent()` and no `role()`
    (spec 01 §6.1) and the PEP falls back to `agt-depth-0` rather than inventing a name.
    Correct there; on screen, beside `agt-doc-reader`, it reads as a label that failed to
    load. `is_principal` is what lets the console say what the node actually is.
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
                    agent_id="agt-depth-0",
                    role="",
                    depth=0,
                ),
                record_hash="a" * 64,
                created_at=now,
            )
        )
        await session.commit()

        (node,) = await build_tree(session, task_id=task_id, now=now)

    assert node.is_principal
    # The audit key is untouched — records reference it, and it is what
    # `/v1/tree/{task}/blocks/{agent_id}` is looked up by.
    assert node.agent_id == "agt-depth-0"
    assert node.principal_id == "kc:alice"


async def test_a_delegated_agent_is_not_the_principal(migrated_engine: AsyncEngine) -> None:
    """Anything with an attenuation block has a name of its own, so it is named by it."""
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

    assert not node.is_principal


async def test_is_principal_cannot_disagree_with_depth() -> None:
    """It is computed, not stored, so there is no second copy to drift.

    Stored, it would be a field every hand-built `TreeNode` had to set correctly, and a
    depth-1 node marked `is_principal=True` would render a sub-agent as the mandate holder.
    """
    node = TreeNode(
        agent_id="agt-x",
        role="worker",
        depth=0,
        task_id="t",
        principal_id="kc:alice",
        block_ids=["b0"],
        scopes=[],
        budget=[],
        revoked=False,
        revocation_reason=None,
        has_pending_escalation=False,
        last_outcome="allow",
        last_reason_code="OK",
        last_seen=datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert node.is_principal
    assert not node.model_copy(update={"depth": 1}).is_principal


async def test_is_principal_reaches_the_console_over_the_wire() -> None:
    """A computed field the JSON does not carry would leave the console unable to use it.

    `computed_field` is what puts it in the dump; a plain `@property` would not, and the
    page would silently fall back to labelling the root `agt-depth-0` again.
    """
    node = TreeNode(
        agent_id="agt-depth-0",
        role="unknown",
        depth=0,
        task_id="t",
        principal_id="kc:alice",
        block_ids=["b0"],
        scopes=[],
        budget=[],
        revoked=False,
        revocation_reason=None,
        has_pending_escalation=False,
        last_outcome="allow",
        last_reason_code="OK",
        last_seen=datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert node.model_dump(mode="json")["is_principal"] is True


async def _seed_generation(
    session: object, *, task_id: uuid.UUID, root: str, seq_base: int, when: datetime
) -> None:
    """One mandate minting: a root and two children, all sharing `root` as block 0."""
    for offset, (agent, depth, chain) in enumerate(
        [
            ("agt-depth-0", 0, [root]),
            ("agt-doc-reader", 1, [root, f"{root}-reader"]),
            ("agt-payer", 1, [root, f"{root}-payer"]),
        ]
    ):
        seq = seq_base + offset
        session.add(  # type: ignore[attr-defined]
            AuditRecordRow(
                seq=seq,
                decision_id=uuid.uuid4(),
                record={
                    "task_id": str(task_id),
                    "agent_id": agent,
                    "depth": depth,
                    "principal_id": "kc:alice",
                    "token_chain_ids": chain,
                    "scope": "invoice:read",
                    "outcome": "allow",
                    "reason_code": "OK",
                },
                # `ck_audit_records_genesis_only_seq_one`: only seq 1 may have no predecessor.
                prev_hash=None if seq == 1 else f"{seq - 1:064d}",
                record_hash=f"{seq:064d}",
                created_at=when,
            )
        )


async def test_re_minting_a_mandate_does_not_duplicate_the_tree(
    migrated_engine: AsyncEngine,
) -> None:
    """Six seed runs rendered six copies of all six agents on the demo stack.

    The audit chain is append-only, so every superseded chain stays queryable forever; the
    tree is a view of what is current and must not accumulate them.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        old = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        new = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        await _seed_generation(session, task_id=task_id, root="root-old", seq_base=1, when=old)
        await _seed_generation(session, task_id=task_id, root="root-new", seq_base=10, when=new)
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=new)
        assert [n.agent_id for n in nodes] == ["agt-depth-0", "agt-doc-reader", "agt-payer"]
        assert {n.block_ids[0] for n in nodes} == {"root-new"}


async def test_superseded_generations_are_served_on_request(
    migrated_engine: AsyncEngine,
) -> None:
    """The old chains are hidden by default, never deleted — an audit store may not forget."""
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        old = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        new = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        await _seed_generation(session, task_id=task_id, root="root-old", seq_base=1, when=old)
        await _seed_generation(session, task_id=task_id, root="root-new", seq_base=10, when=new)
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=new, include_superseded=True)
        assert len(nodes) == 6
        assert {n.block_ids[0] for n in nodes} == {"root-old", "root-new"}


async def test_the_newest_generation_wins_regardless_of_insertion_order(
    migrated_engine: AsyncEngine,
) -> None:
    """`last_seen`, not `seq`, decides which chain is current.

    Inserting the superseded generation at the higher sequence numbers is what a replayed
    or backfilled record looks like; picking by `seq` would show the wrong tree.
    """
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as session:
        task_id = uuid.uuid4()
        old = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        new = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
        await _seed_generation(session, task_id=task_id, root="root-new", seq_base=1, when=new)
        await _seed_generation(session, task_id=task_id, root="root-old", seq_base=10, when=old)
        await session.commit()

        nodes = await build_tree(session, task_id=task_id, now=new)
        assert {n.block_ids[0] for n in nodes} == {"root-new"}
