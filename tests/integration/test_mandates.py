"""Mandates as rows, and a budget that must belong to one — APICTA-02 Part B, gap 7.

`budgets.mandate_id` carried no foreign key because no `mandates` table existed (ADR-014):
nothing stopped a budget being created for a mandate nobody issued. Migration `0010` adds the
table, backfills a row for every mandate an existing budget names, and then adds the key.
`db.mandates.create` is how a mandate is issued from now on: the mandate and its spend pool in
one transaction, then the `mandate:created` event that tells every PEP to get ready for it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db import mandates
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.models import BudgetRow, MandateRow
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.tokens import generate_keypair
from tests.integration.conftest import alembic_config, make_budget
from tests.integration.test_escalations_api import _session_cookie

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


class RecordingPublisher:
    """Collects what would have gone to Redis."""

    def __init__(self, *, fail: bool = False) -> None:
        """Record every publish, or raise on each when `fail`."""
        self.published: list[tuple[str, str]] = []
        self.fail = fail

    async def publish(self, channel: str, message: str) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.published.append((channel, message))


async def _issue(engine: AsyncEngine, **over: Any) -> MandateRow:
    fields: dict[str, Any] = {
        "mandate_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "principal_id": "kc:farhana",
        "spend_total": Decimal("15000.0000"),
        "expires_at": NOW + timedelta(hours=8),
        "now": NOW,
    } | over
    async with make_session_factory(engine)() as session:
        return await mandates.create(session, **fields)


class TestTheMigration:
    def test_existing_budgets_are_backfilled_and_the_key_then_holds(
        self, postgres_url: str
    ) -> None:
        """Upgrading a ledger that already has budgets must not fail on its own new key."""
        cfg = alembic_config(postgres_url)
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "0009")
        legacy = uuid.uuid4()

        async def seed_and_check() -> None:
            engine = create_async_engine(postgres_url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO budgets (id, mandate_id, dimension, total, "
                            "committed, leased, allocated, version) "
                            "VALUES (:id, :m, 'spend_bdt', 100, 0, 0, 0, 0)"
                        ),
                        {"id": uuid.uuid4(), "m": legacy},
                    )
            finally:
                await engine.dispose()

        import asyncio

        asyncio.run(seed_and_check())
        command.upgrade(cfg, "0010")

        async def backfilled() -> list[uuid.UUID]:
            engine = create_async_engine(postgres_url)
            try:
                async with engine.connect() as conn:
                    rows = await conn.execute(text("SELECT mandate_id FROM mandates"))
                    return [r[0] for r in rows]
            finally:
                await engine.dispose()

        assert legacy in asyncio.run(backfilled())
        command.downgrade(cfg, "0009")
        command.upgrade(cfg, "head")

    async def test_a_budget_for_an_unissued_mandate_is_refused(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with make_session_factory(migrated_engine)() as session:
            session.add(BudgetRow(mandate_id=uuid.uuid4(), dimension="spend_bdt", total=1))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_the_test_helper_still_creates_budgets(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """`make_budget` issues the mandate row it needs — every integration test uses it."""
        mandate_id = uuid.uuid4()
        await make_budget(migrated_engine, mandate_id=mandate_id)
        async with make_session_factory(migrated_engine)() as session:
            assert await session.get(MandateRow, mandate_id) is not None


class TestIssuingAMandate:
    async def test_the_mandate_and_its_spend_pool_are_written_together(
        self, migrated_engine: AsyncEngine
    ) -> None:
        issued = await _issue(migrated_engine)
        async with make_session_factory(migrated_engine)() as session:
            row = await session.get(MandateRow, issued.mandate_id)
            budget = (
                await session.execute(
                    select(BudgetRow).where(BudgetRow.mandate_id == issued.mandate_id)
                )
            ).scalar_one()
        assert row is not None
        assert row.principal_id == "kc:farhana"
        assert budget.dimension == "spend_bdt"
        assert budget.total == Decimal("15000.0000")
        assert budget.parent_budget_id is None

    async def test_issuing_the_same_mandate_twice_is_refused_and_writes_nothing_more(
        self, migrated_engine: AsyncEngine
    ) -> None:
        issued = await _issue(migrated_engine)
        with pytest.raises(mandates.MandateExistsError):
            await _issue(migrated_engine, mandate_id=issued.mandate_id)
        async with make_session_factory(migrated_engine)() as session:
            count = (
                await session.execute(
                    select(BudgetRow).where(BudgetRow.mandate_id == issued.mandate_id)
                )
            ).all()
        assert len(count) == 1

    async def test_a_ceiling_that_is_not_positive_is_refused(
        self, migrated_engine: AsyncEngine
    ) -> None:
        with pytest.raises(ValueError, match="positive"):
            await _issue(migrated_engine, spend_total=Decimal(0))

    async def test_the_event_is_published_after_the_commit(
        self, migrated_engine: AsyncEngine
    ) -> None:
        publisher = RecordingPublisher()
        issued = await _issue(migrated_engine, publisher=publisher)
        ((channel, message),) = publisher.published
        assert channel == mandates.CHANNEL == "agentiam:mandates"
        assert json.loads(message) == {
            "event": "mandate:created",
            "mandate_id": str(issued.mandate_id),
        }

    async def test_a_redis_outage_does_not_undo_the_mandate(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Persist, then best-effort publish — the revocation rule (spec 07 §5.1).

        A PEP that missed the event creates the pool when the mandate's first token arrives
        (ADR-078), so a lost publish costs one retryable refusal, not the mandate.
        """
        issued = await _issue(migrated_engine, publisher=RecordingPublisher(fail=True))
        async with make_session_factory(migrated_engine)() as session:
            assert await session.get(MandateRow, issued.mandate_id) is not None

    async def test_ensure_row_is_idempotent(self, migrated_engine: AsyncEngine) -> None:
        mandate_id = uuid.uuid4()
        async with make_session_factory(migrated_engine)() as session, session.begin():
            await mandates.ensure_row(session, mandate_id, now=NOW)
            await mandates.ensure_row(session, mandate_id, now=NOW)
        async with make_session_factory(migrated_engine)() as session:
            assert await session.get(MandateRow, mandate_id) is not None


