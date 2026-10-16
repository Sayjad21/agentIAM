"""Publishing policy from the control plane — APICTA-01 Part B, spec 05 §5.7.

Real Postgres (the serial comes from a sequence, and the concurrency test is about that
sequence) and real HTTP through `httpx.ASGITransport`. Redis is replaced by a recording
publisher: what is under test is *when* the event is sent relative to the commit, which a
real broker would only make harder to see.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import itsdangerous
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db import policy_bundles as store
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.settings import ControlPlaneSettings, PolicyPublishingSettings
from agentiam_core.bundles import (
    BundleSignatureError,
    PolicyBundle,
    bundle_file_payload,
    bundle_from_wire,
    bundle_to_wire,
    sign_bundle,
    verify_bundle,
)
from agentiam_core.corpus import CORPUS_SOURCE, CORPUS_TOOLS
from agentiam_core.tokens import generate_keypair

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
_KEY = Ed25519PrivateKey.generate()
_CFO, _ANALYST = "kc:cfo", "kc:analyst"
_SECRET = "test-session-secret"  # noqa: S105 — throwaway test signing key
_SETTINGS = ControlPlaneSettings(
    root_private_key=generate_keypair().private_key,
    approvers=frozenset({_CFO}),
    session_secret_key=_SECRET,
)
#: A permitted change that still passes all 51 corpus cases: an extra permit for a scope the
#: corpus never exercises.
_PASSING = CORPUS_SOURCE + '\npermit(principal, action == Action::"report:read", resource);\n'
#: Removes every invoice-read permit, which the corpus expects to allow.
_FAILING = "forbid(principal, action, resource);"


#: Publication on, no boot bundle to import.
_PUBLISHING = PolicyPublishingSettings(signing_key=_KEY)


class RecordingPublisher:
    """Records each event and, at that moment, whether the row was already committed."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, fail: bool = False) -> None:
        """Remember where to look for the row."""
        self._factory = factory
        self._fail = fail
        self.events: list[tuple[int, bool]] = []

    async def publish(self, *, serial: int) -> None:
        async with self._factory() as session:
            visible = await store.get(session, serial) is not None
        self.events.append((serial, visible))
        if self._fail:
            raise ConnectionError("redis is down")


def _bundle_file(
    tmp_path: Path, *, serial: int = 1, key: Ed25519PrivateKey = _KEY
) -> PolicyPublishingSettings:
    """The deployment's initial bundle, as `bootstrap_demo_secrets.py` writes it."""
    bundle = PolicyBundle(
        version="demo-1", cedar_source=CORPUS_SOURCE, serial=serial, tools=CORPUS_TOOLS
    )
    (tmp_path / "policy_bundle.json").write_text(
        json.dumps(bundle_file_payload(bundle)), encoding="utf-8"
    )
    (tmp_path / "policy_bundle.sig").write_bytes(sign_bundle(bundle, key))
    return PolicyPublishingSettings(
        signing_key=_KEY,
        initial_bundle_path=tmp_path / "policy_bundle.json",
        initial_bundle_sig_path=tmp_path / "policy_bundle.sig",
    )


def _cookie(principal_id: str) -> str:
    payload = base64.b64encode(json.dumps({"principal_id": principal_id}).encode())
    return itsdangerous.TimestampSigner(_SECRET).sign(payload).decode()


async def _publish(
    factory: async_sessionmaker[AsyncSession], source: str = _PASSING, **kw: object
) -> store.PolicyBundleRow:
    async with factory() as session:
        return await store.publish(
            session,
            cedar_source=source,
            signing_key=_KEY,
            activated_by=_CFO,
            now=_NOW,
            **kw,  # type: ignore[arg-type]
        )


# --- the store -------------------------------------------------------------------------


