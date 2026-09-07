"""Put the demo stack into a state worth looking at — T-057, `ROADMAP.md` M12.

`bootstrap_demo_secrets.py` stops at "the containers can start and enforce something real".
This is the next step: a mandate with an actual budget, a delegation tree with agents that
differ from each other, and enough traffic through the PEP that every console page has
something on it.

**Why this exists at all.** Every console page rendered correctly and was empty on a fresh
install, and that is what let two critical defects sit unnoticed — the PEP never primed its
lease pool, and it evaluated no token caveats. Both were found by hand-building this
scenario in a scratchpad. A committed version is the difference between "the pages work"
and "the pages show the product working".

Two phases, because the PEP binds its mandate at boot (ADR-056):

* ``python scripts/seed_demo.py --out /secrets`` — **before the PEP starts.** Creates the
  budget pool, mints the token chain, writes them next to the bootstrap credentials. Runs
  as a one-shot compose service between ``migrate`` and ``pep``, the same shape
  ``bootstrap`` already uses.
* ``python scripts/seed_demo.py --drive --tokens /secrets/demo-tokens.json`` — **after the
  stack is healthy.** Sends the scenario's traffic through the PEP so decisions, budgets,
  the audit chain and the identity tree fill in.

**The mandate id is fixed, not random.** `docker-compose.demo.yml` has to name it in the
PEP's environment before this script has run, so the two must agree on a constant. It also
makes the seed idempotent and the printed console URLs stable between runs, which matters
when the thing you are demonstrating is a URL you typed into a slide.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentiam_core.models import Mandate

#: Named in `docker-compose.demo.yml` before this script runs, so it cannot be random.
DEMO_MANDATE_ID: Final = uuid.UUID("d0d0d0d0-0000-4000-8000-000000000001")
DEMO_TASK_ID: Final = uuid.UUID("d0d0d0d0-0000-4000-8000-000000000002")

#: `DEMO.md` beat 1's figure: "Procure 500 units, budget ৳500,000."
POOL_TOTAL: Final = Decimal("500000.0000")
DEMO_INTENT: Final = "Procure 500 units of packaging stock, budget BDT 500,000."

DEFAULT_TOKENS_FILE: Final = "demo-tokens.json"

#: The delegation tree. Each child is *strictly* narrower than the root, and they differ
#: from one another — three agents that all held the same authority would demonstrate
#: nothing. `DEMO.md` beat 2 is "root spawns 3 sub-agents, each node's scopes visibly
#: smaller"; this is that, with the ceilings that make beat 3 and beat 4 land.
_CHILDREN: Final[tuple[tuple[str, str, frozenset[str], Decimal], ...]] = (
    # Reads documents, spends nothing. Beat 3's "the doc-reader attempts a payment".
    ("agt-doc-reader", "reader", frozenset({"invoice:read", "vendor:read"}), Decimal("0.0000")),
    # Talks to vendors, cannot read invoices and cannot pay.
    ("agt-negotiator", "worker", frozenset({"vendor:read"}), Decimal("50000.0000")),
    # Pays, and cannot read anything.
    ("agt-payer", "payer", frozenset({"payment:initiate"}), Decimal("200000.0000")),
)

#: Two more levels, to prove the tree is a tree and not a star. Each ceiling is a slice of
#: its parent's, which is the attenuation invariant made visible.
#:
#: `agt-subcontractor` sits at **depth 3**, and that is the point of it: the signed corpus
#: bundle permits `payment:initiate` only `when amount <= 500000 && principal.depth <= 2`,
#: so its payments are refused by *Cedar* while its token is perfectly valid. Without it the
#: scenario has no POLICY_DENIED in it at all — the mandate's own ceiling and the policy's
#: are both 500,000, so no amount can be refused by one and not the other.
_DESCENDANTS: Final[tuple[tuple[str, str, str, frozenset[str], Decimal], ...]] = (
    (
        "agt-payer",
        "agt-settlement",
        "payer",
        frozenset({"payment:initiate"}),
        Decimal("25000.0000"),
    ),
    (
        "agt-settlement",
        "agt-subcontractor",
        "payer",
        frozenset({"payment:initiate"}),
        Decimal("5000.0000"),
    ),
)


def _mandate(now: datetime) -> Mandate:
    """The demo grant. Fixed ids so compose can name the mandate before this runs."""
    from agentiam_core.models import Budget, Mandate

    return Mandate(
        mandate_id=DEMO_MANDATE_ID,
        task_id=DEMO_TASK_ID,
        principal_id="kc:11111111-1111-1111-1111-111111111111",
        intent_hash=hashlib.sha256(DEMO_INTENT.encode()).hexdigest(),
        scopes=frozenset({"invoice:read", "vendor:read", "payment:initiate"}),
        budget=Budget(spend_bdt=POOL_TOTAL, tool_calls=1000, rows_read=100_000),
        max_depth=4,
        not_before=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=8),
    )


async def _ensure_budget(database_url: str) -> bool:
    """Create the mandate's pool row if it is not already there. Returns True if created.

    Idempotent because compose re-runs one-shot services on every `up`, and because a
    second pool row for the same mandate would make `ACQUIRE`'s lookup ambiguous — it does
    `scalar_one()`, so two rows raise `MultipleResultsFound` rather than picking one.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from agentiam_controlplane.db.base import make_session_factory
    from agentiam_controlplane.db.models import BudgetRow
    from agentiam_core.models import BudgetDimension

    engine = create_async_engine(database_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session, session.begin():
            existing = await session.execute(
                select(BudgetRow).where(
                    BudgetRow.mandate_id == DEMO_MANDATE_ID,
                    BudgetRow.dimension == BudgetDimension.SPEND_BDT.value,
                    BudgetRow.parent_budget_id.is_(None),
                )
            )
            if existing.scalar_one_or_none() is not None:
                return False
            session.add(
                BudgetRow(
                    mandate_id=DEMO_MANDATE_ID,
                    dimension=BudgetDimension.SPEND_BDT.value,
                    total=POOL_TOTAL,
                )
            )
        return True
    finally:
        await engine.dispose()


def _mint_chain(secrets: Path, now: datetime) -> dict[str, Any]:
    """Mint the root and its descendants against the bootstrap root key."""
    from biscuit_auth import Algorithm, PrivateKey, PublicKey

    from agentiam_core.attenuation import attenuate
    from agentiam_core.models import BudgetCeiling, BudgetDimension, ScopeSubset
    from agentiam_core.tokens import RootKeySet, mint_root, verify

    # biscuit-python has no `from_hex`, and `from_bytes` needs the algorithm explicitly —
    # the same stub-versus-runtime divergence `pep_service._root_keys` documents.
    # The `type: ignore`s are the ones `pep_service._root_keys` documents: biscuit-python's
    # bundled stubs declare `from_bytes(cls, data)` and omit `Algorithm` entirely, while the
    # runtime requires the algorithm as a second argument. Measured, not assumed.
    private_key = PrivateKey.from_bytes(  # type: ignore[call-arg]
        bytes.fromhex((secrets / "root_private_key.hex").read_text(encoding="utf-8").strip()),
        Algorithm.Ed25519,  # type: ignore[attr-defined]
    )
    public_hex = (secrets / "root_public_key.hex").read_text(encoding="utf-8").strip()
    key_set = RootKeySet(
        [
            PublicKey.from_bytes(  # type: ignore[call-arg]
                bytes.fromhex(public_hex),
                Algorithm.Ed25519,  # type: ignore[attr-defined]
            )
        ]
    )

    mandate = _mandate(now)
    root = mint_root(mandate, private_key)
    verified_root = verify(root, key_set, now=now)

    tokens: dict[str, str] = {"root": root}
    roles: dict[str, str] = {"root": "root"}
    for agent_id, role, scopes, ceiling in _CHILDREN:
        tokens[agent_id] = attenuate(
            verified_root,
            [
                ScopeSubset(scopes=scopes),
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=ceiling),
            ],
            agent_id=agent_id,
            role=role,
        )
        roles[agent_id] = role

    for parent_id, agent_id, role, scopes, ceiling in _DESCENDANTS:
        parent = verify(tokens[parent_id], key_set, now=now)
        tokens[agent_id] = attenuate(
            parent,
            [
                ScopeSubset(scopes=scopes),
                BudgetCeiling(dimension=BudgetDimension.SPEND_BDT, value=ceiling),
            ],
            agent_id=agent_id,
            role=role,
        )
        roles[agent_id] = role

    return {
        "mandate_id": str(DEMO_MANDATE_ID),
        "task_id": str(DEMO_TASK_ID),
        "principal_id": mandate.principal_id,
        "intent": DEMO_INTENT,
        "pool_total": str(POOL_TOTAL),
        "roles": roles,
        "tokens": tokens,
    }