_ROUTE_SETTINGS = ControlPlaneSettings(
    root_private_key=generate_keypair().private_key,
    approvers=frozenset({"kc:farhana", "kc:tanvir"}),
    session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
    operator_tokens={"op-secret": "kc:tanvir"},
)


def _client(
    engine: AsyncEngine, publisher: RecordingPublisher | None = None, *, as_: str | None = None
) -> httpx.AsyncClient:
    app = create_app(
        session_factory=make_session_factory(engine),
        escalation_settings=_ROUTE_SETTINGS,
        mandate_publisher=publisher,
        now=lambda: NOW,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    if as_ is not None:
        client.cookies.set("session", _session_cookie(as_))
    return client


class TestTheRoute:
    """`POST /v1/mandates` — issuing a mandate is allocating the organization's money."""

    async def test_an_approver_issues_one_and_it_is_announced(
        self, migrated_engine: AsyncEngine
    ) -> None:
        publisher = RecordingPublisher()
        async with _client(migrated_engine, publisher, as_="kc:farhana") as client:
            created = await client.post("/v1/mandates", json={"spend_bdt": "15000"})
        assert created.status_code == 201
        body = created.json()
        assert body["principal_id"] == "kc:farhana"
        assert Decimal(body["spend_bdt"]) == Decimal(15000)
        mandate_id = uuid.UUID(body["mandate_id"])
        assert json.loads(publisher.published[0][1])["mandate_id"] == str(mandate_id)
        async with make_session_factory(migrated_engine)() as session:
            assert await session.get(MandateRow, mandate_id) is not None

    async def test_the_principal_is_the_caller_not_the_body(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine, as_="kc:farhana") as client:
            created = await client.post(
                "/v1/mandates", json={"spend_bdt": "100", "principal_id": "kc:tanvir"}
            )
        assert created.json()["principal_id"] == "kc:farhana"

    async def test_an_operator_token_works_from_a_script(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine) as client:
            created = await client.post(
                "/v1/mandates",
                json={"spend_bdt": "100"},
                headers={"Authorization": "Bearer op-secret"},
            )
        assert created.status_code == 201
        assert created.json()["principal_id"] == "kc:tanvir"

    async def test_no_credential_is_401(self, migrated_engine: AsyncEngine) -> None:
        async with _client(migrated_engine) as client:
            refused = await client.post("/v1/mandates", json={"spend_bdt": "100"})
        assert refused.status_code == 401

    async def test_someone_who_is_not_an_approver_is_403(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine, as_="kc:nusrat") as client:
            refused = await client.post("/v1/mandates", json={"spend_bdt": "100"})
        assert refused.status_code == 403

    @pytest.mark.parametrize("spend", ["0", "-5", "12.34567", "lots"])
    async def test_a_ceiling_that_is_not_a_positive_amount_is_422(
        self, migrated_engine: AsyncEngine, spend: str
    ) -> None:
        async with _client(migrated_engine, as_="kc:farhana") as client:
            refused = await client.post("/v1/mandates", json={"spend_bdt": spend})
        assert refused.status_code == 422
