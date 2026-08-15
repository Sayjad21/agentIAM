"""Continuously assert the ledger's invariants — T-016, `PLAN.md` §9.

Three ways this gets used, and each shapes the interface:

* **On screen, during demo Beat 4.** A judge watches three sub-agents spend concurrently
  against a ceiling they set, with this running beside it. So the default output is one
  line per sweep, green while it holds, and loud when it does not.
* **As a sidecar through every chaos scenario** (T-052). So it must survive the database
  going away and come back when it returns — a checker that dies during CH-1 (Postgres
  down) reports nothing about the far more interesting moment when Postgres comes back.
* **As a one-shot gate.** `--once` exits non-zero if anything is broken, which is what a
  test harness or a CI step wants.

The checking itself is `agentiam_controlplane.db.invariants` — read-only, lock-free, one
statement per sweep, measured at ~5 ms over 500 budgets. This file is the loop and the
rendering around it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from typing import TYPE_CHECKING, Final

from sqlalchemy.exc import SQLAlchemyError

from agentiam_controlplane.db.base import make_engine
from agentiam_controlplane.db.invariants import CheckReport, check_invariants

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Matches `docker-compose.yml`'s defaults, so `make up` then this script just works.
DEFAULT_DATABASE_URL: Final = "postgresql+asyncpg://agentiam:agentiam@localhost:5432/agentiam"

_GREEN: Final = "\033[32m"
_RED: Final = "\033[31m"
_YELLOW: Final = "\033[33m"
_RESET: Final = "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without running anything."""
    parser = argparse.ArgumentParser(
        prog="run_invariant_checker",
        description="Continuously assert the AgentIAM ledger's invariants.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "Ledger DSN. Defaults to $DATABASE_URL — the same variable Alembic reads "
            "(.env.example) — then to the compose defaults."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between sweeps (default: 1.0). The acceptance bar is detection <1 s.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Sweep once and exit. Exit code is 0 if every invariant holds, 1 otherwise.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="One JSON object per sweep on stdout, for a harness rather than a human.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first violation. Off by default: a chaos run wants the whole tape.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only violations. Useful when this runs for an hour beside something else.",
    )
    return parser


def resolve_database_url(explicit: str | None) -> str:
    """Pick the DSN: the flag, then the environment, then the compose default."""
    return explicit or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def render(report: CheckReport, *, colour: bool) -> str:
    """One block of human-readable output for a sweep."""
    if report.holds:
        body = report.summary()
        return f"{_GREEN}{body}{_RESET}" if colour else body

    lines = [report.summary(), *(f"  {violation}" for violation in report.violations)]
    body = "\n".join(lines)
    return f"{_RED}{body}{_RESET}" if colour else body


def render_json(report: CheckReport) -> str:
    """One line of JSON per sweep, for the chaos harness and the console's SSE feed."""
    return json.dumps(
        {
            "holds": report.holds,
            "budgets_checked": report.budgets_checked,
            "duration_ms": round(report.duration_ms, 3),
            "violations": [
                {
                    "kind": v.kind.value,
                    "budget_id": str(v.budget_id),
                    "mandate_id": str(v.mandate_id),
                    "dimension": v.dimension,
                    "expected": str(v.expected),
                    "actual": str(v.actual),
                    "delta": str(v.delta),
                }
                for v in report.violations
            ],
        }
    )


async def run(args: argparse.Namespace) -> int:
    """Sweep until interrupted. Returns the process exit code.

    Any violation seen at any point makes the final exit code non-zero, even if later
    sweeps come back green — a chaos run that was briefly inconsistent did not pass.
    """
    engine = make_engine(resolve_database_url(args.database_url))
    colour = sys.stdout.isatty() and not args.as_json
    stop = asyncio.Event()
    ever_violated = False

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:  # pragma: no cover - platform dependent
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows' ProactorEventLoop has no add_signal_handler; KeyboardInterrupt
            # still unwinds `main`, which is the only path that matters there.
            pass

    try:
        while not stop.is_set():
            try:
                report = await check_invariants(engine)
            except (SQLAlchemyError, OSError) as exc:
                # The database going away is the *expected* condition in CH-1, not a
                # reason to stop: what matters is what the ledger looks like when it
                # returns. Report and keep sweeping.
                #
                # `OSError` as well as `SQLAlchemyError`, and not by guesswork —
                # measured: pointing the checker at a dead port raises a bare
                # `ConnectionRefusedError`, which SQLAlchemy never wraps because the
                # failure happens below the dialect. Catching only `SQLAlchemyError`
                # made the script die on exactly the scenario it documents surviving.
                # `socket.gaierror` and `TimeoutError` are both `OSError` too.
                message = f"unreachable: {type(exc).__name__}"
                print(
                    json.dumps({"holds": None, "error": message})
                    if args.as_json
                    else (f"{_YELLOW}WARN  {message}{_RESET}" if colour else f"WARN  {message}"),
                    flush=True,
                )
            else:
                ever_violated = ever_violated or not report.holds
                if args.as_json:
                    print(render_json(report), flush=True)
                elif not (args.quiet and report.holds):
                    print(render(report, colour=colour), flush=True)

                if args.fail_fast and not report.holds:
                    break

            if args.once:
                break

            try:
                await asyncio.wait_for(stop.wait(), timeout=args.interval)
            except TimeoutError:
                pass
    finally:
        await engine.dispose()

    return 1 if ever_violated else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the exit code rather than calling `sys.exit`, so it is testable."""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130


if __name__ == "__main__":
    sys.exit(main())
