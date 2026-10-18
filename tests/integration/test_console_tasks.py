"""A task started from the console is its own mandate, with its own pool — APICTA-02 Part C.

`POST /run/task` used to mint every task under `DEMO_MANDATE_ID`: the ceiling the judge typed
bound the token, and the spend came out of the demo's pool (gap 25). Now the console issues a
mandate (`db.mandates.create`) whose pool *is* that ceiling, and mints the task under it. That
turns a form into something that creates budget, so it takes what `POST /v1/mandates` takes: a
signed-in approver, who becomes the task's principal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.models import BudgetRow, MandateRow
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.tokens import RootKeySet, generate_keypair, verify
from tests.integration.test_escalations_api import _session_cookie
from tests.integration.test_mandates import RecordingPublisher

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
_KEYS = generate_keypair()
_SETTINGS = ControlPlaneSettings(
    root_private_key=_KEYS.private_key,
    root_public_key=_KEYS.public_key,
    approvers=frozenset({"kc:farhana", "kc:tanvir"}),
    session_secret_key="test-session-secret",  # noqa: S106 — throwaway test signing key
    pep_url="http://pep",
)
_FORM = {
    "description": "Procure 500 units of packaging stock.",
    "budget": "15000",
    "scopes": ["invoice:read", "payment:initiate"],
}


def _console(
    engine: AsyncEngine, publisher: RecordingPublisher | None = None, *, as_: str | None = None
) -> tuple[httpx.AsyncClient, object]:
    app = create_app(
        session_factory=make_session_factory(engine),
        escalation_settings=_SETTINGS,
        mandate_publisher=publisher,
        now=lambda: NOW,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    if as_ is not None:
        client.cookies.set("session", _session_cookie(as_))
    return client, app


async def _mandates(engine: AsyncEngine) -> list[MandateRow]:
    async with make_session_factory(engine)() as session:
        return list((await session.execute(select(MandateRow))).scalars())


async def test_an_approver_s_task_gets_its_own_mandate_and_pool(
    migrated_engine: AsyncEngine,
) -> None:
    before = {m.mandate_id for m in await _mandates(migrated_engine)}
    publisher = RecordingPublisher()
    client, app = _console(migrated_engine, publisher, as_="kc:farhana")
    async with client:
        page = await client.post("/run/task", data=_FORM)
    assert page.status_code == 200

    (issued,) = [m for m in await _mandates(migrated_engine) if m.mandate_id not in before]
    assert issued.principal_id == "kc:farhana"
    async with make_session_factory(migrated_engine)() as session:
        pool = (
            await session.execute(
                select(BudgetRow).where(BudgetRow.mandate_id == issued.mandate_id)
            )
        ).scalar_one()
    assert pool.total == Decimal("15000.0000")
    assert json.loads(publisher.published[0][1])["mandate_id"] == str(issued.mandate_id)

    registry = app.state.task_registry  # type: ignore[attr-defined]
    (task,) = registry.list_tasks()
    assert task.mandate_id == issued.mandate_id
    assert task.task_id == issued.task_id
    assert task.principal_id == "kc:farhana"
    verified = verify(task.root.token, RootKeySet([_KEYS.public_key]), now=NOW)
    assert verified.mandate_id == issued.mandate_id
    assert verified.budget.spend_bdt == Decimal("15000")
    # Beat 1: the page shows the intent hash the root token binds.
    assert verified.intent_hash in page.text


async def test_a_task_is_not_started_without_a_signed_in_approver(
    migrated_engine: AsyncEngine,
) -> None:
    before = len(await _mandates(migrated_engine))
    for who in (None, "kc:nusrat"):
        client, app = _console(migrated_engine, as_=who)
        async with client:
            page = await client.post("/run/task", data=_FORM)
        assert page.status_code == 200
        assert "approver" in page.text
        assert app.state.task_registry.list_tasks() == []  # type: ignore[attr-defined]
    assert len(await _mandates(migrated_engine)) == before


async def test_the_principal_is_the_signed_in_approver_not_the_form(
    migrated_engine: AsyncEngine,
) -> None:
    client, app = _console(migrated_engine, as_="kc:tanvir")
    async with client:
        await client.post("/run/task", data=_FORM | {"principal_id": "kc:farhana"})
    (task,) = app.state.task_registry.list_tasks()  # type: ignore[attr-defined]
    assert task.principal_id == "kc:tanvir"


@pytest.mark.parametrize(
    "bad",
    [{"description": "  "}, {"budget": "0"}, {"budget": "lots"}, {"scopes": []}],
)
async def test_a_malformed_task_issues_no_mandate(
    migrated_engine: AsyncEngine, bad: dict[str, object]
) -> None:
    """Validated before issuing: a typo in the form must not create an orphan pool."""
    before = len(await _mandates(migrated_engine))
    client, _ = _console(migrated_engine, as_="kc:farhana")
    async with client:
        page = await client.post("/run/task", data=_FORM | bad)
    assert page.status_code == 200
    assert 'class="alert' in page.text
    assert len(await _mandates(migrated_engine)) == before


async def test_a_call_refused_while_the_pool_is_created_is_retried_once(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket's "the run page retries once".

    A new mandate's first request may arrive before any PEP has its pool, and is refused
    `LEASE_UNAVAILABLE` (ADR-078).
    """
    answers = iter(
        [
            httpx.Response(429, json={"reason_code": "LEASE_UNAVAILABLE"}),
            httpx.Response(200, json={"status": "accepted"}),
        ]
    )
    sent: list[httpx.Request] = []

    def pep(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return next(answers)

    real = httpx.AsyncClient

    def through_fake_pep(**kw: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(pep), **kw)  # type: ignore[arg-type]

    client, app = _console(migrated_engine, as_="kc:farhana")
    async with client:
        await client.post("/run/task", data=_FORM)
        (task,) = app.state.task_registry.list_tasks()  # type: ignore[attr-defined]
        monkeypatch.setattr(httpx, "AsyncClient", through_fake_pep)
        page = await client.post(
            "/run/call",
            data={"task_id": str(task.task_id), "agent_id": "root", "action": "pay"},
        )
    assert len(sent) == 2
    assert "200 OK" in page.text
