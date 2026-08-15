"""The audit-chain verifier CLI — spec 08 §5, NFR-6.

The chain walking itself is tested against real Postgres in
`tests/integration/test_audit_chain.py`. This is the CLI around it: argument handling, output
shapes, and — the part that matters operationally — that *"the chain is broken"* and *"I could
not look at the chain"* are different exit codes. Collapsing them hides an outage behind a
tamper alert, which is a bad night for whoever is on call.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from agentiam_controlplane.db.audit import ChainVerification
from scripts import verify_audit_chain


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _patch(
    monkeypatch: pytest.MonkeyPatch, result: ChainVerification | Exception
) -> dict[str, Any]:
    disposed = {"engine": False}

    class _Engine:
        async def dispose(self) -> None:
            disposed["engine"] = True

    def factory(_engine: object) -> object:
        def make() -> _FakeSession:
            return _FakeSession()

        return make

    async def verify(_session: object) -> ChainVerification:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(verify_audit_chain, "make_engine", lambda _url: _Engine())
    monkeypatch.setattr(verify_audit_chain, "make_session_factory", factory)
    monkeypatch.setattr(verify_audit_chain, "verify_chain", verify)
    return disposed


class TestExitCodes:
    def test_an_intact_chain_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, ChainVerification(ok=True, checked=7))
        assert verify_audit_chain.main([]) == verify_audit_chain.EXIT_OK

    def test_a_broken_chain_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(
            monkeypatch,
            ChainVerification(ok=False, checked=2, first_bad_seq=3, detail="altered"),
        )
        assert verify_audit_chain.main([]) == verify_audit_chain.EXIT_BROKEN

    def test_an_unreachable_database_exits_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Distinct from a broken chain on purpose — see the module docstring."""
        _patch(monkeypatch, OperationalError("select 1", {}, Exception("no route to host")))
        assert verify_audit_chain.main([]) == verify_audit_chain.EXIT_UNAVAILABLE

    def test_the_three_codes_are_distinct(self) -> None:
        codes = {
            verify_audit_chain.EXIT_OK,
            verify_audit_chain.EXIT_BROKEN,
            verify_audit_chain.EXIT_UNAVAILABLE,
        }
        assert len(codes) == 3


class TestOutput:
    def test_a_break_names_the_sequence_number(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """NFR-6 asks for the first inconsistent record, not merely 'invalid'."""
        _patch(
            monkeypatch,
            ChainVerification(ok=False, checked=2, first_bad_seq=3, detail="it was altered"),
        )
        verify_audit_chain.main([])
        assert "seq 3" in capsys.readouterr().err

    def test_json_mode_is_machine_readable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        _patch(
            monkeypatch,
            ChainVerification(ok=False, checked=2, first_bad_seq=3, detail="altered"),
        )
        verify_audit_chain.main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "ok": False,
            "checked": 2,
            "first_bad_seq": 3,
            "detail": "altered",
        }

    def test_json_mode_says_unavailable_rather_than_broken(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        _patch(monkeypatch, OperationalError("select 1", {}, Exception("down")))
        verify_audit_chain.main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["unavailable"] is True

    def test_quiet_prints_nothing_on_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch(monkeypatch, ChainVerification(ok=True, checked=3))
        verify_audit_chain.main(["--quiet"])
        assert capsys.readouterr().out == ""

    def test_success_reports_how_many_were_checked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """'Intact' without a count could mean an empty table nobody noticed was empty."""
        _patch(monkeypatch, ChainVerification(ok=True, checked=42))
        verify_audit_chain.main([])
        assert "42" in capsys.readouterr().out

    def test_the_engine_is_disposed_even_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        disposed = _patch(monkeypatch, OperationalError("select 1", {}, Exception("down")))
        verify_audit_chain.main([])
        assert disposed["engine"], "a leaked connection pool outlives the process"


class TestRepr:
    def test_an_ok_result_reads_cleanly(self) -> None:
        assert repr(ChainVerification(ok=True, checked=3)) == (
            "ChainVerification(ok=True, checked=3)"
        )

    def test_a_broken_result_carries_the_seq(self) -> None:
        text = repr(ChainVerification(ok=False, checked=1, first_bad_seq=2, detail="x"))
        assert "first_bad_seq=2" in text
