"""Tamper with one audit record on purpose, so "Verify chain" can be shown turning red.

The audit chain's claim is that an edited record is caught (spec 08 §5). A console that only
ever shows the green result asks the audience to take the red one on trust. This makes the
red one real, on the demo database, and puts it back afterwards.

The edit is the one a person covering something up would make: the settlement agent's
refusal (`BUDGET_EXHAUSTED_CAVEAT`) rewritten as an allow. Only `record` changes — the
stored `record_hash` is left alone, which is exactly why the verifier catches it.

Reversible by construction: the original record is copied into `demo_tamper_backup` before
the edit, ``--undo`` writes it back and drops the table, and the chain verifies again. A
second tamper while one is outstanding is refused rather than overwriting the only copy of
the original.

Demo only. It edits the ledger directly, which is precisely what production credentials
must not allow (spec 08 §7, limitation 2).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The record the edit targets: the demo's one caveat-ceiling refusal.
TARGET_AGENT: Final = "agt-settlement"
TARGET_REASON: Final = "BUDGET_EXHAUSTED_CAVEAT"
#: Written literally into the SQL below too — never interpolated — so no statement is built
#: from a string at runtime.
BACKUP_TABLE: Final = "demo_tamper_backup"


async def _tamper(database_url: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            exists = (
                await conn.execute(text("select to_regclass(:t) is not null"), {"t": BACKUP_TABLE})
            ).scalar_one()
            if exists:
                print("A tamper is already in place. Run `make demo-untamper` first.")
                return 1

            row = (
                await conn.execute(
                    text(
                        "select seq from audit_records "
                        "where record->>'agent_id' = :agent and record->>'reason_code' = :reason "
                        "order by seq desc limit 1"
                    ),
                    {"agent": TARGET_AGENT, "reason": TARGET_REASON},
                )
            ).first()
            if row is None:
                print(f"No {TARGET_REASON} record for {TARGET_AGENT}. Run `make demo-seed` first.")
                return 1
            seq = int(row[0])

            await conn.execute(
                text(
                    "create table demo_tamper_backup as "
                    "select seq, record from audit_records where seq = :seq"
                ),
                {"seq": seq},
            )
            await conn.execute(
                text(
                    "update audit_records set record = jsonb_set(jsonb_set(record, "
                    "'{outcome}', '\"allow\"'), '{reason_code}', '\"OK\"') where seq = :seq"
                ),
                {"seq": seq},
            )
    finally:
        await engine.dispose()

    print(f"Tampered with record {seq}: {TARGET_AGENT}'s refusal now reads as an allow.")
    print("Click 'Verify chain' on /decisions. Undo with `make demo-untamper`.")
    return 0


async def _undo(database_url: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            exists = (
                await conn.execute(text("select to_regclass(:t) is not null"), {"t": BACKUP_TABLE})
            ).scalar_one()
            if not exists:
                print("Nothing to undo: no tamper is in place.")
                return 0
            restored = await conn.execute(
                text(
                    "update audit_records a set record = b.record "
                    "from demo_tamper_backup b where a.seq = b.seq"
                )
            )
            await conn.execute(text("drop table demo_tamper_backup"))
    finally:
        await engine.dispose()

    print(f"Restored {restored.rowcount} record(s). 'Verify chain' is green again.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without a database."""
    parser = argparse.ArgumentParser(
        prog="demo_tamper", description="Edit one audit record so the chain check fails."
    )
    parser.add_argument("--undo", action="store_true", help="Put the original record back.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Ledger DSN. Defaults to $DATABASE_URL, then the compose service name.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Tamper, or undo it. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    database_url = (
        args.database_url
        or os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://agentiam:agentiam@postgres:5432/agentiam"
    )
    return asyncio.run(_undo(database_url) if args.undo else _tamper(database_url))


if __name__ == "__main__":
    sys.exit(main())
