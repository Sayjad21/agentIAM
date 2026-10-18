"""MCP `tools/call` through the gateway — APICTA-06.

The extractor tests settle which rule a JSON-RPC body selects. These settle what an MCP client
sees: the same decision a REST client gets for the same call, with the refusal wrapped in a
JSON-RPC error envelope rather than a bare body, at the same HTTP status.

`TestParity` is the class that matters. MCP is a second way in to the same tools, and a second
way in that decides differently from the first is a hole with a protocol name. So every rung of
the refusal ladder is sent both ways and the two answers compared, rather than each asserted
against a reason code chosen here.

The upstream is the real stub tools app (`agentiam_demo.tools`) and the table is the shipped one
(`scripts.serve_pep.ROUTES`), so these also prove the demo's MCP surface and its route table
agree with each other.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from agentiam_core.errors import ReasonCode
from agentiam_core.models import BudgetDimension
from agentiam_demo.tools import INVOICES, MCP_TOOLS, create_tools_app
from agentiam_pep.app import MCP_REFUSAL_CODE, create_app
from agentiam_pep.config import PepSettings
from agentiam_pep.extractor import RouteTable
from scripts.serve_pep import ROUTES
from tests.unit.test_pep_pipeline import a_mandate, a_pipeline, bearer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import ASGIApp, Receive, Scope, Send

    from agentiam_pep.pipeline import Pipeline


class CountingUpstream:
    """The stub tools app, counting what reaches it — a refusal must count nothing."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`."""
        self.app = app
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.calls += 1
        await self.app(scope, receive, send)


class Gateway:
    def __init__(self, client: httpx.AsyncClient, upstream: CountingUpstream) -> None:
        """Hold the client and the upstream counter."""
        self.client = client
        self.upstream = upstream

    async def mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        request_id: object = 1,
        content_type: str = "application/json",
    ) -> httpx.Response:
        body: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        if request_id is not None:
            body["id"] = request_id
        return await self.client.post(
            "/proxy/mcp",
            content=json.dumps(body).encode(),
            headers={"content-type": content_type, **(headers or {})},
        )


