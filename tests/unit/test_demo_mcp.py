"""`scripts/demo_mcp.py` — APICTA-06.

`make demo-mcp` sends `seed_demo`'s refusal ladder again over MCP `tools/call` and checks every
rung against the reason code the REST ladder produces (`MASTER.md` §7). What these tests
protect is that it still checks something: that every rung has an expectation, that the
expectations cover the same refusal layers as the REST scenario, and that a wrong answer turns
into a non-zero exit rather than a line nobody reads.

The stack half runs for real in the `demo-stack` CI job, against the built image.
"""

from __future__ import annotations

import json

import httpx

from agentiam_demo.tools import MCP_TOOLS
from scripts import demo_mcp, seed_demo


def minted() -> set[str]:
    agents = {"root"} | {a for a, *_ in seed_demo._CHILDREN}
    return agents | {a for _p, a, *_ in seed_demo._DESCENDANTS}


class TestTheLadder:
    def test_it_uses_only_agents_the_seed_mints(self) -> None:
        used = {rung.who for rung in demo_mcp.LADDER if rung.who is not None}
        assert used <= minted(), used - minted()

    def test_it_reaches_every_refusal_layer_the_rest_scenario_does(self) -> None:
        reasons = {rung.expect for rung in demo_mcp.LADDER}
        assert {
            "OK",
            "SCOPE_ATTENUATED_AWAY",
            "BUDGET_EXHAUSTED_CAVEAT",
            "BUDGET_EXHAUSTED_MANDATE",
            "POLICY_DENIED",
            "MALFORMED_REQUEST",
        } <= reasons

    def test_it_allows_something(self) -> None:
        """A ladder of refusals alone could be a PEP that refuses MCP outright."""
        allowed = [rung for rung in demo_mcp.LADDER if rung.expect == "OK"]
        assert {rung.tool for rung in allowed} >= {"get_invoice", "initiate_payment"}

    def test_it_tries_the_smuggled_name(self) -> None:
        """TM-26 over MCP: the one refusal REST cannot show."""
        smuggled = [rung for rung in demo_mcp.LADDER if rung.raw is not None]
        assert smuggled
        assert all(b'"name"' in rung.body() for rung in smuggled)
        assert all(rung.expect == "MALFORMED_REQUEST" for rung in smuggled)

    def test_every_named_tool_is_one_the_stub_serves_or_a_deliberate_miss(self) -> None:
        served = {tool["name"] for tool in MCP_TOOLS}
        for rung in demo_mcp.LADDER:
            if rung.tool not in served:
                assert rung.expect == "MALFORMED_REQUEST", rung.label

    def test_payments_fit_inside_one_lease(self) -> None:
        """The same rule as the REST traffic: bigger than the lease is LEASE_UNAVAILABLE."""
        from scripts.pep_service import DEFAULT_LEASE_SIZE

        for rung in demo_mcp.LADDER:
            if rung.tool == "initiate_payment" and rung.expect == "OK":
                assert float(rung.arguments["amount"]) <= float(DEFAULT_LEASE_SIZE), rung.label

    def test_each_body_is_a_tools_call(self) -> None:
        for n, rung in enumerate(demo_mcp.LADDER):
            if rung.raw is None:
                body = json.loads(rung.body(request_id=n))
                assert body["method"] == "tools/call"
                assert body["id"] == n
                assert body["params"]["name"] == rung.tool


class TestJudging:
    @staticmethod
    def response(status: int, payload: object) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def test_a_result_is_ok(self) -> None:
        reply = {"jsonrpc": "2.0", "id": 1, "result": {"isError": False}}
        assert demo_mcp.reason_of(self.response(200, reply)) == "OK"

    def test_a_refusal_is_read_from_the_error_data(self) -> None:
        reply = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32001, "message": "no", "data": {"reason_code": "POLICY_DENIED"}},
        }
        assert demo_mcp.reason_of(self.response(403, reply)) == "POLICY_DENIED"

    def test_a_tool_failure_is_not_ok(self) -> None:
        """The PEP allowed it and the tool failed: not the outcome any rung expects."""
        reply = {"jsonrpc": "2.0", "id": 1, "result": {"isError": True}}
        assert demo_mcp.reason_of(self.response(200, reply)) == "TOOL_ERROR"

    def test_a_plain_body_is_reported_as_not_json_rpc(self) -> None:
        """A REST-shaped refusal on the MCP path means the envelope was never applied."""
        assert demo_mcp.reason_of(self.response(401, {"reason_code": "X"})) == "NOT_JSON_RPC"

    def test_a_non_json_body(self) -> None:
        assert demo_mcp.reason_of(httpx.Response(502, text="bad gateway")) == "NOT_JSON_RPC"


def rung_of(request: httpx.Request) -> demo_mcp.Rung:
    """Each rung is sent with its index as the JSON-RPC id."""
    index: int = json.loads(request.content)["id"]
    return demo_mcp.LADDER[index]


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})


TOKENS = {"tokens": dict.fromkeys(minted(), "t"), "task_id": "x"}


class TestExitCode:
    def test_a_wrong_answer_fails_the_run(self) -> None:
        assert demo_mcp.run(TOKENS, "http://pep", transport=httpx.MockTransport(ok)) == 1

    def test_every_right_answer_passes(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            rung = rung_of(request)
            if rung.expect == "OK":
                return ok(request)
            error = {"code": -32001, "message": "m", "data": {"reason_code": rung.expect}}
            return httpx.Response(403, json={"jsonrpc": "2.0", "id": 1, "error": error})

        assert demo_mcp.run(TOKENS, "http://pep", transport=httpx.MockTransport(answer)) == 0

    def test_a_rung_without_an_agent_sends_no_token(self) -> None:
        sent: dict[str | None, set[str | None]] = {}

        def answer(request: httpx.Request) -> httpx.Response:
            sent.setdefault(rung_of(request).who, set()).add(request.headers.get("authorization"))
            return ok(request)

        demo_mcp.run(TOKENS, "http://pep", transport=httpx.MockTransport(answer))
        assert sent[None] == {None}
        assert all(values == {"Bearer t"} for who, values in sent.items() if who is not None)

    def test_it_posts_json_to_the_proxied_mcp_path(self) -> None:
        seen: set[tuple[str, str, str]] = set()

        def answer(request: httpx.Request) -> httpx.Response:
            seen.add((request.method, request.url.path, request.headers["content-type"]))
            return ok(request)

        demo_mcp.run(TOKENS, "http://pep", transport=httpx.MockTransport(answer))
        assert seen == {("POST", "/proxy/mcp", "application/json")}


class TestCli:
    def test_the_defaults_match_demo_seed(self) -> None:
        args = demo_mcp.build_parser().parse_args(["--tokens", "/secrets/demo-tokens.json"])
        assert args.pep_url == seed_demo.build_parser().parse_args([]).pep_url
