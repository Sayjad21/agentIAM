"""A payment the PEP cannot price is not a payment it may authorize — gap 37, APICTA-09.

Spec 02 §3.2 makes an absent argument vacuous: `ArgPredicate` compiles to `reject if`, so a
predicate over an argument the request does not carry never fires. That is right for a filter
on an optional field. It is wrong for the one argument the pipeline *spends* on: with no
`payment.amount` the request asks for ৳0, nothing is reserved, the ceiling caveat and the
policy's amount condition are both vacuous, and the payment is authorized. Measured (APICTA-06):
`authorize(POST /payments, body=None)` returned `Authorized` and the pool did not move.

Three routes led there, and each is closed here:

1. the amount is simply absent from the body;
2. the body is labelled something other than JSON, so spec 10 §6 never read it — while an
   upstream that parses JSON regardless of content type (Go's `json.Decoder` on a raw body
   does) still executes the payment;
3. a route maps `payment.amount` without `:number`, so it extracts as text, and the pipeline —
   which only prices an integer — reserves nothing.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from agentiam_core.errors import ReasonCode
from agentiam_core.models import BudgetDimension
from agentiam_pep.extractor import SPEND_ARG, RouteTable, extract
from agentiam_pep.pipeline import Authorized, Refused
from scripts.serve_pep import ROUTES
from tests.unit.test_pep_mcp_gateway import a_gateway
from tests.unit.test_pep_pipeline import a_mandate, a_pipeline, bearer

PAYMENT: dict[str, object] = {
    "method": "POST",
    "path": "/payments",
    "scope": "payment:initiate",
    "tool": "payment_api",
    "args": {SPEND_ARG: "body.amount:number", "payment.to": "body.recipient.account_id"},
}
EMAIL: dict[str, object] = {
    "method": "POST",
    "path": "/email/send",
    "scope": "email:send",
    "tool": "email_internal",
    "args": {"email.to": "body.to"},
}
JSON_HEADERS = [("content-type", "application/json")]


def table(*rules: dict[str, object]) -> RouteTable:
    return RouteTable.from_config({"routes": list(rules)})


class TestTheSpendArgumentIsRequired:
    async def test_a_payment_without_an_amount_is_refused(self) -> None:
        pipeline, _, pool = await a_pipeline()
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=[*bearer(a_mandate()), *JSON_HEADERS],
            body=b'{"recipient": {"account_id": "acct_1"}}',
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.MALFORMED_REQUEST
        assert "payment.amount" in result.detail
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before

    async def test_a_payment_with_no_body_is_refused(self) -> None:
        """The measured case: what the gateway passes when it did not read the body."""
        pipeline, _, pool = await a_pipeline()
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        result = await pipeline.authorize(
            method="POST", path="/payments", headers=bearer(a_mandate()), body=None
        )
        assert isinstance(result, Refused)
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before

    async def test_a_priced_payment_is_still_authorized(self) -> None:
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(
            method="POST",
            path="/payments",
            headers=[*bearer(a_mandate()), *JSON_HEADERS],
            body=b'{"amount": "10.0000", "recipient": {"account_id": "acct_1"}}',
        )
        assert isinstance(result, Authorized)

    async def test_a_route_that_spends_nothing_is_unaffected(self) -> None:
        pipeline, _, _ = await a_pipeline()
        result = await pipeline.authorize(
            method="GET", path="/invoices/inv_001", headers=bearer(a_mandate())
        )
        assert isinstance(result, Authorized)

    async def test_an_mcp_payment_without_an_amount_is_refused(self) -> None:
        pipeline, _, _ = await a_pipeline(routes=ROUTES)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "initiate_payment", "arguments": {"recipient": {"account_id": "a"}}},
        }
        result = await pipeline.authorize(
            method="POST",
            path="/mcp",
            headers=[*bearer(a_mandate()), *JSON_HEADERS],
            body=json.dumps(body).encode(),
        )
        assert isinstance(result, Refused)
        assert result.reason_code is ReasonCode.MALFORMED_REQUEST


class TestTheSpendArgumentIsNumeric:
    def test_a_route_mapping_it_as_text_is_a_load_error(self) -> None:
        """Text is never priced, so such a route could only ever spend ৳0."""
        rule = PAYMENT | {"args": {SPEND_ARG: "body.amount"}}
        with pytest.raises(ValueError, match=r"payment\.amount"):
            table(rule)

    def test_the_shipped_table_declares_it_numeric_everywhere(self) -> None:
        RouteTable.from_config(ROUTES)


class TestABodyRouteIsReadWhateverItsLabel:
    """Spec 10 §5: the PEP must read the request the way the upstream will.

    The gateway used to read a body only when its `content-type` said JSON, so a payment
    labelled `text/plain` carried no amount to us while an upstream that parses JSON
    regardless — Go's `json.Decoder` on a raw body does — executed it. Refusing non-JSON
    labels would also have closed it, and broken spec 10 §6's legitimate case: a form upload to
    a route whose caveats do not constrain it. Reading the body is the rule MCP already
    follows.
    """

    def test_the_table_knows_which_requests_it_must_read(self) -> None:
        routes = table(EMAIL, {"method": "GET", "path": "/ping", "scope": "s", "tool": "t"})
        assert routes.reads_body("POST", "/email/send")
        assert not routes.reads_body("GET", "/ping")
        assert not routes.reads_body("POST", "/unmapped")

    async def test_a_payment_labelled_text_is_read_and_priced(self) -> None:
        pipeline, _, pool = await a_pipeline(routes=ROUTES)
        client, upstream = await a_gateway(pipeline)
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        async with client:
            await client.post(
                "/proxy/payments",
                content=b'{"amount": "25.0000", "recipient": {"account_id": "x"}}',
                headers={"content-type": "text/plain", **dict(bearer(a_mandate()))},
            )
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before - Decimal(25)
        assert upstream.calls == 1

    async def test_an_unaffordable_payment_labelled_text_never_reaches_the_tool(self) -> None:
        """The measured case: 400,000 under a policy capped at 100,000, sent as text."""
        pipeline, _, pool = await a_pipeline(routes=ROUTES)
        client, upstream = await a_gateway(pipeline)
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        async with client:
            response = await client.post(
                "/proxy/payments",
                content=b'{"amount": "400000", "recipient": {"account_id": "x"}}',
                headers={"content-type": "text/plain", **dict(bearer(a_mandate()))},
            )
        assert response.status_code == 403
        assert response.json()["reason_code"] == "POLICY_DENIED"
        assert upstream.calls == 0
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before

    def test_a_body_that_is_not_json_still_yields_no_values(self) -> None:
        """Spec 10 §6's form upload, to a route that does not spend, is not refused."""
        extraction = extract(
            table(EMAIL),
            method="POST",
            path="/email/send",
            headers=[("content-type", "application/x-www-form-urlencoded")],
            body=b"to=x%40example.com",
        )
        assert extraction.args == {}
