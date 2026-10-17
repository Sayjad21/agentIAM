"""The sibling race, shown live — `make demo-conc`.

`make demo-seed` sends one call at a time through one PEP, and its payments never approach
the pool, so it cannot show INV-5: three siblings, each holding a token that on its own
allows the whole budget, spending at once. `tests/integration/test_sibling_budgets.py`
proves the ledger handles that. This makes it visible on the demo stack.

**The shape.** One mandate with a small pool. Three sibling agents, each minted a token whose
per-request ceiling is the *whole* pool — nothing about any single token is wrong. Three
PEPs (`pep-conc-a/b/c`, an opt-in compose profile), one per sibling, each bound to that
mandate and holding its own lease. Every sibling fires all of its payments at once. Together
they ask for twice the pool. The ledger's row lock is the only thing that sees all three.

**A refusal is not always the end.** A PEP whose lease runs dry refuses with
`LEASE_UNAVAILABLE` while its top-up is in flight, then serves the next request. An agent
would retry, so the driver does. `BUDGET_EXHAUSTED_MANDATE` — the ledger had nothing left to
lease — is final. So is anything else.

**Every run is a fresh mandate.** The pool is spent by one run, and a PEP binds its mandate
at boot (ADR-056, STATUS gap 25), so `make demo-conc` seeds with ``--new`` and then
recreates the three PEPs. Without ``--new`` the seed reuses the current run, which is what
the compose one-shot does: re-minting there would leave running PEPs bound to a mandate
the tokens no longer name.

Three phases:

* ``--out /secrets --new`` — budget row, tokens, and the mandate id file the PEPs read.
* (compose recreates ``pep-conc-a/b/c``)
* ``--drive`` — the burst, then the ledger's own figures from the control plane.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The pool, and what each sibling's token allows per request: the same number. That is
#: INV-5's premise — a parent handing its full ceiling to every child.
POOL_TOTAL: Final = Decimal("15000.0000")
SIBLING_CEILING: Final = POOL_TOTAL
#: Each PEP's lease. Three primed at boot hold 6,000, so 9,000 is raced for during the drive.
LEASE_SIZE: Final = Decimal("2000.0000")
PAYMENT: Final = Decimal("500.0000")
#: 20 x 500 = 10,000 per sibling, inside its own ceiling; 30,000 together, twice the pool.
PAYMENTS_PER_SIBLING: Final = 20

#: (agent, the compose service of the PEP it spends through).
SIBLINGS: Final[tuple[tuple[str, str], ...]] = (
    ("agt-supplier-a", "pep-conc-a"),
    ("agt-supplier-b", "pep-conc-b"),
    ("agt-supplier-c", "pep-conc-c"),
)
SEED_SERVICE: Final = "seed-conc"
#: Where the operator's browser reaches the console — the host side, not this network.
CONSOLE_URL: Final = "http://localhost:8000"

STATE_DIR: Final = "concurrency"
STATE_FILE: Final = "run.json"
MANDATE_ID_FILE: Final = "mandate_id"
TOKENS_FILE: Final = "tokens.json"

INTENT: Final = "Negotiate with three suppliers in parallel, budget BDT 15,000."
PRINCIPAL_ID: Final = "kc:11111111-1111-1111-1111-111111111111"

#: Retry pacing for `LEASE_UNAVAILABLE`. A top-up is one ledger round trip, so a few tens of
#: milliseconds is enough; the cap stops a broken stack from spinning forever.
RETRY_DELAY_S: Final = 0.05
MAX_ATTEMPTS: Final = 40
#: How long to wait for queued settlements to reach the ledger before reading it back.
SETTLE_TIMEOUT_S: Final = 15.0

Outcome = Literal["allowed", "retry", "final"]


def requested_total() -> Decimal:
    """Everything the siblings ask for, together."""
    return len(SIBLINGS) * PAYMENTS_PER_SIBLING * PAYMENT


def classify(status: int, reason: str) -> Outcome:
    """What an agent does with a PEP's answer."""
    if 200 <= status < 300:
        return "allowed"
    if reason == "LEASE_UNAVAILABLE":
        return "retry"
    return "final"


