"""MCP `tools/call` extraction — APICTA-06, spec 10 §3.1.

Every MCP call is `POST /mcp`, with the tool named in the JSON-RPC body rather than the path, so
a path table alone cannot tell `initiate_payment` from `get_invoice`. A rule's `mcp_tool` field
is what can.

Two classes carry the weight:

* `TestNoFallThrough` — a plain rule on the same path must never catch a `tools/call`. If it
  could, an unmapped tool would be authorized under whatever scope the plain rule grants, and
  the table would read as if it denied it.
* `TestEnvelopeAmbiguity` — TM-26 again, one level up. Measured, `json.loads` keeps the *last*
  of two `method` keys, so `{"method": "tools/list", "method": "tools/call"}` is a tool call to
  us. A parser that keeps the first sees a listing. The tool's identity is now part of what the
  two parsers must agree on, and the extractor refuses rather than choosing.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from agentiam_core.errors import ReasonCode
from agentiam_core.hashing import hash_object
from agentiam_pep.extractor import (
    ExtractionError,
    RouteTable,
    extract,
    mcp_request_id,
)

PAY: dict[str, Any] = {
    "method": "POST",
    "path": "/mcp",
    "mcp_tool": "initiate_payment",
    "scope": "payment:initiate",
    "tool": "payment_api",
    "args": {
        "payment.amount": "body.params.arguments.amount:number",
        "payment.to": "body.params.arguments.recipient.account_id",
    },
}
READ: dict[str, Any] = {
    "method": "POST",
    "path": "/mcp",
    "mcp_tool": "get_invoice",
    "scope": "invoice:read",
    "tool": "invoice_api",
    "args": {"invoice.id": "body.params.arguments.invoice_id"},
}
REST_PAY: dict[str, Any] = {
    "method": "POST",
    "path": "/payments",
    "scope": "payment:initiate",
    "tool": "payment_api",
    "args": {"payment.amount": "body.amount:number"},
}


def table(*rules: dict[str, Any], default: str = "deny") -> RouteTable:
    return RouteTable.from_config({"routes": list(rules), "default": {"action": default}})


def call(name: object, arguments: object = None, **envelope: object) -> bytes:
    """A `tools/call` body. `envelope` overrides or adds top-level members."""
    params: dict[str, object] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    body: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    body.update(envelope)
    return json.dumps(body).encode()


def mcp(routes: RouteTable, body: bytes | None, *, path: str = "/mcp") -> Any:
    return extract(routes, method="POST", path=path, body=body)


def refused(routes: RouteTable, body: bytes | None, *, path: str = "/mcp") -> ExtractionError:
    with pytest.raises(ExtractionError) as info:
        mcp(routes, body, path=path)
    assert info.value.reason is ReasonCode.MALFORMED_REQUEST
    return info.value


class TestMatching:
    def test_the_tool_name_chooses_the_rule(self) -> None:
        routes = table(READ, PAY)
        assert mcp(routes, call("get_invoice", {"invoice_id": "inv_001"})).scope == "invoice:read"
        assert mcp(routes, call("initiate_payment", {"amount": 1})).scope == "payment:initiate"

    def test_the_rules_own_tool_id_is_what_the_policy_sees(self) -> None:
        """`mcp_tool` is the wire name; `tool` stays the policy's resource id, as for REST."""
        extraction = mcp(table(PAY), call("initiate_payment", {"amount": 1}))
        assert extraction.tool == "payment_api"

    def test_arguments_are_read_through_the_existing_body_syntax(self) -> None:
        body = call(
            "initiate_payment", {"amount": "1250.5", "recipient": {"account_id": "acct_9001"}}
        )
        extraction = mcp(table(PAY), body)
        assert extraction.args == {"payment.amount": 12_505_000, "payment.to": "acct_9001"}
        assert extraction.arg_digest == hash_object(extraction.args)

    def test_an_absent_argument_is_absent_not_a_denial(self) -> None:
        """Spec 10 §2's third row holds over MCP exactly as over REST."""
        assert mcp(table(PAY), call("initiate_payment", {})).args == {}

    def test_a_number_finer_than_four_places_is_still_refused(self) -> None:
        refused(table(PAY), call("initiate_payment", {"amount": "0.00005"}))

    def test_an_unmapped_tool_is_refused(self) -> None:
        error = refused(table(READ, PAY), call("delete_everything"))
        assert "delete_everything" in error.detail

    def test_an_unmapped_tool_follows_the_default_like_any_unmapped_route(self) -> None:
        extraction = mcp(table(READ, default="allow_unmapped"), call("delete_everything"))
        assert extraction.scope == ""

    def test_the_name_is_compared_exactly(self) -> None:
        """No case folding or normalization: a name we would not recognise is unmapped."""
        for near_miss in ("Get_Invoice", "get_invoice ", "get-invoice"):
            refused(table(READ), call(near_miss))

    def test_a_rule_for_one_path_does_not_match_another(self) -> None:
        refused(table(READ), call("get_invoice"), path="/other-mcp")

    def test_a_parameterized_path_is_supported(self) -> None:
        rule = READ | {"path": "/mcp/{server}"}
        assert mcp(table(rule), call("get_invoice"), path="/mcp/finance").scope == "invoice:read"

    def test_a_notification_without_an_id_is_still_a_tool_call(self) -> None:
        """The upstream executes a `tools/call` notification; it just does not answer it."""
        body = json.loads(call("initiate_payment", {"amount": 1}))
        del body["id"]
        assert mcp(table(PAY), json.dumps(body).encode()).scope == "payment:initiate"

    def test_rest_routes_beside_mcp_ones_are_unaffected(self) -> None:
        routes = table(PAY, REST_PAY)
        extraction = extract(routes, method="POST", path="/payments", body=b'{"amount": 5}')
        assert extraction.args == {"payment.amount": 50_000}


