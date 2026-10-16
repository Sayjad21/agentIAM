"""How a PEP stays current — APICTA-01 Part C, spec 05 §5.7.4.

The fetch-and-decide logic against a fake control plane (`httpx.MockTransport`) and a real
`PolicyCache`. The Redis push path is exercised against a real broker in
`tests/integration/test_policy_distribution.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentiam_core.bundles import PolicyBundle, bundle_to_wire, sign_bundle
from agentiam_pep.policy_cache import PolicyCache
from agentiam_pep.policy_sync import PolicySync

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
PERMIT = 'permit(principal, action == Action::"invoice:read", resource);'
KEY = Ed25519PrivateKey.generate()


def a_bundle(serial: int, source: str = PERMIT) -> PolicyBundle:
    return PolicyBundle(version=f"bundle-{serial}", cedar_source=source, serial=serial)


def wire(bundle: PolicyBundle, key: Ed25519PrivateKey = KEY) -> dict[str, object]:
    return bundle_to_wire(bundle, sign_bundle(bundle, key)) | {"provenance": {}}


class Clock:
    def __init__(self) -> None:
        """Start at NOW."""
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t


class ControlPlane:
    """Serves whatever `current` is set to, or fails the way it is told to."""

    def __init__(self) -> None:
        """Nothing published yet."""
        self.current: dict[str, object] | None = None
        self.status = 200
        self.body: bytes | None = None
        self.raise_error = False
        self.hang = False
        self.requests = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.hang:
            await asyncio.sleep(30)
        if self.raise_error:
            raise httpx.ConnectError("control plane down", request=request)
        if self.body is not None:
            return httpx.Response(self.status, content=self.body)
        if self.current is None:
            return httpx.Response(404, json={"detail": "nothing published"})
        return httpx.Response(self.status, json=self.current)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cache(clock: Clock) -> PolicyCache:
    built = PolicyCache(
        public_key=KEY.public_key(), max_staleness=timedelta(seconds=300), now=clock
    )
    boot = a_bundle(1)
    built.load(boot, sign_bundle(boot, KEY))
    return built


@pytest.fixture
def control_plane() -> ControlPlane:
    return ControlPlane()


@pytest.fixture
async def sync(cache: PolicyCache, control_plane: ControlPlane) -> AsyncIterator[PolicySync]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(control_plane.handle), base_url="http://cp"
    )
    built = PolicySync(
        cache=cache,
        control_plane_client=client,
        redis_client=None,
        pull_interval_s=0.05,
        boot_timeout_s=0.2,
        fetch_timeout_s=0.2,
    )
    yield built
    await built.aclose()
    await client.aclose()


class TestOneSync:
    async def test_a_newer_bundle_is_loaded(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        control_plane.current = wire(a_bundle(2))
        assert await sync.sync_once() == "loaded"
        assert cache.serial == 2

    async def test_the_same_bundle_confirms_and_restarts_the_clock(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane, clock: Clock
    ) -> None:
        control_plane.current = wire(a_bundle(1))
        clock.t = NOW + timedelta(seconds=299)
        assert await sync.sync_once() == "confirmed"
        clock.t = NOW + timedelta(seconds=500)
        assert not cache.stale

    async def test_an_older_bundle_is_ignored_and_does_not_confirm(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane, clock: Clock
    ) -> None:
        cache.load(a_bundle(5), sign_bundle(a_bundle(5), KEY))
        control_plane.current = wire(a_bundle(3))
        clock.t = NOW + timedelta(seconds=299)
        assert await sync.sync_once() == "behind"
        clock.t = NOW + timedelta(seconds=301)
        assert cache.stale, "a control plane behind the PEP is not evidence the PEP is current"

    async def test_a_different_bundle_under_the_held_serial_does_not_confirm(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane, clock: Clock
    ) -> None:
        # Validly signed, same serial, different policy: two bundles cannot share a serial, so
        # this is not a confirmation of what the PEP holds.
        control_plane.current = wire(a_bundle(1, source="forbid(principal, action, resource);"))
        clock.t = NOW + timedelta(seconds=299)
        assert await sync.sync_once() == "mismatch"
        clock.t = NOW + timedelta(seconds=301)
        assert cache.stale

    async def test_a_same_serial_bundle_that_does_not_verify_does_not_confirm(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane, clock: Clock
    ) -> None:
        # Otherwise anyone able to answer for the control plane could keep a PEP looking fresh
        # while it missed real updates.
        control_plane.current = wire(a_bundle(1), key=Ed25519PrivateKey.generate())
        clock.t = NOW + timedelta(seconds=299)
        assert await sync.sync_once() == "rejected"
        clock.t = NOW + timedelta(seconds=301)
        assert cache.stale

    async def test_a_forged_newer_bundle_is_refused_and_the_old_keeps_serving(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        control_plane.current = wire(a_bundle(2), key=Ed25519PrivateKey.generate())
        assert await sync.sync_once() == "rejected"
        assert cache.serial == 1
        assert cache.rejected == 1

    async def test_a_malformed_body_is_refused(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        control_plane.body = b'{"serial": "two"}'
        assert await sync.sync_once() == "rejected"
        assert cache.serial == 1

    async def test_nothing_published_changes_nothing(
        self, sync: PolicySync, cache: PolicyCache
    ) -> None:
        assert await sync.sync_once() == "absent"
        assert cache.serial == 1

    async def test_an_unreachable_control_plane_changes_nothing(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        control_plane.raise_error = True
        assert await sync.sync_once() == "unavailable"
        control_plane.raise_error = False
        control_plane.status = 500
        control_plane.current = wire(a_bundle(2))
        assert await sync.sync_once() == "unavailable"
        assert cache.serial == 1

    async def test_outcomes_are_counted(
        self, sync: PolicySync, control_plane: ControlPlane
    ) -> None:
        control_plane.current = wire(a_bundle(2))
        await sync.sync_once()
        await sync.sync_once()
        assert sync.outcomes["loaded"] == 1
        assert sync.outcomes["confirmed"] == 1
        assert sync.status()["outcomes"] == {"loaded": 1, "confirmed": 1}


class TestPush:
    async def test_an_event_fetches_the_current_bundle(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        # "current", not the serial named: events can arrive out of order or be coalesced.
        control_plane.current = wire(a_bundle(3))
        assert await sync.handle_push('{"event": "policy:reload", "serial": 2}')
        assert cache.serial == 3

    @pytest.mark.parametrize("raw", ["not json", '{"event": "something else"}', "[1, 2]"])
    async def test_an_unrecognised_event_is_ignored(
        self, sync: PolicySync, control_plane: ControlPlane, raw: str
    ) -> None:
        assert not await sync.handle_push(raw)
        assert control_plane.requests == 0


class TestLifecycle:
    async def test_boot_waits_a_bounded_time_for_the_control_plane(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        control_plane.hang = True
        started = asyncio.get_running_loop().time()
        await sync.start()
        assert asyncio.get_running_loop().time() - started < 1.0
        assert cache.serial == 1, "the boot bundle keeps serving while the control plane is away"

    async def test_boot_pulls_a_newer_bundle_before_returning(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        control_plane.current = wire(a_bundle(4))
        await sync.start()
        assert cache.serial == 4

    async def test_the_pull_loop_picks_up_a_bundle_published_later(
        self, sync: PolicySync, cache: PolicyCache, control_plane: ControlPlane
    ) -> None:
        await sync.start()
        control_plane.current = wire(a_bundle(6))
        for _ in range(40):
            if cache.serial == 6:
                break
            await asyncio.sleep(0.025)
        assert cache.serial == 6

    async def test_aclose_stops_the_loops(
        self, sync: PolicySync, control_plane: ControlPlane
    ) -> None:
        await sync.start()
        await sync.aclose()
        seen = control_plane.requests
        await asyncio.sleep(0.2)
        assert control_plane.requests == seen


class TestTheRecordNamesTheBundleThatDecided:
    async def test_a_bundle_landing_mid_request_is_not_credited_with_the_decision(
        self,
    ) -> None:
        """A sync can complete while a request awaits — here, the escalation store.

        The record used to read `policy.bundle.version` when it was written, after that await,
        so it named whatever bundle was current *then* rather than the one the decision used.
        """
        from datetime import timedelta as _td

        from agentiam_core.escalation import Escalation
        from agentiam_core.models import RequiresApproval
        from tests.unit.test_pep_pipeline import (
            POLICY,
            FakeEscalationSink,
            a_mandate,
            a_pipeline,
            bearer,
        )

        cache = PolicyCache(
            public_key=KEY.public_key(), max_staleness=_td(seconds=300), now=Clock()
        )
        first = PolicyBundle(version="bundle-1", cedar_source=POLICY, serial=1)
        cache.load(first, sign_bundle(first, KEY))
        second = PolicyBundle(version="bundle-2", cedar_source=POLICY, serial=2)

        class LoadsMidRequest(FakeEscalationSink):
            async def create(self, **kw: Any) -> Escalation:
                cache.load(second, sign_bundle(second, KEY))
                return await super().create(**kw)

        pipeline, sink, _ = await a_pipeline(
            caveats=(RequiresApproval(scopes=frozenset({"payment:initiate"})),),
            escalation_sink=LoadsMidRequest(),
        )
        pipeline._policy = cache
        emitter = pipeline._emitter
        await emitter.start()
        await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=bearer(a_mandate()),
            body=b'{"amount": "10.0000", "recipient": {"account_id": "acct_1"}}',
        )
        await emitter.flush()
        assert cache.serial == 2, "the new bundle did land during the request"
        assert sink.records[-1].policy_version == "bundle-1"  # type: ignore[attr-defined]