# -- run state ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunIds:
    """One run's mandate and task. A new pair per run, because one run spends the pool."""

    mandate_id: uuid.UUID
    task_id: uuid.UUID


def _state_path(out: Path) -> Path:
    return out / STATE_DIR / STATE_FILE


def resolve_ids(out: Path, *, new: bool) -> RunIds:
    """This run's ids: fresh when asked for, otherwise whatever the last seed wrote."""
    path = _state_path(out)
    if not new and path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        return RunIds(uuid.UUID(state["mandate_id"]), uuid.UUID(state["task_id"]))
    return RunIds(uuid.uuid4(), uuid.uuid4())


def write_state(out: Path, ids: RunIds) -> None:
    """Record the run. The mandate id also goes in a plain file for the PEPs' `cat`."""
    directory = out / STATE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STATE_FILE).write_text(
        json.dumps({"mandate_id": str(ids.mandate_id), "task_id": str(ids.task_id)}),
        encoding="utf-8",
    )
    (directory / MANDATE_ID_FILE).write_text(str(ids.mandate_id), encoding="utf-8")


# -- phase one: seed ---------------------------------------------------------------------


async def _ensure_budget(database_url: str, mandate_id: uuid.UUID) -> bool:
    """Create the run's pool row unless it exists. Returns True if created."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from agentiam_controlplane.db.base import make_session_factory
    from agentiam_controlplane.db.mandates import ensure_row
    from agentiam_controlplane.db.models import BudgetRow
    from agentiam_core.models import BudgetDimension

    engine = create_async_engine(database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            existing = await session.execute(
                select(BudgetRow).where(
                    BudgetRow.mandate_id == mandate_id,
                    BudgetRow.dimension == BudgetDimension.SPEND_BDT.value,
                    BudgetRow.parent_budget_id.is_(None),
                )
            )
            if existing.scalar_one_or_none() is not None:
                return False
            await ensure_row(session, mandate_id, now=datetime.now(UTC))
            session.add(
                BudgetRow(
                    mandate_id=mandate_id,
                    dimension=BudgetDimension.SPEND_BDT.value,
                    total=POOL_TOTAL,
                )
            )
        return True
    finally:
        await engine.dispose()


def mint_tokens(secrets: Path, ids: RunIds, now: datetime) -> dict[str, str]:
    """Mint the root for this run and one sibling token per agent, all under the demo key."""
    from biscuit_auth import Algorithm, PrivateKey, PublicKey

    from agentiam_core.attenuation import attenuate
    from agentiam_core.hashing import intent_hash
    from agentiam_core.models import Budget, BudgetCeiling, BudgetDimension, Mandate, ScopeSubset
    from agentiam_core.tokens import RootKeySet, mint_root, verify

    # Same stub-versus-runtime divergence `seed_demo._mint_chain` documents.
    private_key = PrivateKey.from_bytes(  # type: ignore[call-arg]
        bytes.fromhex((secrets / "root_private_key.hex").read_text(encoding="utf-8").strip()),
        Algorithm.Ed25519,  # type: ignore[attr-defined]
    )
    key_set = RootKeySet(
        [
            PublicKey.from_bytes(  # type: ignore[call-arg]
                bytes.fromhex(
                    (secrets / "root_public_key.hex").read_text(encoding="utf-8").strip()
                ),
                Algorithm.Ed25519,  # type: ignore[attr-defined]
            )
        ]
    )

    mandate = Mandate(
        mandate_id=ids.mandate_id,
        task_id=ids.task_id,
        principal_id=PRINCIPAL_ID,
        intent_hash=intent_hash(INTENT),
        scopes=frozenset({"payment:initiate"}),
        budget=Budget(spend_bdt=POOL_TOTAL, tool_calls=100_000, rows_read=0),
        max_depth=2,
        not_before=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=8),
    )
    root = verify(mint_root(mandate, private_key), key_set, now=now)
    return {
        agent: attenuate(
            root,
            [
                ScopeSubset(scopes=frozenset({"payment:initiate"})),
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=SIBLING_CEILING),
            ],
            agent_id=agent,
            role="senior",
        )
        for agent, _ in SIBLINGS
    }


def seed(out: Path, database_url: str, *, new: bool) -> int:
    """Phase one: pool row, tokens, and the mandate id the PEPs bind."""
    ids = resolve_ids(out, new=new)
    created = asyncio.run(_ensure_budget(database_url, ids.mandate_id))
    write_state(out, ids)
    tokens = mint_tokens(out, ids, datetime.now(UTC))
    (out / STATE_DIR / TOKENS_FILE).write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    print(
        f"pool {POOL_TOTAL:,.0f} BDT for mandate {ids.mandate_id}: "
        + ("created" if created else "already present")
    )
    print(f"minted {len(tokens)} sibling tokens; task id {ids.task_id}")
    return 0


# -- phase two: drive --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Summary:
    """What the burst came to, from the agents' side."""

    allowed_payments: Mapping[str, int]
    final_refusals: Mapping[str, int]
    retries: int
    gave_up: int
    elapsed_s: float

    @property
    def total_allowed(self) -> Decimal:
        """Every payment the PEPs allowed, in BDT."""
        return sum(self.allowed_payments.values()) * PAYMENT

    @property
    def held(self) -> bool:
        """Whether the total stayed inside the pool — the only claim the demo makes."""
        return self.total_allowed <= POOL_TOTAL