class TestSerials:
    async def test_an_empty_store_starts_at_one(self, migrated_engine: AsyncEngine) -> None:
        row = await _publish(make_session_factory(migrated_engine))
        assert row.serial == 1
        assert row.version == "bundle-1"

    async def test_publishing_after_the_import_takes_the_next_serial(
        self, migrated_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        factory = make_session_factory(migrated_engine)
        settings = _bundle_file(tmp_path, serial=1)
        async with factory() as session:
            imported = await store.import_initial_from(session, settings, now=_NOW)
        assert imported is not None and imported.serial == 1
        # Every PEP boots from serial 1; a new bundle numbered 1 would be refused as a
        # rollback by all of them (spec 05 §5.2).
        assert (await _publish(factory)).serial == 2

    async def test_concurrent_publishes_get_distinct_serials(
        self, migrated_engine: AsyncEngine
    ) -> None:
        # `max(serial) + 1` would hand two of these the same serial; a sequence cannot.
        factory = make_session_factory(migrated_engine)
        rows = await asyncio.gather(*(_publish(factory) for _ in range(8)))
        assert sorted(r.serial for r in rows) == list(range(1, 9))

    async def test_a_signature_made_for_the_serial_verifies_after_the_wire(
        self, migrated_engine: AsyncEngine
    ) -> None:
        row = await _publish(make_session_factory(migrated_engine))
        wire = json.loads(json.dumps(bundle_to_wire(store.bundle_of(row), row.signature)))
        bundle, signature = bundle_from_wire(wire)
        verify_bundle(bundle, signature, _KEY.public_key())
        assert bundle.serial == row.serial
        assert bundle.created_at == _NOW


class TestWhatABundleCarries:
    async def test_the_catalogue_is_carried_forward(
        self, migrated_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        # An activation supplies Cedar only; the policy and the attributes it reads stay one
        # artifact (spec 05 §5.6).
        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            await store.import_initial_from(session, _bundle_file(tmp_path), now=_NOW)
        row = await _publish(factory)
        assert row.tools == CORPUS_TOOLS

    async def test_the_english_is_kept_only_as_a_digest(self, migrated_engine: AsyncEngine) -> None:
        row = await _publish(make_session_factory(migrated_engine), nl_digest="0123456789abcdef")
        assert row.nl_digest == "0123456789abcdef"

    async def test_a_rollback_is_a_new_serial_carrying_the_old_source(
        self, migrated_engine: AsyncEngine
    ) -> None:
        factory = make_session_factory(migrated_engine)
        first = await _publish(factory, CORPUS_SOURCE)
        await _publish(factory, _PASSING)
        async with factory() as session:
            rolled = await store.republish(
                session, source_serial=first.serial, signing_key=_KEY, activated_by=_CFO, now=_NOW
            )
        # Re-sending serial 1 would be refused by every PEP; "roll back" means publish it anew.
        assert rolled.serial == 3
        assert rolled.cedar_source == CORPUS_SOURCE
        assert rolled.source_serial == first.serial

    async def test_republishing_an_unknown_serial_raises(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with make_session_factory(migrated_engine)() as session:
            with pytest.raises(store.BundleNotFound):
                await store.republish(
                    session, source_serial=99, signing_key=_KEY, activated_by=_CFO, now=_NOW
                )


class TestImport:
    async def test_a_bundle_that_does_not_verify_is_refused(
        self, migrated_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        factory = make_session_factory(migrated_engine)
        settings = _bundle_file(tmp_path, key=Ed25519PrivateKey.generate())
        async with factory() as session:
            with pytest.raises(BundleSignatureError):
                await store.import_initial_from(session, settings, now=_NOW)
        async with factory() as session:
            assert await store.current(session) is None

    async def test_import_is_a_no_op_once_the_store_has_bundles(
        self, migrated_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        factory = make_session_factory(migrated_engine)
        await _publish(factory)
        async with factory() as session:
            assert (
                await store.import_initial_from(session, _bundle_file(tmp_path), now=_NOW) is None
            )
            assert len(await store.history(session)) == 1

    async def test_an_imported_bundle_keeps_its_own_signature(
        self, migrated_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        factory = make_session_factory(migrated_engine)
        async with factory() as session:
            row = await store.import_initial_from(session, _bundle_file(tmp_path), now=_NOW)
        assert row is not None
        assert row.created_at is None, "the bootstrap bundle was signed without a timestamp"
        verify_bundle(store.bundle_of(row), row.signature, _KEY.public_key())
        assert row.activated_by == "bootstrap"


class TestTheEvent:
    async def test_it_is_sent_after_the_commit_and_names_the_serial(
        self, migrated_engine: AsyncEngine
    ) -> None:
        factory = make_session_factory(migrated_engine)
        publisher = RecordingPublisher(factory)
        row = await _publish(factory, publisher=publisher)
        assert publisher.events == [(row.serial, True)]

    async def test_a_broker_outage_does_not_lose_the_bundle(
        self, migrated_engine: AsyncEngine
    ) -> None:
        # Persist, then publish (spec 07's rule, reused): PEPs that miss the event still pull.
        factory = make_session_factory(migrated_engine)
        row = await _publish(factory, publisher=RecordingPublisher(factory, fail=True))
        async with factory() as session:
            assert await store.get(session, row.serial) is not None


# --- over HTTP -------------------------------------------------------------------------


def _client(
    engine: AsyncEngine,
    *,
    publishing: PolicyPublishingSettings | None = _PUBLISHING,
    publisher: RecordingPublisher | None = None,
    session_of: str | None = None,
) -> httpx.AsyncClient:
    app = create_app(
        session_factory=make_session_factory(engine),
        escalation_settings=_SETTINGS,
        policy_publishing=publishing,
        policy_publisher=publisher,
        now=lambda: _NOW,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    if session_of is not None:
        client.cookies.set("session", _cookie(session_of))
    return client


class TestActivation:
    async def test_no_session_is_401_and_publishes_nothing(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine) as client:
            response = await client.post("/policy/activate", data={"source": _PASSING})
            assert response.status_code == 401
            assert (await client.get("/v1/policy/bundles/current")).status_code == 404

    async def test_a_non_approver_is_403(self, migrated_engine: AsyncEngine) -> None:
        async with _client(migrated_engine, session_of=_ANALYST) as client:
            response = await client.post("/policy/activate", data={"source": _PASSING})
        assert response.status_code == 403

    async def test_a_policy_failing_the_corpus_is_409_and_publishes_nothing(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine, session_of=_CFO) as client:
            response = await client.post("/policy/activate", data={"source": _FAILING})
            assert response.status_code == 409
            assert "corpus tests failed" in response.text
            assert (await client.get("/v1/policy/bundles/current")).status_code == 404

    async def test_unparseable_cedar_is_409(self, migrated_engine: AsyncEngine) -> None:
        async with _client(migrated_engine, session_of=_CFO) as client:
            response = await client.post("/policy/activate", data={"source": "not cedar"})
        assert response.status_code == 409

    async def test_an_empty_policy_is_refused(self, migrated_engine: AsyncEngine) -> None:
        # An empty policy set denies everything — never "activated successfully".
        async with _client(migrated_engine, session_of=_CFO) as client:
            assert (await client.post("/policy/activate", data={"source": ""})).status_code == 409

    async def test_breaking_beat_4s_payment_limit_is_refused_and_named(
        self, migrated_engine: AsyncEngine
    ) -> None:
        broken = CORPUS_SOURCE.replace('decimal("500000.0")', 'decimal("1000.0")')
        async with _client(migrated_engine, session_of=_CFO) as client:
            response = await client.post("/policy/activate", data={"source": broken})
        assert response.status_code == 409
        assert "corpus tests failed" in response.text

    async def test_the_previous_policy_keeps_serving_after_a_refusal(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine, session_of=_CFO) as client:
            await client.post("/policy/activate", data={"source": _PASSING})
            await client.post("/policy/activate", data={"source": "not cedar"})
            current = (await client.get("/v1/policy/bundles/current")).json()
        assert current["serial"] == 1
        assert current["cedar_source"] == _PASSING

    async def test_an_approver_publishes_and_the_response_names_the_serial(
        self, migrated_engine: AsyncEngine
    ) -> None:
        factory = make_session_factory(migrated_engine)
        publisher = RecordingPublisher(factory)
        async with _client(migrated_engine, session_of=_CFO, publisher=publisher) as client:
            response = await client.post(
                "/policy/activate", data={"source": _PASSING, "nl_digest": "0123456789abcdef"}
            )
            assert response.status_code == 200
            assert "serial 1" in response.text
            current = (await client.get("/v1/policy/bundles/current")).json()
        assert current["serial"] == 1
        assert current["cedar_source"] == _PASSING
        assert current["provenance"] == {
            "activated_by": _CFO,
            "source_serial": None,
            "nl_digest": "0123456789abcdef",
        }
        assert publisher.events == [(1, True)]

    async def test_a_malformed_digest_is_dropped_not_stored(
        self, migrated_engine: AsyncEngine
    ) -> None:
        # The field arrives from a browser form; only the 16-hex shape the compiler emits is
        # kept, so nothing else can be smuggled into the record through it.
        async with _client(migrated_engine, session_of=_CFO) as client:
            await client.post(
                "/policy/activate", data={"source": _PASSING, "nl_digest": "Pay alice 5000 BDT"}
            )
            current = (await client.get("/v1/policy/bundles/current")).json()
        assert current["provenance"]["nl_digest"] is None

    async def test_without_a_signing_key_activation_says_publication_is_off(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine, session_of=_CFO, publishing=None) as client:
            response = await client.post("/policy/activate", data={"source": _PASSING})
        assert response.status_code == 503

    async def test_the_policy_page_shows_the_published_history(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine, session_of=_CFO) as client:
            await client.post("/policy/activate", data={"source": _PASSING})
            page = (await client.get("/policy")).text
        assert "bundle-1" in page
        assert _CFO in page

    async def test_an_approver_can_roll_back_to_an_earlier_serial(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine, session_of=_CFO) as client:
            await client.post("/policy/activate", data={"source": CORPUS_SOURCE})
            await client.post("/policy/activate", data={"source": _PASSING})
            response = await client.post("/policy/republish", data={"serial": "1"})
            assert response.status_code == 200
            current = (await client.get("/v1/policy/bundles/current")).json()
        assert current["serial"] == 3
        assert current["cedar_source"] == CORPUS_SOURCE
        assert current["provenance"]["source_serial"] == 1

    async def test_rolling_back_needs_an_approver_too(self, migrated_engine: AsyncEngine) -> None:
        async with _client(migrated_engine, session_of=_ANALYST) as client:
            assert (await client.post("/policy/republish", data={"serial": "1"})).status_code == 403


class TestTheFetchRoutes:
    async def test_current_is_404_before_anything_is_published(
        self, migrated_engine: AsyncEngine
    ) -> None:
        async with _client(migrated_engine) as client:
            assert (await client.get("/v1/policy/bundles/current")).status_code == 404

    async def test_a_fetched_bundle_verifies(self, migrated_engine: AsyncEngine) -> None:
        await _publish(make_session_factory(migrated_engine))
        async with _client(migrated_engine) as client:
            by_serial = (await client.get("/v1/policy/bundles/1")).json()
            assert (await client.get("/v1/policy/bundles/2")).status_code == 404
        bundle, signature = bundle_from_wire(by_serial)
        verify_bundle(bundle, signature, _KEY.public_key())


class TestStartup:
    async def test_the_lifespan_imports_the_deployment_bundle(
        self, migrated_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        app = create_app(
            session_factory=make_session_factory(migrated_engine),
            escalation_settings=_SETTINGS,
            policy_publishing=_bundle_file(tmp_path),
            now=lambda: _NOW,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://cp"
            ) as client:
                current = (await client.get("/v1/policy/bundles/current")).json()
        assert current["serial"] == 1
        assert current["provenance"]["activated_by"] == "bootstrap"