def seed(out: Path, database_url: str) -> int:
    """Phase one: budget row, token chain, tokens file."""
    created = asyncio.run(_ensure_budget(database_url))
    print(
        f"budget pool {POOL_TOTAL} for mandate {DEMO_MANDATE_ID}: "
        + ("created" if created else "already present")
    )

    out.mkdir(parents=True, exist_ok=True)
    payload = _mint_chain(out, datetime.now(UTC))
    (out / DEFAULT_TOKENS_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"minted {len(payload['tokens'])} tokens -> {out / DEFAULT_TOKENS_FILE}")
    print(f"task id: {DEMO_TASK_ID}")
    return 0


#: The traffic. Each line is (label, token, method, path, body, why it is in the demo).
#: Chosen so every console page has something to show and every distinct refusal is
#: represented: allows, a scope refusal, a caveat-ceiling refusal, the **mandate's own**
#: ceiling, and a policy refusal.
#:
#: The last two were one line in the console until TODO item 17 — the mandate's own
#: per-request ceiling reported `BUDGET_EXHAUSTED_CAVEAT`, so "root attempts more than the
#: mandate grants" and "settlement agent exceeds its ceiling" read as the same refusal. They
#: are not: one is an agent narrowing itself, the other is the grant it never had.
_TRAFFIC: Final[tuple[tuple[str, str, str, str, dict[str, Any] | None], ...]] = (
    ("root reads an invoice", "root", "GET", "/proxy/invoices/inv_001", None),
    ("root reads another", "root", "GET", "/proxy/invoices/inv_002", None),
    ("doc-reader reads an invoice", "agt-doc-reader", "GET", "/proxy/invoices/inv_001", None),
    ("doc-reader reads a third", "agt-doc-reader", "GET", "/proxy/invoices/inv_003", None),
    # Beat 3: the read-only agent attempts a payment. Refused by its own scope subset.
    (
        "doc-reader attempts a payment",
        "agt-doc-reader",
        "POST",
        "/proxy/payments",
        {"amount": "1500.0000", "recipient": {"account_id": "acct_9001"}},
    ),
    # The negotiator's own scope. Without a call of its own it has no decisions, and the
    # identity tree — derived from the audit chain — would not show it at all.
    ("negotiator looks up a vendor", "agt-negotiator", "GET", "/proxy/vendors/ven_01", None),
    # And the boundary of that scope: it holds vendor:read and nothing else.
    (
        "negotiator attempts to read an invoice",
        "agt-negotiator",
        "GET",
        "/proxy/invoices/inv_001",
        None,
    ),
    # Refused by scope too, the other way round: the payer holds no read scope.
    ("payer attempts to read an invoice", "agt-payer", "GET", "/proxy/invoices/inv_001", None),
    (
        "payer settles a small invoice",
        "agt-payer",
        "POST",
        "/proxy/payments",
        {"amount": "1250.0000", "recipient": {"account_id": "acct_9001"}},
    ),
    (
        "settlement agent pays within its slice",
        "agt-settlement",
        "POST",
        "/proxy/payments",
        {"amount": "900.0000", "recipient": {"account_id": "acct_9002"}},
    ),
    # Beat 4's shape: over the child's own ceiling, well under the mandate's.
    (
        "settlement agent exceeds its ceiling",
        "agt-settlement",
        "POST",
        "/proxy/payments",
        {"amount": "30000.0000", "recipient": {"account_id": "acct_9002"}},
    ),
    # Over the *mandate's* own ceiling, so the token refuses it before Cedar is consulted.
    (
        "root attempts more than the mandate grants",
        "root",
        "POST",
        "/proxy/payments",
        {"amount": "600000.0000", "recipient": {"account_id": "acct_9003"}},
    ),
    # Depth 3, and the signed bundle permits payments only at `principal.depth <= 2`. The
    # token is entirely valid; this is the *policy* layer refusing, which no other line
    # here demonstrates.
    (
        "sub-contractor is too deep for the policy",
        "agt-subcontractor",
        "POST",
        "/proxy/payments",
        {"amount": "100.0000", "recipient": {"account_id": "acct_9004"}},
    ),
)


