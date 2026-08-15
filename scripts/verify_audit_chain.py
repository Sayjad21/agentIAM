"""Verify the audit hash chain — M4, spec 08 §5, NFR-6.

Three uses, and each shapes the interface:

* **On stage, during demo Beat 8.** A judge alters one audit record in the database and this
  names the sequence number it broke at. So the default output is short, and the failure says
  *where*, not merely *no*.
* **As a gate.** Exits non-zero on a broken chain, which is what an evidence-pack build
  (T-055) or a CI step wants.
* **After a chaos run.** `--json` so T-052's harness can assert on it.

The verification itself is `agentiam_controlplane.db.audit.verify_chain` — a single ordered
walk recomputing every link. This file is the CLI around it.

NFR-6 asks for tamper detection that *names the first inconsistent record*. Reporting only
"invalid" would satisfy the letter and be useless to whoever has to act on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING, Final

from sqlalchemy.exc import SQLAlchemyError

from agentiam_controlplane.db.audit import verify_chain
from agentiam_controlplane.db.base import make_engine, make_session_factory

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Matches `docker-compose.yml`'s defaults, so `make up` then this script just works.
DEFAULT_DATABASE_URL: Final = "postgresql+asyncpg://agentiam:agentiam@localhost:5432/agentiam"

_GREEN: Final = "\033[32m"
_RED: Final = "\033[31m"
_RESET: Final = "\033[0m"

#: Exit codes, so a caller can tell "the chain is broken" from "I could not look".
EXIT_OK: Final = 0
EXIT_BROKEN: Final = 1
EXIT_UNAVAILABLE: Final = 2


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_audit_chain",
        description="Walk the audit chain and report the first inconsistent record (NFR-6).",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Postgres URL. Defaults to $DATABASE_URL, then the compose default.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one JSON object instead of prose, for a harness to assert on.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print nothing on success; the exit code carries the answer.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    engine = make_engine(args.database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            result = await verify_chain(session)
    except SQLAlchemyError as exc:
        # Distinct from a broken chain: "I could not look" and "it is wrong" call for
        # different responses, and collapsing them into one exit code hides an outage.
        if args.as_json:
            print(json.dumps({"ok": False, "unavailable": True, "detail": str(exc)}))
        else:
            print(f"{_RED}cannot verify{_RESET}: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    finally:
        await engine.dispose()

    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "checked": result.checked,
                    "first_bad_seq": result.first_bad_seq,
                    "detail": result.detail,
                }
            )
        )
    elif not result.ok:
        print(
            f"{_RED}CHAIN BROKEN{_RESET} at seq {result.first_bad_seq}: {result.detail}\n"
            f"  {result.checked} record(s) verified before the break; everything after "
            f"seq {result.first_bad_seq} is untrustworthy by construction.",
            file=sys.stderr,
        )
    elif not args.quiet:
        print(f"{_GREEN}chain intact{_RESET}: {result.checked} record(s) verified")

    return EXIT_OK if result.ok else EXIT_BROKEN


def main(argv: Sequence[str] | None = None) -> int:
    """Verify once and return an exit code."""
    return asyncio.run(_run(_parse(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