async def a_gateway(pipeline: Pipeline) -> tuple[httpx.AsyncClient, CountingUpstream]:
    upstream = CountingUpstream(create_tools_app())
    tools = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream), base_url="http://tools")
    app = create_app(
        settings=PepSettings(upstream_base_url="http://tools"),
        upstream_client=tools,
        pipeline=pipeline,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pep")
    return client, upstream


@pytest.fixture
async def gateway() -> AsyncIterator[Gateway]:
    pipeline, _, _ = await a_pipeline(routes=ROUTES)
    client, upstream = await a_gateway(pipeline)
    async with client:
        yield Gateway(client, upstream)


def a_payment(amount: str) -> dict[str, Any]:
    return {"amount": amount, "recipient": {"account_id": "acct_9001"}}


class TestAllowed:
    async def test_a_read_reaches_the_tool_and_comes_back_as_json_rpc(
        self, gateway: Gateway
    ) -> None:
        response = await gateway.mcp(
            "get_invoice", {"invoice_id": "inv_001"}, headers=dict(bearer(a_mandate()))
        )
        assert response.status_code == 200
        reply = response.json()
        assert reply["id"] == 1
        assert reply["result"]["structuredContent"] == INVOICES["inv_001"]
        assert gateway.upstream.calls == 1

    async def test_a_payment_spends_from_the_pool(self) -> None:
        pipeline, _, pool = await a_pipeline(routes=ROUTES)
        client, _ = await a_gateway(pipeline)
        before = pool.remaining(BudgetDimension.SPEND_BDT)
        async with client:
            response = await Gateway(client, CountingUpstream(create_tools_app())).mcp(
                "initiate_payment", a_payment("250.0000"), headers=dict(bearer(a_mandate()))
            )
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"]["amount"] == "250.0000"
        assert pool.remaining(BudgetDimension.SPEND_BDT) == before - 250

    async def test_the_body_is_read_whatever_its_content_type_claims(
        self, gateway: Gateway
    ) -> None:
        """A tool call's identity is in the body, so a `text/plain` label cannot hide it.

        Over REST a non-JSON body yields no arguments (spec 10 §6) and the request proceeds; if
        that held here, the rule could not match and every such call would be refused as
        unmapped — fail-closed, but only by accident. This pins that the body is read.
        """
        response = await gateway.mcp(
            "initiate_payment",
            a_payment("150000.0000"),
            headers=dict(bearer(a_mandate())),
            content_type="text/plain",
        )
        error = response.json()["error"]
        assert error["data"]["reason_code"] != ReasonCode.MALFORMED_REQUEST.value


class TestRefusalEnvelope:
    async def test_a_refusal_is_a_json_rpc_error_with_the_request_id(
        self, gateway: Gateway
    ) -> None:
        response = await gateway.mcp("get_invoice", {"invoice_id": "inv_001"}, request_id="r-9")
        reply = response.json()
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == "r-9"
        assert reply["error"]["code"] == MCP_REFUSAL_CODE
        assert reply["error"]["data"]["reason_code"] == ReasonCode.MALFORMED_REQUEST.value
        assert reply["error"]["message"]
        assert "result" not in reply

    async def test_the_envelope_is_labelled_json(self, gateway: Gateway) -> None:
        """The MCP SDK reads an error body at a 4xx only when it says `application/json`."""
        response = await gateway.mcp("get_invoice", {"invoice_id": "inv_001"})
        assert response.headers["content-type"].startswith("application/json")

    async def test_the_data_carries_everything_the_rest_body_does(self, gateway: Gateway) -> None:
        """`decision_id` and `escalation_id` travel here, or a client cannot follow up."""
        mandate = a_mandate(scopes=frozenset({"invoice:read"}))
        response = await gateway.mcp(
            "initiate_payment", a_payment("10.0000"), headers=dict(bearer(mandate))
        )
        data = response.json()["error"]["data"]
        assert {"reason_code", "detail", "trace_id", "decision_id"} <= set(data)

    async def test_a_notification_is_answered_with_a_null_id(self, gateway: Gateway) -> None:
        response = await gateway.mcp("get_invoice", {}, request_id=None)
        assert response.json()["id"] is None

    async def test_a_refusal_never_reaches_the_tool(self, gateway: Gateway) -> None:
        await gateway.mcp("initiate_payment", a_payment("10.0000"))
        await gateway.mcp("delete_everything", {}, headers=dict(bearer(a_mandate())))
        assert gateway.upstream.calls == 0

    async def test_an_unmapped_method_is_refused_as_json_rpc(self, gateway: Gateway) -> None:
        """`tools/list` is not mapped: T-041's scope-filtered listing is not built."""
        response = await gateway.client.post(
            "/proxy/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            headers=dict(bearer(a_mandate())),
        )
        assert response.json()["error"]["data"]["reason_code"] == "MALFORMED_REQUEST"
        assert response.json()["id"] == 3
        assert gateway.upstream.calls == 0

    async def test_a_smuggled_second_name_is_refused(self, gateway: Gateway) -> None:
        raw = (
            b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":'
            b'{"name":"get_invoice","name":"initiate_payment",'
            b'"arguments":{"invoice_id":"inv_001","amount":"400000"}}}'
        )
        response = await gateway.client.post(
            "/proxy/mcp",
            content=raw,
            headers={"content-type": "application/json", **dict(bearer(a_mandate()))},
        )
        assert response.json()["error"]["data"]["reason_code"] == "MALFORMED_REQUEST"
        assert gateway.upstream.calls == 0

    async def test_rest_refusals_keep_their_plain_body(self, gateway: Gateway) -> None:
        response = await gateway.client.get("/proxy/invoices/inv_001")
        assert "jsonrpc" not in response.json()
        assert response.json()["reason_code"] == "MALFORMED_REQUEST"


class TestParity:
    """The same call, sent over REST and over MCP, must be decided the same way."""

    @staticmethod
    async def both(
        gateway: Gateway,
        *,
        rest: tuple[str, str, dict[str, Any] | None],
        tool: str,
        arguments: dict[str, Any],
        headers: dict[str, str],
    ) -> tuple[httpx.Response, httpx.Response]:
        method, path, body = rest
        over_rest = await gateway.client.request(method, path, json=body, headers=headers)
        over_mcp = await gateway.mcp(tool, arguments, headers=headers)
        return over_rest, over_mcp

    @staticmethod
    def reason(response: httpx.Response) -> str:
        reply = response.json()
        if "error" in reply:
            return str(reply["error"]["data"]["reason_code"])
        if "result" in reply:
            return "OK"
        return str(reply.get("reason_code", "OK"))

    @pytest.mark.parametrize(
        ("label", "mandate_over", "rest", "tool", "arguments"),
        [
            (
                "allowed read",
                {},
                ("GET", "/proxy/invoices/inv_001", None),
                "get_invoice",
                {"invoice_id": "inv_001"},
            ),
            (
                "allowed payment",
                {},
                ("POST", "/proxy/payments", a_payment("120.0000")),
                "initiate_payment",
                a_payment("120.0000"),
            ),
            (
                "scope refusal",
                {"scopes": frozenset({"invoice:read"})},
                ("POST", "/proxy/payments", a_payment("120.0000")),
                "initiate_payment",
                a_payment("120.0000"),
            ),
            (
                "policy refusal",
                {},
                ("POST", "/proxy/payments", a_payment("150000.0000")),
                "initiate_payment",
                a_payment("150000.0000"),
            ),
            (
                "malformed amount",
                {},
                ("POST", "/proxy/payments", a_payment("0.00001")),
                "initiate_payment",
                a_payment("0.00001"),
            ),
        ],
    )
    async def test_rest_and_mcp_agree(
        self,
        gateway: Gateway,
        label: str,
        mandate_over: dict[str, object],
        rest: tuple[str, str, dict[str, Any] | None],
        tool: str,
        arguments: dict[str, Any],
    ) -> None:
        headers = dict(bearer(a_mandate(**mandate_over)))
        over_rest, over_mcp = await self.both(
            gateway, rest=rest, tool=tool, arguments=arguments, headers=headers
        )
        assert over_mcp.status_code == over_rest.status_code, label
        assert self.reason(over_mcp) == self.reason(over_rest), label

    async def test_the_ladder_is_not_vacuous(self, gateway: Gateway) -> None:
        """Parity of two answers that are both "OK" would prove nothing about refusals."""
        refused = await gateway.mcp(
            "initiate_payment",
            a_payment("120.0000"),
            headers=dict(bearer(a_mandate(scopes=frozenset({"invoice:read"})))),
        )
        assert refused.status_code == 403
        assert self.reason(refused) != "OK"


class TestTheShippedTable:
    """`scripts.serve_pep.ROUTES` is what the demo PEP loads, via the bootstrap copy."""

    @staticmethod
    def mcp_rules() -> dict[str, dict[str, Any]]:
        return {r["mcp_tool"]: r for r in ROUTES["routes"] if "mcp_tool" in r}

    def test_every_tool_the_stub_lists_is_mapped(self) -> None:
        assert set(self.mcp_rules()) == {tool["name"] for tool in MCP_TOOLS}

    def test_each_mcp_rule_decides_as_its_rest_twin_does(self) -> None:
        """Same scope, same policy tool id: the token and the bundle cannot tell them apart."""
        rest = {(r["scope"], r["tool"]) for r in ROUTES["routes"] if "mcp_tool" not in r}
        for name, rule in self.mcp_rules().items():
            assert (rule["scope"], rule["tool"]) in rest, name
            assert rule["drift_mode"] == "off", name

    def test_each_rest_argument_has_an_mcp_twin(self) -> None:
        by_tool: dict[str, set[str]] = {}
        for rule in ROUTES["routes"]:
            if "mcp_tool" not in rule:
                by_tool.setdefault(rule["tool"], set()).update(rule["args"])
        for name, rule in self.mcp_rules().items():
            assert set(rule["args"]) == by_tool[rule["tool"]], name

    def test_it_loads(self) -> None:
        assert RouteTable.from_config(ROUTES).is_mcp("POST", "/mcp")
