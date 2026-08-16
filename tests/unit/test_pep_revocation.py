"""The revocation set — T-023/T-038, step 3.

Small, and worth its own module for one reason: an empty revocation set is **not** a stub.
Before T-038, nothing in this system could revoke anything, so *no token is revoked* was the
true state of the world. Compare with the allow-all policy engine T-023 deliberately did not
ship (ADR-027) — that one would have reported work it did not do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentiam_core.decision import OracleUnavailable
from agentiam_pep.revocation import InMemoryRevocationSet, RedisRevocationSet


class TestLookups:
    def test_an_empty_set_revokes_nothing(self) -> None:
        assert not InMemoryRevocationSet().is_revoked("blk_1")

    def test_a_known_id_is_revoked(self) -> None:
        assert InMemoryRevocationSet(["blk_1"]).is_revoked("blk_1")

    def test_an_unknown_id_is_not(self) -> None:
        assert not InMemoryRevocationSet(["blk_1"]).is_revoked("blk_2")

    def test_revoking_takes_effect_immediately(self) -> None:
        """Demo Beat 7 flips this live, on stage."""
        oracle = InMemoryRevocationSet()
        assert not oracle.is_revoked("blk_1")
        oracle.revoke("blk_1")
        assert oracle.is_revoked("blk_1")

    def test_revoking_twice_is_idempotent(self) -> None:
        oracle = InMemoryRevocationSet()
        oracle.revoke("blk_1")
        oracle.revoke("blk_1")
        assert len(oracle) == 1


class TestUnavailable:
    """The shape T-038 needs when its data is too stale to answer from."""

    def test_an_unavailable_set_raises_rather_than_answering(self) -> None:
        """`decide()` turns this into CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED.

        Answering "not revoked" from a set known to be stale is the one wrong reply: it is
        indistinguishable from a correct answer and it fails open.
        """
        oracle = InMemoryRevocationSet(unavailable="gossip stream is 40 s behind")
        with pytest.raises(OracleUnavailable, match="40 s behind"):
            oracle.is_revoked("blk_1")

    def test_an_available_set_does_not_raise(self) -> None:
        assert InMemoryRevocationSet().is_revoked("blk_1") is False


class TestSize:
    def test_length_reports_what_is_held(self) -> None:
        """`/readyz` reports this, so an operator can see the set is populated at all."""
        assert len(InMemoryRevocationSet(["a", "b"])) == 2


class _FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        """Never fails in these tests; failure is exercised via `FailingClient`."""

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeControlPlaneClient:
    """Serves fixed pages from `GET /v1/revocations`, one per call, then repeats empty."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    async def get(self, path: str, *, params: dict[str, Any]) -> _FakeResponse:
        self.calls.append(dict(params))
        page = self._pages.pop(0) if self._pages else {"entries": [], "next_seq": params["since"]}
        return _FakeResponse(page)


class _FailingControlPlaneClient:
    async def get(self, path: str, *, params: dict[str, Any]) -> _FakeResponse:
        raise ConnectionError("control plane unreachable")


def _clock(instant: datetime) -> Any:
    return lambda: instant


def _a_set(
    *, http: Any, now: datetime = datetime(2026, 8, 17, tzinfo=UTC), **over: object
) -> RedisRevocationSet:
    kwargs: dict[str, object] = {
        "redis_client": object(),  # never touched: these tests never start the subscribe loop
        "control_plane_client": http,
        "pull_interval_s": 3.0,
        "staleness_limit_s": 9.0,
        "now": _clock(now),
    }
    return RedisRevocationSet(**(kwargs | over))  # type: ignore[arg-type]