async def _burst(tokens: Mapping[str, str], pep_urls: Mapping[str, str]) -> Summary:
    import httpx

    allowed: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    counters = {"retries": 0, "gave_up": 0}

    async def pay(client: httpx.AsyncClient, agent: str, n: int) -> None:
        body = {"amount": str(PAYMENT), "recipient": {"account_id": f"acct_{agent[-1]}{n:02d}"}}
        for attempt in range(MAX_ATTEMPTS):
            response = await client.post(
                f"{pep_urls[agent]}/proxy/payments",
                headers={"Authorization": f"Bearer {tokens[agent]}"},
                json=body,
            )
            try:
                reason = str(response.json().get("reason_code", "OK"))
            except ValueError:
                reason = "OK"
            outcome = classify(response.status_code, reason)
            if outcome == "allowed":
                allowed[agent] += 1
                return
            if outcome == "final":
                refusals[reason] += 1
                return
            counters["retries"] += 1
            await asyncio.sleep(RETRY_DELAY_S * (1 + attempt % 4))
        counters["gave_up"] += 1

    limits = httpx.Limits(max_connections=len(SIBLINGS) * PAYMENTS_PER_SIBLING)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        await asyncio.gather(
            *(pay(client, agent, n) for agent, _ in SIBLINGS for n in range(PAYMENTS_PER_SIBLING))
        )
    return Summary(
        allowed_payments={agent: allowed[agent] for agent, _ in SIBLINGS},
        final_refusals=dict(refusals),
        retries=counters["retries"],
        gave_up=counters["gave_up"],
        elapsed_s=time.perf_counter() - started,
    )


def _ledger_view(
    control_plane_url: str, mandate_id: uuid.UUID, expected_committed: Decimal
) -> tuple[dict[str, Any] | None, bool | None]:
    """The control plane's gauge for this mandate, once settlements have landed."""
    import httpx

    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    gauge: dict[str, Any] | None = None
    invariants_ok: bool | None = None
    while True:
        try:
            payload = httpx.get(f"{control_plane_url}/v1/budgets/dashboard", timeout=5.0).json()
        except (httpx.HTTPError, ValueError):
            return None, None
        invariants_ok = bool(payload.get("invariants_ok"))
        gauge = next(
            (g for g in payload.get("gauges", []) if g.get("mandate_id") == str(mandate_id)),
            None,
        )
        settled = gauge is not None and Decimal(gauge["committed"]) >= expected_committed
        if settled or time.monotonic() >= deadline:
            return gauge, invariants_ok
        time.sleep(0.5)


def _bdt(amount: Decimal | str) -> str:
    return f"{Decimal(amount):,.0f}"


