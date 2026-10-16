"""An activation in the console changes what the PEP enforces — APICTA-01 Part C, gap 26.

Everything real except the network between the two halves: Postgres for the bundle store,
Redis for the push, the control plane's own app (in process, over `httpx.ASGITransport`) and a
PEP pipeline whose policy is the `PolicyCache` that `PolicySync` keeps current. The policy
published passes the full corpus — the gate is not bypassed — and forbids one agent the corpus
never mentions, so the change is visible in the next decision and nowhere else.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import itsdangerous
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from redis import asyncio as redis_asyncio

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.policy_bundles import RedisPolicyPublisher
from agentiam_controlplane.settings import ControlPlaneSettings, PolicyPublishingSettings
from agentiam_core.bundles import PolicyBundle, bundle_file_payload, sign_bundle
from agentiam_core.corpus import CORPUS_SOURCE, CORPUS_TOOLS
from agentiam_core.errors import ReasonCode
from agentiam_core.tokens import generate_keypair
from agentiam_pep.pipeline import Authorized, Refused
from agentiam_pep.policy import AgentPrincipal
from agentiam_pep.policy_cache import PolicyCache
from agentiam_pep.policy_sync import PolicySync
from tests.unit.test_pep_pipeline import a_mandate, a_pipeline, bearer

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from agentiam_core.tokens import VerifiedToken
    from agentiam_pep.pipeline import Pipeline

pytestmark = pytest.mark.integration

_SECRET = "test-session-secret"  # noqa: S105 — throwaway test signing key
_CFO = "kc:cfo"
_QUARANTINED = "agt-quarantined"
_QUARANTINE = CORPUS_SOURCE + f'\nforbid(principal == Agent::"{_QUARANTINED}", action, resource);\n'


def _cookie(principal_id: str) -> str:
    payload = base64.b64encode(json.dumps({"principal_id": principal_id}).encode())
    return itsdangerous.TimestampSigner(_SECRET).sign(payload).decode()


def _boot_files(
    tmp_path: Path, key: Ed25519PrivateKey
) -> tuple[PolicyBundle, bytes, PolicyPublishingSettings]:
    boot = PolicyBundle(
        version="demo-1",
        cedar_source=CORPUS_SOURCE,
        serial=1,
        tools=CORPUS_TOOLS,
        created_at=datetime.now(UTC).replace(microsecond=0),
    )
    signature = sign_bundle(boot, key)
    (tmp_path / "policy_bundle.json").write_text(
        json.dumps(bundle_file_payload(boot)), encoding="utf-8"
    )
    (tmp_path / "policy_bundle.sig").write_bytes(signature)
    return (
        boot,
        signature,
        PolicyPublishingSettings(
            signing_key=key,
            initial_bundle_path=tmp_path / "policy_bundle.json",
            initial_bundle_sig_path=tmp_path / "policy_bundle.sig",
        ),
    )


def _quarantined(token: VerifiedToken) -> AgentPrincipal:
    return AgentPrincipal(
        agent_id=_QUARANTINED, role="worker", principal_id=token.principal_id, task_id=token.task_id
    )


async def _read_invoice(pipeline: Pipeline) -> Authorized | Refused:
    return await pipeline.authorize(
        method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
    )


async def _until(predicate: Callable[[], bool], timeout_s: float) -> float:
    started = time.perf_counter()
    while not predicate():
        if time.perf_counter() - started > timeout_s:
            raise AssertionError(f"not reached within {timeout_s} s")
        await asyncio.sleep(0.001)
    return time.perf_counter() - started


@pytest.mark.parametrize("push", [True, False], ids=["push", "pull-only"])
async def test_an_activation_changes_the_peps_next_decision(
    migrated_engine: AsyncEngine, redis_url: str, tmp_path: Path, push: bool
) -> None:
    key = Ed25519PrivateKey.generate()
    boot, boot_signature, publishing = _boot_files(tmp_path, key)
    cp_redis = redis_asyncio.Redis.from_url(redis_url)
    pep_redis = redis_asyncio.Redis.from_url(redis_url)

    control_plane = create_app(
        session_factory=make_session_factory(migrated_engine),
        escalation_settings=ControlPlaneSettings(
            root_private_key=generate_keypair().private_key,
            approvers=frozenset({_CFO}),
            session_secret_key=_SECRET,
        ),
        policy_publishing=publishing,
        policy_publisher=RedisPolicyPublisher(cp_redis),
    )
    async with control_plane.router.lifespan_context(control_plane):
        cp_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control_plane), base_url="http://cp"
        )
        cache = PolicyCache(
            public_key=key.public_key(),
            max_staleness=timedelta(seconds=300),
            now=lambda: datetime.now(UTC),
        )
        cache.load(boot, boot_signature)
        sync = PolicySync(
            cache=cache,
            control_plane_client=cp_client,
            redis_client=pep_redis if push else None,
            # With push on, the pull is an hour away: only the event can explain a fast update.
            pull_interval_s=3600 if push else 0.2,
        )
        await sync.start()
        assert sync.last_outcome == "confirmed", "the boot bundle is the imported serial 1"
        if push:
            await asyncio.sleep(0.2)  # let the subscription establish

        pipeline, _, _ = await a_pipeline()
        pipeline._policy = cache
        pipeline._principal_for = _quarantined
        assert isinstance(await _read_invoice(pipeline), Authorized)

        cp_client.cookies.set("session", _cookie(_CFO))
        clicked = time.perf_counter()
        response = await cp_client.post("/policy/activate", data={"source": _QUARANTINE})
        published = time.perf_counter()
        assert response.status_code == 200, response.text

        waited = await _until(lambda: cache.serial == 2, timeout_s=2.0 if push else 3.0)
        click_to_enforced_ms = (published - clicked + waited) * 1000
        print(
            f"\n[{'push' if push else 'pull-only'}] activation request "
            f"{(published - clicked) * 1000:.1f} ms; "
            f"published -> PEP loaded {waited * 1000:.1f} ms; "
            f"click -> enforced {click_to_enforced_ms:.1f} ms"
        )

        refused = await _read_invoice(pipeline)
        assert isinstance(refused, Refused)
        assert refused.reason_code is ReasonCode.POLICY_DENIED
        if push:
            assert waited < 1.0, "spec 05 §5.7.6: enforced within one second by push"

        await sync.aclose()
        await cp_client.aclose()
    await cp_redis.aclose()
    await pep_redis.aclose()


async def test_an_unrelated_agent_is_unaffected(
    migrated_engine: AsyncEngine, redis_url: str, tmp_path: Path
) -> None:
    """The change is the forbid it states and nothing else (the corpus gate already says so)."""
    key = Ed25519PrivateKey.generate()
    boot, boot_signature, _ = _boot_files(tmp_path, key)
    cache = PolicyCache(
        public_key=key.public_key(),
        max_staleness=timedelta(seconds=300),
        now=lambda: datetime.now(UTC),
    )
    cache.load(boot, boot_signature)
    quarantine = PolicyBundle(
        version="bundle-2", cedar_source=_QUARANTINE, serial=2, tools=CORPUS_TOOLS
    )
    cache.load(quarantine, sign_bundle(quarantine, key))

    pipeline, _, _ = await a_pipeline()
    pipeline._policy = cache
    pipeline._principal_for = lambda token: AgentPrincipal(
        agent_id="agt-doc-reader",
        role="worker",
        principal_id=token.principal_id,
        task_id=uuid.uuid4(),
    )
    assert isinstance(await _read_invoice(pipeline), Authorized)