class TestRedisRevocationSetPull:
    async def test_before_any_pull_the_set_is_unavailable(self) -> None:
        oracle = _a_set(http=_FakeControlPlaneClient([]))
        with pytest.raises(OracleUnavailable, match="no revocation pull"):
            oracle.is_revoked("blk_1")

    async def test_a_successful_pull_populates_the_set(self) -> None:
        http = _FakeControlPlaneClient(
            [{"entries": [{"block_id": "blk_1"}, {"block_id": "blk_2"}], "next_seq": 2}]
        )
        oracle = _a_set(http=http)
        await oracle._pull_once()
        assert oracle.is_revoked("blk_1")
        assert oracle.is_revoked("blk_2")
        assert not oracle.is_revoked("blk_3")
        assert len(oracle) == 2

    async def test_the_cursor_advances_across_pulls(self) -> None:
        http = _FakeControlPlaneClient(
            [
                {"entries": [{"block_id": "blk_1"}], "next_seq": 1},
                {"entries": [{"block_id": "blk_2"}], "next_seq": 2},
            ]
        )
        oracle = _a_set(http=http)
        await oracle._pull_once()
        await oracle._pull_once()
        assert http.calls == [{"since": 0}, {"since": 1}]
        assert oracle.is_revoked("blk_1")
        assert oracle.is_revoked("blk_2")

    async def test_an_empty_page_leaves_the_cursor_unchanged(self) -> None:
        http = _FakeControlPlaneClient([{"entries": [], "next_seq": 0}])
        oracle = _a_set(http=http)
        await oracle._pull_once()
        await oracle._pull_once()
        assert http.calls == [{"since": 0}, {"since": 0}]

    async def test_a_failed_pull_does_not_raise_and_leaves_the_set_intact(self) -> None:
        http = _FakeControlPlaneClient([{"entries": [{"block_id": "blk_1"}], "next_seq": 1}])
        oracle = _a_set(http=http)
        await oracle._pull_once()
        assert oracle.is_revoked("blk_1")

        oracle._http = _FailingControlPlaneClient()  # type: ignore[assignment]
        await oracle._pull_once()  # must not raise
        assert oracle.is_revoked("blk_1")  # unaffected — the old data is kept


class TestRedisRevocationSetStaleness:
    async def test_fresh_after_a_pull_does_not_raise(self) -> None:
        now = datetime(2026, 8, 17, tzinfo=UTC)
        http = _FakeControlPlaneClient([{"entries": [], "next_seq": 0}])
        oracle = _a_set(http=http, now=now, staleness_limit_s=9.0)
        await oracle._pull_once()
        assert oracle.is_revoked("blk_1") is False

    async def test_past_the_staleness_limit_it_raises(self) -> None:
        clock = {"now": datetime(2026, 8, 17, tzinfo=UTC)}
        http = _FakeControlPlaneClient([{"entries": [], "next_seq": 0}])
        oracle = _a_set(http=http, now=clock["now"], staleness_limit_s=9.0)
        oracle._now = lambda: clock["now"]
        await oracle._pull_once()

        clock["now"] = clock["now"] + timedelta(seconds=10)
        with pytest.raises(OracleUnavailable, match="stale"):
            oracle.is_revoked("blk_1")

    async def test_exactly_at_the_limit_still_raises(self) -> None:
        # Boundary matches Escalation's "expires at 12:15" precedent: inclusive at the edge,
        # stated explicitly so it is never ambiguous in code.
        clock = {"now": datetime(2026, 8, 17, tzinfo=UTC)}
        http = _FakeControlPlaneClient([{"entries": [], "next_seq": 0}])
        oracle = _a_set(http=http, staleness_limit_s=9.0)
        oracle._now = lambda: clock["now"]
        await oracle._pull_once()

        clock["now"] = clock["now"] + timedelta(seconds=9)
        with pytest.raises(OracleUnavailable):
            oracle.is_revoked("blk_1")


class TestRedisRevocationSetPush:
    async def test_a_well_formed_push_message_revokes_the_id(self) -> None:
        oracle = _a_set(http=_FakeControlPlaneClient([{"entries": [], "next_seq": 0}]))
        await oracle._pull_once()
        oracle._handle_push(b'{"block_id": "blk_1", "seq": 1}')
        assert oracle.is_revoked("blk_1")

    async def test_a_malformed_push_message_is_ignored_rather_than_raising(self) -> None:
        oracle = _a_set(http=_FakeControlPlaneClient([{"entries": [], "next_seq": 0}]))
        await oracle._pull_once()
        oracle._handle_push(b"not json")
        oracle._handle_push(b"{}")
        assert len(oracle) == 0


class TestRedisRevocationSetLifecycle:
    async def test_start_performs_a_pull_before_returning(self) -> None:
        """Spec 07 §7 cold start: don't serve from an empty, unearned set."""
        http = _FakeControlPlaneClient([{"entries": [{"block_id": "blk_1"}], "next_seq": 1}])
        oracle = _a_set(http=http)
        # `start()` also launches the subscribe task, which needs a real Redis client —
        # exercised in the integration suite. Here we only need the cold-start pull, so
        # call the same method `start()` calls before the background tasks.
        await oracle._pull_once()
        assert oracle.is_revoked("blk_1")

    async def test_aclose_before_start_does_not_raise(self) -> None:
        oracle = _a_set(http=_FakeControlPlaneClient([]))
        await oracle.aclose()

    async def test_aclose_is_idempotent(self) -> None:
        oracle = _a_set(http=_FakeControlPlaneClient([]))
        await oracle.aclose()
        await oracle.aclose()
