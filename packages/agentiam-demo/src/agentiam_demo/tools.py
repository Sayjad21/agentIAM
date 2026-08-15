"""Stub tool servers — M4, `PLAN.md` §5.

Four upstreams for the PEP to protect: invoices, vendors, payments, email. They are stubs in
the sense that they persist nothing and call nothing, not in the sense that they are vague:
the PEP's whole job is to decide *before* a real side effect happens, so what sits behind it
only has to be a believable HTTP surface.

**Everything here is deterministic.** No clock, no randomness, no `uuid4()` at request time.
`ROADMAP.md` M6 asks for scripted, deterministic demo beats, and a stub that returns a
different id each run makes a screencast disagree with a live run for no reason. Ids are
derived from the request instead.

**Money is `Decimal`, rendered as a string.** Rule 6 does not stop applying because this is a
stub — a float here would come back through the PEP's commit path and into the ledger.

MCP versions of these are T-041/T-042 and deferred (`PLAN.md` §21); these speak plain HTTP,
which is what the T-018 gateway proxies.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any, Final

from fastapi import FastAPI, HTTPException

__all__ = ["INVOICES", "VENDORS", "create_tools_app"]

#: Fixed seed data. Amounts are strings so they never touch a float on the way in or out.
INVOICES: Final[dict[str, dict[str, Any]]] = {
    "inv_001": {"id": "inv_001", "vendor_id": "ven_01", "total": "12500.0000", "status": "open"},
    "inv_002": {"id": "inv_002", "vendor_id": "ven_01", "total": "480000.0000", "status": "open"},
    "inv_003": {"id": "inv_003", "vendor_id": "ven_02", "total": "9900.5000", "status": "paid"},
}

VENDORS: Final[dict[str, dict[str, Any]]] = {
    "ven_01": {"id": "ven_01", "name": "Padma Supplies", "account_id": "acct_1001"},
    "ven_02": {"id": "ven_02", "name": "Jamuna Traders", "account_id": "acct_1002"},
}


#: ASCII unit separator, written as an escape rather than pasted in: a raw control
#: character in source is invisible in an editor and survives exactly one careless edit.
#: Joining with a character that cannot occur in the parts is what stops ('a', 'bc') and
#: ('ab', 'c') hashing to the same id.
_SEPARATOR: Final = "\x1f"


def _stable_id(prefix: str, *parts: str) -> str:
    """A stable id derived from the request.

    `sha256`, not `hash()`. Measured: Python randomizes string hashing per process
    (`PYTHONHASHSEED`), so `hash()` gives a different id every run — which would make a demo
    re-run disagree with its own screencast, the precise failure this determinism exists to
    avoid. The bug was written here first and caught by running it three times.

    A real payment service mints its own id; this is the seam where that difference lives.
    """
    digest = hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:12]}"


def create_tools_app() -> FastAPI:
    """One ASGI app carrying all four stub tools.

    One app rather than four processes because the PEP proxies by path, and the demo's point
    is the gateway in front of them rather than the topology behind it. Splitting them is a
    compose-file change, not a code change.
    """
    app = FastAPI(title="AgentIAM stub tools", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness, so compose can wait on it."""
        return {"status": "ok"}

    # -- invoices ------------------------------------------------------------------

    @app.get("/invoices")
    async def list_invoices(limit: int = 50) -> dict[str, Any]:
        """List invoices, honouring `limit` so the extractor's query mapping has a target."""
        items = list(INVOICES.values())[:limit]
        return {"items": items, "count": len(items)}

    @app.get("/invoices/{invoice_id}")
    async def get_invoice(invoice_id: str) -> dict[str, Any]:
        """One invoice, or 404."""
        invoice = INVOICES.get(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail=f"no invoice {invoice_id}")
        return invoice

    # -- vendors -------------------------------------------------------------------

    @app.get("/vendors")
    async def list_vendors() -> dict[str, Any]:
        """List vendors."""
        return {"items": list(VENDORS.values()), "count": len(VENDORS)}

    @app.get("/vendors/{vendor_id}")
    async def get_vendor(vendor_id: str) -> dict[str, Any]:
        """One vendor, or 404."""
        vendor = VENDORS.get(vendor_id)
        if vendor is None:
            raise HTTPException(status_code=404, detail=f"no vendor {vendor_id}")
        return vendor

    # -- payments ------------------------------------------------------------------

    @app.post("/payments")
    async def initiate_payment(body: dict[str, Any]) -> dict[str, Any]:
        """Accept a payment and echo what was charged.

        The echoed `amount` is what the PEP settles against at step 9 — spec 04 §4.3's
        `actual`, which may differ from what was reserved. Returning it is what makes the
        commit path exercisable end to end.
        """
        raw_amount = body.get("amount")
        recipient = body.get("recipient", {})
        account_id = recipient.get("account_id") if isinstance(recipient, dict) else None
        if raw_amount is None or account_id is None:
            raise HTTPException(status_code=400, detail="amount and recipient.account_id required")
        try:
            amount = Decimal(str(raw_amount))
        except Exception as exc:  # a malformed amount is the caller's error, not a crash
            raise HTTPException(status_code=400, detail="amount is not a number") from exc

        return {
            "payment_id": _stable_id("pay", str(account_id), f"{amount:f}"),
            "account_id": account_id,
            "amount": f"{amount:f}",
            "status": "accepted",
        }

    # -- email ---------------------------------------------------------------------

    @app.post("/email/send")
    async def send_email(body: dict[str, Any]) -> dict[str, Any]:
        """Accept an email. Sends nothing, which is the entire point of a stub here."""
        to = body.get("to")
        if not to:
            raise HTTPException(status_code=400, detail="to is required")
        return {"message_id": _stable_id("msg", str(to)), "to": to, "status": "queued"}

    return app
