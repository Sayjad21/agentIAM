"""An approved escalation reaches the agent that asked — APICTA-03.

End to end against real Postgres: a real `Pipeline` refuses a payment with
`APPROVAL_REQUIRED` and opens the escalation through the real `LedgerEscalationSink`; a real
control-plane router approves it under a session; the requesting agent collects the
elevated token from `GET /v1/escalations/{id}/status` with the token it was refused under.

The pipeline is the unit suite's (`tests/unit/test_pep_pipeline.py`) with only the sink
swapped, so the PEP side of this test is the same code the unit tests already pin. What is
new here is that both halves agree through a database: the mandate and the delegation path
the PEP wrote are the ones the approval mints under, and the token it mints is one the PEP
then accepts as the same principal.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.escalation_sink import LedgerEscalationSink
from agentiam_controlplane.db.revocations import revoke
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.attenuation import attenuate
from agentiam_core.datalog import chain_identity
from agentiam_core.errors import ReasonCode
from agentiam_core.models import RequiresApproval, ScopeSubset
from agentiam_core.tokens import mint_root, verify
from agentiam_pep.pipeline import Authorized, Refused
from tests.integration.test_escalations_api import _session_cookie
from tests.unit.test_pep_pipeline import KEY_SET, NOW, ROOT, a_mandate, a_pipeline

pytestmark = pytest.mark.integration

_SETTINGS = ControlPlaneSettings(
    root_private_key=ROOT.private_key,
    root_public_key=ROOT.public_key,
    approvers=frozenset({"kc:tanvir", "kc:farhana"}),
    session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
)
_PATH = ("agt-treasury", "agt-bulk-buyer")
_PAYMENT = b'{"amount": "75000.0000", "recipient": {"account_id": "acct_supplier"}}'


def _chain(mandate_id: uuid.UUID, task_id: uuid.UUID, *, leaf: str) -> str:
    """A root → `agt-treasury` → `leaf` chain under one mandate, as the demo builds it."""
    mandate = a_mandate(mandate_id=mandate_id, task_id=task_id, principal_id="kc:farhana")
    root = verify(mint_root(mandate, ROOT.private_key), KEY_SET, now=NOW)
    treasury = attenuate(
        root,
        [ScopeSubset(scopes=frozenset({"invoice:read", "payment:initiate"}))],
        agent_id="agt-treasury",
        role="payer",
    )
    return attenuate(
        verify(treasury, KEY_SET, now=NOW),
        [ScopeSubset(scopes=frozenset({"payment:initiate"}))],
        agent_id=leaf,
        role="payer",
    )


async def _client(engine: AsyncEngine, *, signed_in_as: str | None = None) -> httpx.AsyncClient:
    app = create_app(
        session_factory=make_session_factory(engine),
        escalation_settings=_SETTINGS,
        now=lambda: NOW,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    if signed_in_as is not None:
        client.cookies.set("session", _session_cookie(signed_in_as))
    return client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _refused_payment(engine: AsyncEngine) -> tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]:
    """Drive a real PEP refusal; return the escalation id, the agent's token, mandate, task."""
    mandate_id, task_id = uuid.uuid4(), uuid.uuid4()
    token = _chain(mandate_id, task_id, leaf="agt-bulk-buyer")
    pipeline, _, _ = await a_pipeline(
        caveats=(RequiresApproval(scopes=frozenset({"payment:initiate"})),),
        escalation_sink=LedgerEscalationSink(make_session_factory(engine)),
    )
    result = await pipeline.authorize(
        method="POST",
        path="/payments",
        headers=[("authorization", f"Bearer {token}")],
        body=_PAYMENT,
    )
    assert isinstance(result, Refused)
    assert result.reason_code is ReasonCode.APPROVAL_REQUIRED
    assert result.escalation_id is not None
    return result.escalation_id, token, mandate_id, task_id


async def test_the_requesting_agent_collects_the_grant_after_approval(
    migrated_engine: AsyncEngine,
) -> None:
    escalation_id, token, mandate_id, task_id = await _refused_payment(migrated_engine)

    async with await _client(migrated_engine) as agent:
        pending = await agent.get(f"/v1/escalations/{escalation_id}/status", headers=_bearer(token))
    assert pending.status_code == 200
    assert pending.json()["state"] == "pending"
    assert pending.json()["amount"] == "75000.0000"
    assert "elevated_token" not in pending.json()

    async with await _client(migrated_engine, signed_in_as="kc:tanvir") as tanvir:
        approved = await tanvir.post(
            f"/v1/escalations/{escalation_id}/approve",
            json={"max_amount": "50000", "elevation_ttl_s": 300},
        )
    assert approved.status_code == 200

    async with await _client(migrated_engine) as agent:
        status = await agent.get(f"/v1/escalations/{escalation_id}/status", headers=_bearer(token))
    assert status.status_code == 200
    body = status.json()
    assert body["state"] == "approved"
    assert body["elevated_token"] == approved.json()["elevated_token"]
    assert Decimal(body["amount"]) == Decimal(50000)
    assert body["scopes"] == ["payment:initiate"]

    # Minted under the task's own mandate, standing where the requester stood.
    elevated = verify(body["elevated_token"], KEY_SET, now=NOW + timedelta(seconds=1))
    assert elevated.mandate_id == mandate_id
    assert elevated.task_id == task_id
    assert elevated.budget.spend_bdt == Decimal(50000)
    assert chain_identity(elevated).path == _PATH