def render(
    summary: Summary,
    siblings: Sequence[tuple[str, str]],
    *,
    gauge: Mapping[str, Any] | None,
    invariants_ok: bool | None = None,
) -> str:
    """The result, as the operator reads it."""
    requested_each = PAYMENTS_PER_SIBLING * PAYMENT
    lines = [
        f"pool {_bdt(POOL_TOTAL)} BDT, shared by {len(siblings)} siblings",
        f"each sibling's token allows up to {_bdt(SIBLING_CEILING)} per request "
        "- every token, on its own, says yes",
        f"requested at once: {len(siblings)} x {PAYMENTS_PER_SIBLING} x {_bdt(PAYMENT)} "
        f"= {_bdt(requested_total())} BDT, through {len(siblings)} separate PEPs",
        "",
    ]
    for agent, pep in siblings:
        count = summary.allowed_payments.get(agent, 0)
        lines.append(
            f"  {agent:16s} via {pep}:  allowed {count:2d} x {_bdt(PAYMENT)} "
            f"= {_bdt(count * PAYMENT):>6s} of {_bdt(requested_each)} asked"
        )
    lines.append("")
    verdict = (
        "held - never more than the pool"
        if summary.held
        else "VIOLATED - more was allowed than the pool holds"
    )
    lines.append(
        f"total allowed: {_bdt(summary.total_allowed)} / {_bdt(POOL_TOTAL)} BDT   {verdict}"
    )
    refused = ", ".join(f"{k}={v}" for k, v in sorted(summary.final_refusals.items()))
    lines.append(f"refused: {refused or 'none'}")
    lines.append(
        f"retried while a lease topped up (LEASE_UNAVAILABLE): {summary.retries}"
        + (f", gave up: {summary.gave_up}" if summary.gave_up else "")
    )
    lines.append(f"burst took {summary.elapsed_s:.2f} s")
    if gauge is not None:
        lamp = {True: "invariants OK", False: "INVARIANTS VIOLATED", None: "invariants unknown"}
        lines += [
            "",
            "ledger, read back from the control plane:",
            f"  total {_bdt(gauge['total'])}  committed {_bdt(gauge['committed'])}  "
            f"leased {_bdt(gauge['leased'])}  available {_bdt(gauge['available'])}  "
            f"- {lamp[invariants_ok]}",
        ]
    return "\n".join(lines)


def drive(out: Path, control_plane_url: str) -> int:
    """Phase two: fire every sibling's payments at once, then read the ledger back."""
    ids = resolve_ids(out, new=False)
    tokens: dict[str, str] = json.loads((out / STATE_DIR / TOKENS_FILE).read_text(encoding="utf-8"))
    pep_urls = {agent: f"http://{pep}:8080" for agent, pep in SIBLINGS}

    print(f"mandate {ids.mandate_id}\n")
    summary = asyncio.run(_burst(tokens, pep_urls))
    gauge, invariants_ok = _ledger_view(control_plane_url, ids.mandate_id, summary.total_allowed)
    print(render(summary, SIBLINGS, gauge=gauge, invariants_ok=invariants_ok))
    print(f"\nwatch: {CONSOLE_URL}/budgets")
    print(f"       {CONSOLE_URL}/decisions")
    return 0 if summary.held and invariants_ok is not False else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without side effects."""
    parser = argparse.ArgumentParser(
        prog="demo_concurrency", description="Seed and drive the sibling-race demo."
    )
    parser.add_argument("--out", type=Path, default=Path("/secrets"))
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--new", action="store_true", help="Start a fresh mandate and pool.")
    parser.add_argument("--drive", action="store_true", help="Fire the burst instead of seeding.")
    parser.add_argument("--control-plane-url", default="http://controlplane:8000")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Seed the run, or drive it. Returns the process exit code."""
    import os

    args = build_parser().parse_args(argv)
    if args.drive:
        return drive(args.out, args.control_plane_url.rstrip("/"))
    database_url = (
        args.database_url
        or os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://agentiam:agentiam@postgres:5432/agentiam"
    )
    return seed(args.out, database_url, new=args.new)


if __name__ == "__main__":
    raise SystemExit(main())