class TestNoFallThrough:
    """A plain rule on an MCP path serves the session's other methods, never a tool call."""

    SESSION: ClassVar[dict[str, Any]] = {
        "method": "POST",
        "path": "/mcp",
        "scope": "mcp:session",
        "tool": "mcp_gateway",
    }

    def test_an_unmapped_tool_is_not_caught_by_a_plain_rule_on_the_same_path(self) -> None:
        """The bypass this whole class exists for: first-match-wins would otherwise pick it."""
        refused(table(self.SESSION, PAY), call("delete_everything"))

    def test_a_mapped_tool_is_matched_by_its_own_rule_even_after_a_plain_one(self) -> None:
        extraction = mcp(table(self.SESSION, PAY), call("initiate_payment", {"amount": 1}))
        assert extraction.scope == "payment:initiate"

    def test_a_catch_all_rule_does_not_catch_a_tool_call_either(self) -> None:
        catch_all = {"method": "ANY", "path": "/{rest:path}", "scope": "any", "tool": "any"}
        refused(table(catch_all, PAY), call("delete_everything"))

    def test_another_method_on_the_path_can_be_mapped_by_a_plain_rule(self) -> None:
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        extraction = mcp(table(self.SESSION, PAY), json.dumps(listing).encode())
        assert extraction.scope == "mcp:session"

    def test_another_method_with_no_plain_rule_is_unmapped(self) -> None:
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        refused(table(PAY), json.dumps(listing).encode())

    def test_a_get_on_the_path_is_not_an_mcp_request(self) -> None:
        """MCP's GET opens a server-to-client stream; there is no body and no tool."""
        rule = self.SESSION | {"method": "GET"}
        extraction = extract(table(PAY, rule), method="GET", path="/mcp")
        assert extraction.scope == "mcp:session"


class TestEnvelopeAmbiguity:
    """TM-26 applied to the envelope: what tool runs must not depend on whose parser reads it."""

    @staticmethod
    def raw(text: str) -> bytes:
        return text.encode()

    def test_two_method_keys_are_refused_in_either_order(self) -> None:
        routes = table(PAY, TestNoFallThrough.SESSION)
        for first, second in (("tools/list", "tools/call"), ("tools/call", "tools/list")):
            refused(
                routes,
                self.raw(
                    f'{{"jsonrpc":"2.0","id":1,"method":"{first}","method":"{second}",'
                    f'"params":{{"name":"initiate_payment"}}}}'
                ),
            )

    def test_two_names_are_refused(self) -> None:
        """Read as `initiate_payment` here, and as `get_invoice` by a first-wins upstream."""
        error = refused(
            table(READ, PAY),
            self.raw(
                '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                '"params":{"name":"get_invoice","name":"initiate_payment"}}'
            ),
        )
        assert "name" in error.detail

    def test_two_params_objects_are_refused(self) -> None:
        refused(
            table(READ, PAY),
            self.raw(
                '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                '"params":{"name":"get_invoice"},"params":{"name":"initiate_payment"}}'
            ),
        )

    def test_two_jsonrpc_members_are_refused(self) -> None:
        refused(
            table(READ),
            self.raw(
                '{"jsonrpc":"1.0","jsonrpc":"2.0","id":1,"method":"tools/call",'
                '"params":{"name":"get_invoice"}}'
            ),
        )

    def test_a_duplicated_argument_is_refused_as_it_is_over_rest(self) -> None:
        refused(
            table(PAY),
            self.raw(
                '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
                '{"name":"initiate_payment","arguments":{"amount":1,"amount":999999}}}'
            ),
        )

    def test_a_batch_is_refused(self) -> None:
        """Removed from MCP in 2025-06-18; an older upstream would still run every member."""
        batch = [json.loads(call("get_invoice")), json.loads(call("initiate_payment"))]
        assert "batch" in refused(table(READ, PAY), json.dumps(batch).encode()).detail

    @pytest.mark.parametrize(
        "body",
        [None, b"", b"not json", b"\xff\xfe", b'"tools/call"', b"42", b"null"],
        ids=["absent", "empty", "text", "not-utf8", "string", "number", "null"],
    )
    def test_a_body_that_is_not_a_json_rpc_object_is_refused(self, body: bytes | None) -> None:
        refused(table(READ, PAY), body)

    def test_a_tool_call_must_declare_json_rpc_2(self) -> None:
        refused(table(READ), call("get_invoice", jsonrpc="1.0"))
        body = json.loads(call("get_invoice"))
        del body["jsonrpc"]
        refused(table(READ), json.dumps(body).encode())

    @pytest.mark.parametrize("name", [None, 7, ["get_invoice"], {"n": "get_invoice"}])
    def test_a_name_that_is_not_a_string_is_refused(self, name: object) -> None:
        refused(table(READ), call(name))

    def test_params_that_are_not_an_object_are_refused(self) -> None:
        refused(table(READ), call("get_invoice", params=["get_invoice"]))

    def test_a_method_that_is_not_a_string_is_refused(self) -> None:
        refused(table(READ), call("get_invoice", method=["tools/call"]))

    def test_an_oversized_body_is_refused_before_it_is_parsed(self) -> None:
        routes = RouteTable.from_config({"routes": [READ], "max_extract_body_bytes": 64})
        refused(routes, call("get_invoice", {"invoice_id": "x" * 100}))