def drive(tokens_file: Path, pep_url: str, control_plane_url: str) -> int:
    """Phase two: send the scenario's traffic so the console pages have content."""
    import httpx

    payload = json.loads(tokens_file.read_text(encoding="utf-8"))
    tokens = payload["tokens"]

    print(f"driving {len(_TRAFFIC)} calls through {pep_url}\n")
    outcomes: dict[str, int] = {}
    with httpx.Client(timeout=30.0) as client:
        for label, who, method, path, body in _TRAFFIC:
            response = client.request(
                method,
                f"{pep_url}{path}",
                headers={"Authorization": f"Bearer {tokens[who]}"},
                json=body,
            )
            try:
                reason = response.json().get("reason_code", "OK")
            except ValueError:
                reason = "OK"
            verdict = "allow" if response.is_success else "deny "
            outcomes[reason] = outcomes.get(reason, 0) + 1
            print(f"  {verdict} {response.status_code}  {label:44s} {reason}")

    print("\noutcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
    print("\nNow look at:")
    print(f"  {control_plane_url}/                      overview")
    print(f"  {control_plane_url}/decisions             every call above")
    print(f"  {control_plane_url}/budgets               the pool moving")
    print(f"  {control_plane_url}/identity-tree?task_id={payload['task_id']}")
    print(f"  {control_plane_url}/audit                 the hash-chained ledger")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without side effects."""
    parser = argparse.ArgumentParser(
        prog="seed_demo", description="Seed and drive the AgentIAM demo scenario (T-057)."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_REPO_ROOT / "deploy" / "demo-secrets",
        help="Where the bootstrap credentials live; the tokens file is written beside them.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Ledger DSN. Defaults to $DATABASE_URL, then the compose service name.",
    )
    parser.add_argument(
        "--drive",
        action="store_true",
        help="Send the scenario's traffic through a running PEP instead of seeding.",
    )
    parser.add_argument("--pep-url", default="http://localhost:8082")
    parser.add_argument("--control-plane-url", default="http://localhost:8000")
    parser.add_argument(
        "--tokens",
        type=Path,
        default=None,
        help=f"The tokens file --drive reads. Defaults to <--out>/{DEFAULT_TOKENS_FILE}.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Seed the scenario, or drive it. Returns the process exit code."""
    import os

    args = build_parser().parse_args(argv)

    if args.drive:
        return drive(
            args.tokens or (args.out / DEFAULT_TOKENS_FILE),
            args.pep_url.rstrip("/"),
            args.control_plane_url.rstrip("/"),
        )

    database_url = (
        args.database_url
        or os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://agentiam:agentiam@postgres:5432/agentiam"
    )
    return seed(args.out, database_url)


if __name__ == "__main__":
    raise SystemExit(main())