async def test_the_pep_accepts_the_elevated_token_up_to_the_grant(
    migrated_engine: AsyncEngine,
) -> None:
    """The token is not only verifiable: a PEP with no approval caveat lets it pay."""
    escalation_id, _, _, _ = await _refused_payment(migrated_engine)
    async with await _client(migrated_engine, signed_in_as="kc:tanvir") as tanvir:
        approved = await tanvir.post(
            f"/v1/escalations/{escalation_id}/approve", json={"max_amount": "50000"}
        )
    elevated = approved.json()["elevated_token"]

    pipeline, _, _ = await a_pipeline(granted=Decimal(100000))
    result = await pipeline.authorize(
        method="POST",
        path="/payments",
        headers=[("authorization", f"Bearer {elevated}")],
        body=b'{"amount": "50000.0000", "recipient": {"account_id": "acct_supplier"}}',
    )
    assert isinstance(result, Authorized)


async def test_another_agent_of_the_same_task_gets_404(migrated_engine: AsyncEngine) -> None:
    escalation_id, _, mandate_id, task_id = await _refused_payment(migrated_engine)
    sibling = _chain(mandate_id, task_id, leaf="agt-auditor")

    async with await _client(migrated_engine, signed_in_as="kc:tanvir") as tanvir:
        await tanvir.post(f"/v1/escalations/{escalation_id}/approve", json={})
    async with await _client(migrated_engine) as other:
        result = await other.get(
            f"/v1/escalations/{escalation_id}/status", headers=_bearer(sibling)
        )
    assert result.status_code == 404
    assert "elevated_token" not in result.text


async def test_the_requester_s_parent_gets_404(migrated_engine: AsyncEngine) -> None:
    """The parent could mint the child's path itself, but not hold the child's own token."""
    escalation_id, _, mandate_id, task_id = await _refused_payment(migrated_engine)
    mandate = a_mandate(mandate_id=mandate_id, task_id=task_id, principal_id="kc:farhana")
    parent = attenuate(
        verify(mint_root(mandate, ROOT.private_key), KEY_SET, now=NOW),
        [ScopeSubset(scopes=frozenset({"invoice:read", "payment:initiate"}))],
        agent_id="agt-treasury",
        role="payer",
    )
    async with await _client(migrated_engine) as other:
        result = await other.get(f"/v1/escalations/{escalation_id}/status", headers=_bearer(parent))
    assert result.status_code == 404


async def test_an_unknown_id_and_someone_else_s_id_are_the_same_404(
    migrated_engine: AsyncEngine,
) -> None:
    escalation_id, token, _, _ = await _refused_payment(migrated_engine)
    stranger = _chain(uuid.uuid4(), uuid.uuid4(), leaf="agt-bulk-buyer")
    async with await _client(migrated_engine) as client:
        foreign = await client.get(
            f"/v1/escalations/{escalation_id}/status", headers=_bearer(stranger)
        )
        unknown = await client.get(f"/v1/escalations/{uuid.uuid4()}/status", headers=_bearer(token))
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json().keys() == unknown.json().keys()


async def test_an_unauthenticated_caller_gets_401(migrated_engine: AsyncEngine) -> None:
    escalation_id, _, _, _ = await _refused_payment(migrated_engine)
    async with await _client(migrated_engine) as anon:
        missing = await anon.get(f"/v1/escalations/{escalation_id}/status")
        garbage = await anon.get(
            f"/v1/escalations/{escalation_id}/status", headers=_bearer("not-a-biscuit")
        )
    assert missing.status_code == 401
    assert garbage.status_code == 401


async def test_a_revoked_agent_cannot_collect_its_grant(migrated_engine: AsyncEngine) -> None:
    """TM-28: a revoked agent cannot collect the grant it asked for.

    The elevated token is its own root and survives revoking the requester's chain, so this
    route is where a killed agent must be stopped.
    """
    escalation_id, token, _, _ = await _refused_payment(migrated_engine)
    async with await _client(migrated_engine, signed_in_as="kc:tanvir") as tanvir:
        await tanvir.post(f"/v1/escalations/{escalation_id}/approve", json={})

    leaf_block = verify(token, KEY_SET, now=NOW).revocation_ids[-1]
    async with make_session_factory(migrated_engine)() as session:
        await revoke(
            session,
            block_id=leaf_block,
            scope="token",
            reason="killed mid-task",
            revoked_by="kc:tanvir",
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )
    async with await _client(migrated_engine) as agent:
        result = await agent.get(f"/v1/escalations/{escalation_id}/status", headers=_bearer(token))
    assert result.status_code == 401
    assert "elevated_token" not in result.text


async def test_the_task_s_own_principal_still_cannot_approve(migrated_engine: AsyncEngine) -> None:
    """Separation of duties (EC-A08) is unchanged by delivery: Farhana owns this task."""
    escalation_id, token, _, _ = await _refused_payment(migrated_engine)
    async with await _client(migrated_engine, signed_in_as="kc:farhana") as farhana:
        result = await farhana.post(f"/v1/escalations/{escalation_id}/approve", json={})
    assert result.status_code == 403

    async with await _client(migrated_engine) as agent:
        status = await agent.get(f"/v1/escalations/{escalation_id}/status", headers=_bearer(token))
    assert status.json()["state"] == "pending"


async def test_a_denial_reaches_the_agent_with_its_reason(migrated_engine: AsyncEngine) -> None:
    escalation_id, token, _, _ = await _refused_payment(migrated_engine)
    async with await _client(migrated_engine, signed_in_as="kc:tanvir") as tanvir:
        await tanvir.post(
            f"/v1/escalations/{escalation_id}/deny", json={"reason": "supplier not vetted"}
        )
    async with await _client(migrated_engine) as agent:
        status = await agent.get(f"/v1/escalations/{escalation_id}/status", headers=_bearer(token))
    assert status.json()["state"] == "denied"
    assert status.json()["reason"] == "supplier not vetted"
    assert "elevated_token" not in status.json()