class TestLoading:
    def test_an_mcp_rule_must_be_a_post(self) -> None:
        with pytest.raises(ValueError, match="mcp_tool"):
            table(READ | {"method": "GET"})

    def test_two_rules_naming_one_tool_on_one_path_are_a_load_error(self) -> None:
        """The second could never match; a reviewer reading it would think it does."""
        with pytest.raises(ValueError, match="get_invoice"):
            table(READ, READ | {"scope": "invoice:write"})

    def test_one_tool_on_two_paths_is_fine(self) -> None:
        table(READ, READ | {"path": "/mcp/v2"})

    @pytest.mark.parametrize("value", ["", 7, None])
    def test_mcp_tool_must_be_a_non_empty_string(self, value: object) -> None:
        with pytest.raises(ValueError, match="mcp_tool"):
            table(READ | {"mcp_tool": value})

    def test_an_mcp_rule_always_reads_the_body(self) -> None:
        """Even with no `body.` argument: the tool's name is in the body."""
        rule = READ | {"args": {}}
        assert table(rule).rules[0].needs_body

    def test_the_mapping_version_sees_the_tool_name(self) -> None:
        """Repointing a rule at another tool changes what every token constrains (spec 10 §8)."""
        assert table(READ).mapping_version != table(READ | {"mcp_tool": "pay"}).mapping_version

    def test_a_table_with_no_mcp_rule_keeps_its_old_mapping_version(self) -> None:
        """Adding the field must not silently re-version every deployed REST table."""
        expected = hash_object(
            {
                "default": "deny",
                "max_extract_body_bytes": 1 << 20,
                "routes": [
                    {
                        "method": "POST",
                        "path": "/payments",
                        "scope": "payment:initiate",
                        "tool": "payment_api",
                        "args": {"payment.amount": "body.amount:number"},
                    }
                ],
            }
        )
        assert table(REST_PAY).mapping_version == expected


class TestIsMcp:
    def test_a_post_to_a_path_an_mcp_rule_covers(self) -> None:
        routes = table(READ, REST_PAY)
        assert routes.is_mcp("POST", "/mcp")
        assert routes.is_mcp("post", "/mcp")

    def test_not_another_method_or_another_path(self) -> None:
        routes = table(READ, REST_PAY)
        assert not routes.is_mcp("GET", "/mcp")
        assert not routes.is_mcp("POST", "/payments")

    def test_a_table_with_no_mcp_rule_has_no_mcp_path(self) -> None:
        assert not table(REST_PAY).is_mcp("POST", "/mcp")


class TestRequestId:
    """What the refusal envelope echoes. Never a reason to refuse; at worst `null`."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (b'{"id": 7}', 7),
            (b'{"id": "req-1"}', "req-1"),
            (b'{"id": null}', None),
            (b"{}", None),
            (b'{"id": true}', None),
            (b'{"id": 1.5}', None),
            (b'{"id": {"a": 1}}', None),
            (b"[1, 2]", None),
            (b"not json", None),
            (b"", None),
            (None, None),
        ],
    )
    def test_it_echoes_a_string_or_integer_id_and_nothing_else(
        self, body: bytes | None, expected: object
    ) -> None:
        assert mcp_request_id(body) == expected

    def test_a_large_integer_survives_exactly(self) -> None:
        assert mcp_request_id(b'{"id": 12345678901234567890}') == 12345678901234567890

    def test_decimal_never_leaks_out(self) -> None:
        assert not isinstance(mcp_request_id(b'{"id": 7}'), Decimal)
