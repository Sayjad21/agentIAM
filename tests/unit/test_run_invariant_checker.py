"""The invariant checker's CLI (`scripts/run_invariant_checker.py`).

The sweep itself is tested against a real ledger in
`tests/integration/test_invariant_checker.py`. What is checked here is the shell around
it, which has three consumers with different needs: a judge watching a screen, a chaos
harness parsing stdout, and a CI step reading an exit code.
"""

from __future__ import annotations

import argparse
import json
import uuid
from decimal import Decimal

import pytest

from agentiam_controlplane.db.invariants import CheckReport, InvariantKind, Violation
from scripts.run_invariant_checker import (
    DEFAULT_DATABASE_URL,
    build_parser,
    render,
    render_json,
    resolve_database_url,
    run,
)

MANDATE = uuid.UUID("9f2c1e40-7a3b-4d21-9c88-1b2e5f0a4d77")
BUDGET = uuid.UUID("1b2e5f0a-4d77-4d21-9c88-9f2c1e407a3b")


def a_violation(kind: InvariantKind = InvariantKind.COMMITTED_VS_RESERVATIONS) -> Violation:
    return Violation(
        kind=kind,
        budget_id=BUDGET,
        mandate_id=MANDATE,
        dimension="spend_bdt",
        expected=Decimal("40.0000"),
        actual=Decimal("50.0000"),
    )


GREEN = CheckReport(budgets_checked=7, violations=(), duration_ms=4.2)
RED = CheckReport(
    budgets_checked=7,
    violations=(a_violation(), a_violation(InvariantKind.POOL)),
    duration_ms=4.2,
)


class TestArguments:
    def test_the_defaults_are_the_demo_defaults(self) -> None:
        args = build_parser().parse_args([])
        assert args.interval == 1.0
        assert args.once is False
        assert args.as_json is False
        assert args.fail_fast is False

    def test_once_is_available_for_a_gate(self) -> None:
        assert build_parser().parse_args(["--once"]).once is True

    def test_the_interval_is_a_float(self) -> None:
        assert build_parser().parse_args(["--interval", "0.25"]).interval == 0.25

    def test_an_unknown_flag_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--noo"])


class TestDatabaseUrl:
    def test_the_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env/db")
        assert (
            resolve_database_url("postgresql+asyncpg://flag/db") == "postgresql+asyncpg://flag/db"
        )

    def test_the_environment_is_next(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env/db")
        assert resolve_database_url(None) == "postgresql+asyncpg://env/db"

    def test_the_compose_default_is_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`make up` then run it — no configuration for the common case."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert resolve_database_url(None) == DEFAULT_DATABASE_URL
        assert "localhost:5432" in DEFAULT_DATABASE_URL


class TestHumanRendering:
    def test_green_is_one_line(self) -> None:
        assert "\n" not in render(GREEN, colour=False)

    def test_red_names_every_violation_on_its_own_line(self) -> None:
        lines = render(RED, colour=False).splitlines()
        assert len(lines) == 3, "a summary plus one line per violation"
        assert str(MANDATE) in lines[1]

    def test_colour_is_off_when_asked(self) -> None:
        assert "\033[" not in render(GREEN, colour=False)
        assert "\033[" not in render(RED, colour=False)

    def test_colour_is_on_when_asked(self) -> None:
        """A judge is looking at this from across a room."""
        assert render(GREEN, colour=True).startswith("\033[32m")
        assert render(RED, colour=True).startswith("\033[31m")

    def test_green_and_red_are_distinguishable_without_colour(self) -> None:
        """Piped to a file, or read by someone who cannot rely on hue."""
        assert render(GREEN, colour=False).startswith("OK")
        assert render(RED, colour=False).startswith("FAIL")


class TestJsonRendering:
    def test_it_is_one_line_of_valid_json(self) -> None:
        line = render_json(RED)
        assert "\n" not in line
        json.loads(line)

    def test_green_carries_the_shape_a_harness_branches_on(self) -> None:
        payload = json.loads(render_json(GREEN))
        assert payload["holds"] is True
        assert payload["violations"] == []
        assert payload["budgets_checked"] == 7

    def test_red_carries_every_violation(self) -> None:
        payload = json.loads(render_json(RED))
        assert payload["holds"] is False
        assert len(payload["violations"]) == 2
        assert {v["kind"] for v in payload["violations"]} == {
            "committed_vs_reservations",
            "pool",
        }

    def test_money_is_a_string_not_a_float(self) -> None:
        """Rule 4 does not stop at the process boundary.

        `json.dumps` turns a `Decimal` into a float given half a chance, and a chaos
        report that rounds the number it exists to report is worse than no report.
        """
        payload = json.loads(render_json(RED))
        violation = payload["violations"][0]
        for field in ("expected", "actual", "delta"):
            assert isinstance(violation[field], str), field
        assert violation["expected"] == "40.0000"

    def test_ids_survive_as_strings(self) -> None:
        violation = json.loads(render_json(RED))["violations"][0]
        assert violation["mandate_id"] == str(MANDATE)
        assert violation["budget_id"] == str(BUDGET)


class TestUnreachableDatabase:
    """CH-1 is *Postgres down*, and this runs as a sidecar through it.

    Needs no container: a port nothing listens on is enough, which is why these live with
    the unit tests. The first version of the loop caught only `SQLAlchemyError` and died
    here — measured, a refused connection surfaces as a bare `ConnectionRefusedError`,
    because the failure happens below the dialect and SQLAlchemy never wraps it. The
    script documented surviving the exact condition that killed it.
    """

    @staticmethod
    def args(**overrides: object) -> argparse.Namespace:
        base = build_parser().parse_args(["--once"])
        # Port 1 is reserved and nothing binds it; connections are refused immediately.
        base.database_url = "postgresql+asyncpg://agentiam:agentiam@127.0.0.1:1/nope"
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    async def test_it_warns_instead_of_crashing(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = await run(self.args())

        assert exit_code == 0, "unreachable is not the same as violated"
        assert "unreachable" in capsys.readouterr().out

    async def test_the_json_form_stays_parseable_when_the_database_is_down(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A harness parsing stdout must not choke on the one line that matters."""
        await run(self.args(as_json=True))

        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["holds"] is None, "unknown, not false — we did not observe a violation"
        assert "error" in payload

    async def test_being_unreachable_never_reports_a_violation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Failing to look is not the same as looking and finding nothing wrong.

        Nor is it the same as finding something wrong. Conflating the two would make
        every chaos run that bounces Postgres look like an invariant breach.
        """
        await run(self.args())
        assert "FAIL" not in capsys.readouterr().out
