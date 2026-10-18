"""The refusal ladder again, over MCP `tools/call` — APICTA-06.

`seed_demo --drive` shows each refusal layer over REST. This sends the same rungs as JSON-RPC
`tools/call` messages to `POST /proxy/mcp` and **checks** each against the reason code the REST
ladder produces (`MASTER.md` §7): MCP is a second way in to the same tools, and a second way in
that decided differently would be a hole with a protocol name. Any rung that comes back
different makes the run exit 1, which is what lets the `demo-stack` CI job use it as a test.

Two rungs have no REST twin:

* **An unmapped tool.** Every MCP call has the same path, so "unmapped" is now a property of the
  body; a tool nobody wrote a rule for is refused like an unmapped route.
* **A smuggled tool name.** `"name": "initiate_payment", "name": "get_invoice"` — Python's
  parser keeps the last, so without the refusal the PEP would authorize a read while an
  upstream that keeps the first (several do) ran the payment. TM-26, one level up.

Refusals come back as JSON-RPC errors; the reason is in `error.data.reason_code`.

```bash
make demo-mcp                                                     # inside the demo stack
python scripts/demo_mcp.py --tokens deploy/demo-secrets/demo-tokens.json --pep-url http://localhost:8082
```
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class Rung:
    """One call, who makes it, and the reason code the REST ladder gets for it."""

    label: str
    #: The agent whose token is sent, or None to send no token at all.
    who: str | None
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    expect: str = "OK"
    #: A hand-written body, `%d` standing for the id, for what `json.dumps` cannot produce.
    raw: bytes | None = None

    def body(self, request_id: int = 0) -> bytes:
        """The JSON-RPC request, with `request_id` as its id."""
        if self.raw is not None:
            return self.raw % request_id
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": self.tool, "arguments": self.arguments},
            }
        ).encode()


def _pay(amount: str, account: str) -> dict[str, Any]:
    return {"amount": amount, "recipient": {"account_id": account}}


#: Amounts and agents are `seed_demo`'s, so each rung is refused by the layer it is there to
#: show — and so the expectations are the ones `MASTER.md` §7 records for REST.
LADDER: Final[tuple[Rung, ...]] = (
    Rung("doc-reader reads an invoice", "agt-doc-reader", "get_invoice", {"invoice_id": "inv_001"}),
    Rung(
        "payer settles a small invoice",
        "agt-payer",
        "initiate_payment",
        _pay("1250.0000", "acct_9001"),
    ),
    Rung(
        "doc-reader attempts a payment",
        "agt-doc-reader",
        "initiate_payment",
        _pay("1500.0000", "acct_9001"),
        expect="SCOPE_ATTENUATED_AWAY",
    ),
    Rung(
        "settlement agent exceeds its ceiling",
        "agt-settlement",
        "initiate_payment",
        _pay("30000.0000", "acct_9002"),
        expect="BUDGET_EXHAUSTED_CAVEAT",
    ),
    Rung(
        "root attempts more than the mandate grants",
        "root",
        "initiate_payment",
        _pay("600000.0000", "acct_9003"),
        expect="BUDGET_EXHAUSTED_MANDATE",
    ),
    Rung(
        "sub-contractor is too deep for the policy",
        "agt-subcontractor",
        "initiate_payment",
        _pay("100.0000", "acct_9004"),
        expect="POLICY_DENIED",
    ),
    Rung(
        "doc-reader calls a tool nobody mapped",
        "agt-doc-reader",
        "delete_invoice",
        {"invoice_id": "inv_001"},
        expect="MALFORMED_REQUEST",
    ),
    Rung(
        "doc-reader smuggles a payment behind a read",
        "agt-doc-reader",
        "get_invoice",
        expect="MALFORMED_REQUEST",
        raw=(
            b'{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":'
            b'{"name":"initiate_payment","name":"get_invoice","arguments":'
            b'{"invoice_id":"inv_001","amount":"250000",'
            b'"recipient":{"account_id":"offshore-acct-99"}}}}'
        ),
    ),
    Rung(
        "a call with no token",
        None,
        "get_invoice",
        {"invoice_id": "inv_001"},
        expect="MALFORMED_REQUEST",
    ),
)


def reason_of(response: httpx.Response) -> str:
    """What the PEP decided, read from a JSON-RPC reply.

    `OK` for a successful result; the refusal's reason code for an error; `TOOL_ERROR` when the
    PEP allowed the call and the tool itself failed; `NOT_JSON_RPC` for anything else — a
    REST-shaped body on this path means the envelope was never applied.
    """
    try:
        reply = response.json()
    except ValueError:
        return "NOT_JSON_RPC"
    if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0":
        return "NOT_JSON_RPC"
    if "error" in reply:
        data = reply["error"].get("data") or {}
        return str(data.get("reason_code", "NOT_JSON_RPC"))
    if reply.get("result", {}).get("isError"):
        return "TOOL_ERROR"
    return "OK"


def run(
    payload: dict[str, Any], pep_url: str, *, transport: httpx.BaseTransport | None = None
) -> int:
    """Send every rung and compare. Returns the process exit code: 0 only if all matched."""
    tokens = payload["tokens"]
    mismatches: list[str] = []
    print(f"sending {len(LADDER)} MCP tools/call messages to {pep_url}/proxy/mcp\n")
    with httpx.Client(timeout=30.0, transport=transport) as client:
        for request_id, rung in enumerate(LADDER):
            headers = {"content-type": "application/json"}
            if rung.who is not None:
                headers["authorization"] = f"Bearer {tokens[rung.who]}"
            response = client.post(
                f"{pep_url}/proxy/mcp", content=rung.body(request_id), headers=headers
            )
            got = reason_of(response)
            mark = "ok  " if got == rung.expect else "FAIL"
            print(f"  {mark} {response.status_code}  {rung.label:46s} {got}")
            if got != rung.expect:
                mismatches.append(f"{rung.label}: expected {rung.expect}, got {got}")

    if mismatches:
        print("\nMCP decided differently from REST:")
        for line in mismatches:
            print(f"  {line}")
        return 1
    print(f"\nall {len(LADDER)} rungs decided as the REST ladder decides them")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without side effects."""
    parser = argparse.ArgumentParser(
        prog="demo_mcp", description="Send the refusal ladder over MCP tools/call (APICTA-06)."
    )
    parser.add_argument("--tokens", type=Path, required=True, help="seed_demo's tokens file.")
    parser.add_argument("--pep-url", default="http://localhost:8082")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ladder. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    payload = json.loads(args.tokens.read_text(encoding="utf-8"))
    return run(payload, args.pep_url.rstrip("/"))


if __name__ == "__main__":
    sys.exit(main())
