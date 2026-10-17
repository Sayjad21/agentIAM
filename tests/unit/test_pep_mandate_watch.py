"""`MandateWatch` — a PEP getting ready for a mandate it was told about (APICTA-02 Part B).

Only the message handling is tested here; the subscription loop is `PolicySync`'s, and the
round trip through a real Redis is `tests/integration/test_mandate_watch.py`.
"""

from __future__ import annotations

import json
import uuid

import pytest

from agentiam_pep.mandate_watch import MandateWatch


class FakeRegistry:
    def __init__(self) -> None:
        """Record which mandates were ensured."""
        self.ensured: list[uuid.UUID] = []

    async def ensure(self, mandate_id: uuid.UUID) -> object:
        self.ensured.append(mandate_id)
        return None


MANDATE = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a5")


async def test_a_created_event_makes_the_pool_ready() -> None:
    registry = FakeRegistry()
    watch = MandateWatch(registry=registry, redis_client=None)
    raw = json.dumps({"event": "mandate:created", "mandate_id": str(MANDATE)})
    assert await watch.handle_push(raw)
    assert registry.ensured == [MANDATE]
    assert watch.received == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        json.dumps(["a", "list"]),
        json.dumps({"event": "policy:reload", "mandate_id": str(MANDATE)}),
        json.dumps({"event": "mandate:created"}),
        json.dumps({"event": "mandate:created", "mandate_id": "not-a-uuid"}),
    ],
)
async def test_anything_else_is_ignored(raw: str | bytes) -> None:
    registry = FakeRegistry()
    watch = MandateWatch(registry=registry, redis_client=None)
    assert not await watch.handle_push(raw)
    assert registry.ensured == []


async def test_start_and_aclose_without_redis_are_harmless() -> None:
    """A PEP with no Redis still converges: the first token creates the pool (ADR-078)."""
    watch = MandateWatch(registry=FakeRegistry(), redis_client=None)
    await watch.start()
    await watch.aclose()


def test_the_pep_listens_where_the_control_plane_announces() -> None:
    """Two constants in two packages — the PEP does not import the control plane."""
    from agentiam_controlplane.db.mandates import CHANNEL as ANNOUNCED
    from agentiam_pep.mandate_watch import CHANNEL as WATCHED

    assert ANNOUNCED == WATCHED
