"""HTTP scope and argument extraction — spec 10, T-020.

Step 1 of the pipeline, and the only step that reads an untrusted wire format. Everything
after it reasons about a `RequestContext`; these tests are about how an HTTP request
becomes one.

The route-pattern table below is the acceptance criterion (`PLAN.md` §9 T-020 asks for ≥15
patterns including path and query params). The parts worth reading are the scaling rules in
`TestNumericScaling` and the normalization in `TestNormalization` — both settle cases where
our view of the request could differ from the upstream's. The ambiguity half of that lives
in `tests/security/test_parameter_pollution.py`, because it is TM-26.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from agentiam_core.errors import ReasonCode
from agentiam_core.hashing import hash_object
from agentiam_pep.extractor import Extraction, ExtractionError, RouteTable, extract

# --------------------------------------------------------------------------------------
# A route table covering the demo surface. Sixteen rules, exercising every source form.
# --------------------------------------------------------------------------------------

CONFIG: dict[str, object] = {
    "routes": [
        {
            "method": "GET",
            "path": "/invoices",
            "scope": "invoice:read",
            "tool": "invoice_api",
            "args": {"invoice.limit": "query.limit:number"},
        },
        {
            "method": "GET",
            "path": "/invoices/{id}",
            "scope": "invoice:read",
            "tool": "invoice_api",
            "args": {"invoice.id": "path.id"},
        },
        {
            "method": "POST",
            "path": "/invoices",
            "scope": "invoice:write",
            "tool": "invoice_api",
            "args": {"invoice.total": "body.total:number"},
        },
        {
            "method": "PATCH",
            "path": "/invoices/{id}",
            "scope": "invoice:write",
            "tool": "invoice_api",
            "args": {"invoice.id": "path.id"},
        },
        {
            "method": "DELETE",
            "path": "/invoices/{id}",
            "scope": "invoice:delete",
            "tool": "invoice_api",
            "args": {"invoice.id": "path.id"},
        },
        {
            "method": "GET",
            "path": "/vendors",
            "scope": "vendor:read",
            "tool": "vendor_api",
            "args": {},
        },
        {
            "method": "GET",
            "path": "/vendors/{vid}",
            "scope": "vendor:read",
            "tool": "vendor_api",
            "args": {"vendor.id": "path.vid"},
        },
        {
            "method": "PUT",
            "path": "/vendors/{vid}",
            "scope": "vendor:write",
            "tool": "vendor_api",
            "args": {"vendor.id": "path.vid"},
        },
        {
            "method": "GET",
            "path": "/vendors/{vid}/invoices/{iid}",
            "scope": "invoice:read",
            "tool": "invoice_api",
            "args": {"vendor.id": "path.vid", "invoice.id": "path.iid"},
        },
        {
            "method": "POST",
            "path": "/payments",
            "scope": "payment:initiate",
            "tool": "payment_api",
            "args": {
                "payment.amount": "body.amount:number",
                "payment.to": "body.recipient.account_id",
            },
        },
        {
            "method": "GET",
            "path": "/payments/{pid}",
            "scope": "payment:read",
            "tool": "payment_api",
            "args": {"payment.id": "path.pid"},
        },
        {
            "method": "POST",
            "path": "/payments/{pid}/cancel",
            "scope": "payment:cancel",
            "tool": "payment_api",
            "args": {"payment.id": "path.pid"},
        },
        {
            "method": "POST",
            "path": "/email/send",
            "scope": "email:send",
            "tool": "email_api",
            "args": {"email.domain": "body.to", "email.request_id": "header.x-request-id"},
        },
        {
            "method": "GET",
            "path": "/search",
            "scope": "search:read",
            "tool": "search_api",
            "args": {"search.q": "query.q", "search.limit": "query.limit:number"},
        },
        {
            "method": "ANY",
            "path": "/status",
            "scope": "status:read",
            "tool": "status_api",
            "args": {},
        },
        {
            "method": "GET",
            "path": "/reports/{path:path}",
            "scope": "report:read",
            "tool": "report_api",
            "args": {"report.path": "path.path"},
        },
    ],
    "default": {"action": "deny"},
}


@pytest.fixture(scope="module")
def table() -> RouteTable:
    return RouteTable.from_config(CONFIG)


def call(
    table: RouteTable,
    method: str,
    path: str,
    *,
    query: str = "",
    headers: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
) -> Extraction:
    return extract(
        table,
        method=method,
        path=path,
        query_string=query,
        headers=headers or [],
        body=body,
    )


class TestRouteMatching:
    """The ≥15 patterns of the acceptance criterion, plus the ordering rule."""

    @pytest.mark.parametrize(
        ("method", "path", "scope", "tool"),
        [
            ("GET", "/invoices", "invoice:read", "invoice_api"),
            ("GET", "/invoices/inv_42", "invoice:read", "invoice_api"),
            ("POST", "/invoices", "invoice:write", "invoice_api"),
            ("PATCH", "/invoices/inv_42", "invoice:write", "invoice_api"),
            ("DELETE", "/invoices/inv_42", "invoice:delete", "invoice_api"),
            ("GET", "/vendors", "vendor:read", "vendor_api"),
            ("GET", "/vendors/v_7", "vendor:read", "vendor_api"),
            ("PUT", "/vendors/v_7", "vendor:write", "vendor_api"),
            ("GET", "/vendors/v_7/invoices/inv_42", "invoice:read", "invoice_api"),
            ("POST", "/payments", "payment:initiate", "payment_api"),
            ("GET", "/payments/p_1", "payment:read", "payment_api"),
            ("POST", "/payments/p_1/cancel", "payment:cancel", "payment_api"),
            ("POST", "/email/send", "email:send", "email_api"),
            ("GET", "/search", "search:read", "search_api"),
            ("GET", "/status", "status:read", "status_api"),
            ("POST", "/status", "status:read", "status_api"),
            ("GET", "/reports/2026/q1/summary.csv", "report:read", "report_api"),
        ],
    )
    def test_pattern_maps_to_its_scope(
        self, table: RouteTable, method: str, path: str, scope: str, tool: str
    ) -> None:
        body = b'{"amount": "1", "recipient": {"account_id": "a"}, "to": "x@y.com", "total": "1"}'
        result = call(table, method, path, body=body, headers=[("x-request-id", "r1")])
        assert result.scope == scope
        assert result.tool == tool

    def test_a_path_parameter_does_not_span_a_slash(self, table: RouteTable) -> None:
        """`{id}` compiles to `[^/]+`, so this is unmapped rather than a match with id='a/b'."""
        with pytest.raises(ExtractionError) as caught:
            call(table, "GET", "/invoices/a/b")
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_the_path_converter_does_span_slashes(self, table: RouteTable) -> None:
        result = call(table, "GET", "/reports/2026/q1/summary.csv")
        assert result.args["report.path"] == "2026/q1/summary.csv"

    def test_method_is_part_of_the_match(self, table: RouteTable) -> None:
        """Same path, different method, different scope — the mapping is (method, path)."""
        assert call(table, "GET", "/vendors/v_7").scope == "vendor:read"
        assert call(table, "PUT", "/vendors/v_7").scope == "vendor:write"

    def test_any_matches_every_method(self, table: RouteTable) -> None:
        for method in ("GET", "POST", "DELETE", "OPTIONS"):
            assert call(table, method, "/status").scope == "status:read"

    def test_first_matching_rule_wins(self) -> None:
        """Order is explicit so a specific rule can precede a general one (spec 10 §3)."""
        config = {
            "routes": [
                {
                    "method": "GET",
                    "path": "/invoices/draft",
                    "scope": "invoice:draft",
                    "tool": "invoice_api",
                    "args": {},
                },
                {
                    "method": "GET",
                    "path": "/invoices/{id}",
                    "scope": "invoice:read",
                    "tool": "invoice_api",
                    "args": {"invoice.id": "path.id"},
                },
            ],
            "default": {"action": "deny"},
        }
        ordered = RouteTable.from_config(config)
        assert call(ordered, "GET", "/invoices/draft").scope == "invoice:draft"
        assert call(ordered, "GET", "/invoices/other").scope == "invoice:read"


class TestUnmappedRoutes:
    def test_unmapped_denies_by_default(self, table: RouteTable) -> None:
        """An unmapped route is an unreviewed route (spec 10 §2)."""
        with pytest.raises(ExtractionError) as caught:
            call(table, "GET", "/admin/keys")
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_unmapped_method_on_a_mapped_path_denies(self, table: RouteTable) -> None:
        with pytest.raises(ExtractionError):
            call(table, "DELETE", "/vendors/v_7")

    def test_allow_unmapped_is_configurable(self) -> None:
        config = dict(CONFIG) | {"default": {"action": "allow_unmapped"}}
        permissive = RouteTable.from_config(config)
        result = call(permissive, "GET", "/admin/keys")
        assert result.scope == ""
        assert result.args == {}


class TestArgumentSources:
    def test_path_parameter(self, table: RouteTable) -> None:
        assert call(table, "GET", "/invoices/inv_42").args["invoice.id"] == "inv_42"

    def test_two_path_parameters(self, table: RouteTable) -> None:
        args = call(table, "GET", "/vendors/v_7/invoices/inv_42").args
        assert args == {"vendor.id": "v_7", "invoice.id": "inv_42"}

    def test_query_parameter(self, table: RouteTable) -> None:
        args = call(table, "GET", "/search", query="q=boots&limit=10").args
        assert args["search.q"] == "boots"
        assert args["search.limit"] == 100_000  # 10 scaled by 10^4

    def test_nested_body_path(self, table: RouteTable) -> None:
        body = json.dumps({"amount": "25.5", "recipient": {"account_id": "acct_9"}}).encode()
        args = call(table, "POST", "/payments", body=body).args
        assert args["payment.to"] == "acct_9"

    def test_header_source(self, table: RouteTable) -> None:
        body = json.dumps({"to": "finance@example.com"}).encode()
        args = call(
            table, "POST", "/email/send", body=body, headers=[("X-Request-Id", "req-7")]
        ).args
        assert args["email.request_id"] == "req-7"

    def test_an_absent_argument_is_omitted_not_denied(self, table: RouteTable) -> None:
        """Spec 10 §2: `arg` facts are optional, so `ArgPredicate` stays vacuous (spec 02 §3.2)."""
        result = call(table, "GET", "/search", query="q=boots")
        assert "search.q" in result.args
        assert "search.limit" not in result.args

    def test_an_object_valued_source_is_a_mapping_error(self) -> None:
        """`args` is scalar-valued; a path resolving to an object cannot be represented."""
        config = {
            "routes": [
                {
                    "method": "POST",
                    "path": "/payments",
                    "scope": "payment:initiate",
                    "tool": "payment_api",
                    "args": {"payment.recipient": "body.recipient"},
                }
            ],
            "default": {"action": "deny"},
        }
        bad = RouteTable.from_config(config)
        body = json.dumps({"recipient": {"account_id": "a"}}).encode()
        with pytest.raises(ExtractionError) as caught:
            call(bad, "POST", "/payments", body=body)
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_a_dotted_key_is_unreachable_and_fails_open(self) -> None:
        """Spec 10 §9 limitation 1, stated so it is not discovered later."""
        config = {
            "routes": [
                {
                    "method": "POST",
                    "path": "/payments",
                    "scope": "payment:initiate",
                    "tool": "payment_api",
                    "args": {"payment.amount": "body.a.b:number"},
                }
            ],
            "default": {"action": "deny"},
        }
        odd = RouteTable.from_config(config)
        result = call(odd, "POST", "/payments", body=json.dumps({"a.b": "5"}).encode())
        assert "payment.amount" not in result.args


class TestNumericScaling:
    """Spec 10 §4.3 — one comparison rule for every numeric term (spec 02 §4.6)."""

    @pytest.mark.parametrize(
        ("raw", "scaled"),
        [("1", 10_000), ("25.5", 255_000), ("0.0001", 1), ("1e3", 10_000_000), ("0", 0)],
    )
    def test_numbers_scale_by_ten_thousand(self, table: RouteTable, raw: str, scaled: int) -> None:
        body = json.dumps({"total": Decimal(raw)}, default=str).encode()
        assert call(table, "POST", "/invoices", body=body).args["invoice.total"] == scaled

    @pytest.mark.parametrize("raw", ["0.00005", "1.23456"])
    def test_more_than_four_decimal_places_is_refused(self, table: RouteTable, raw: str) -> None:
        """Rounding would enforce a value the caller did not request (spec 10 §4.3)."""
        body = ('{"total": ' + raw + "}").encode()
        with pytest.raises(ExtractionError) as caught:
            call(table, "POST", "/invoices", body=body)
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_numbers_are_refused(self, table: RouteTable, raw: str) -> None:
        """Every comparison against NaN is false, so a `reject if` predicate never fires."""
        body = ('{"total": ' + raw + "}").encode()
        with pytest.raises(ExtractionError) as caught:
            call(table, "POST", "/invoices", body=body)
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_numbers_never_pass_through_float(self, table: RouteTable) -> None:
        """Rule 6: money never touches a float. 0.1+0.2 is the canonical demonstration."""
        body = b'{"total": 0.3}'
        assert call(table, "POST", "/invoices", body=body).args["invoice.total"] == 3_000

    def test_a_non_numeric_string_stays_a_string(self, table: RouteTable) -> None:
        assert isinstance(call(table, "GET", "/invoices/inv_42").args["invoice.id"], str)


class TestNormalization:
    """Spec 10 §5.3 — make our view match what the upstream will act on."""

    def test_path_parameters_are_percent_decoded(self, table: RouteTable) -> None:
        """`compile_path` leaves `a%2Fb` encoded; the upstream will read `a/b`."""
        assert call(table, "GET", "/invoices/a%2Fb").args["invoice.id"] == "a/b"

    def test_query_parameters_are_percent_decoded(self, table: RouteTable) -> None:
        assert call(table, "GET", "/search", query="q=a%20b").args["search.q"] == "a b"

    def test_decoding_happens_once(self, table: RouteTable) -> None:
        """Repeated decoding is its own smuggling primitive; one pass is what upstreams do."""
        assert call(table, "GET", "/invoices/a%252Fb").args["invoice.id"] == "a%2Fb"

    def test_strings_are_nfc_normalized(self, table: RouteTable) -> None:
        """Two visually identical arguments must not produce two different digests."""
        composed = call(table, "GET", "/search", query="q=é")
        decomposed = call(table, "GET", "/search", query="q=é")
        assert composed.args["search.q"] == decomposed.args["search.q"]
        assert composed.arg_digest == decomposed.arg_digest


class TestArgDigest:
    def test_digest_is_the_canonical_hash_of_the_extracted_args(self, table: RouteTable) -> None:
        result = call(table, "GET", "/invoices/inv_42")
        assert result.arg_digest == hash_object(result.args)

    def test_digest_ignores_unmapped_body_fields(self, table: RouteTable) -> None:
        """Spec 10 §7: stable against anything the mapping does not read."""
        lean = json.dumps({"amount": "1", "recipient": {"account_id": "a"}}).encode()
        fat = json.dumps(
            {"amount": "1", "recipient": {"account_id": "a"}, "memo": "unmapped", "x": [1, 2]}
        ).encode()
        assert (
            call(table, "POST", "/payments", body=lean).arg_digest
            == call(table, "POST", "/payments", body=fat).arg_digest
        )

    def test_digest_changes_when_a_mapped_value_changes(self, table: RouteTable) -> None:
        a = call(table, "GET", "/invoices/inv_1").arg_digest
        b = call(table, "GET", "/invoices/inv_2").arg_digest
        assert a != b


class TestBodyHandling:
    """Spec 10 §6 — reading the body is bounded, and not reading it is not a denial."""

    def test_a_body_over_the_cap_is_refused(self) -> None:
        config = dict(CONFIG) | {"max_extract_body_bytes": 64}
        capped = RouteTable.from_config(config)
        body = json.dumps({"total": "1", "pad": "x" * 200}).encode()
        with pytest.raises(ExtractionError) as caught:
            call(capped, "POST", "/invoices", body=body)
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_the_cap_does_not_apply_to_routes_without_a_body_mapping(self) -> None:
        """Otherwise every large upload through an unconstrained route would be denied."""
        config = dict(CONFIG) | {"max_extract_body_bytes": 64}
        capped = RouteTable.from_config(config)
        assert call(capped, "GET", "/vendors", body=b"x" * 5000).scope == "vendor:read"

    def test_a_non_json_body_yields_no_args_and_no_denial(self, table: RouteTable) -> None:
        result = call(table, "POST", "/invoices", body=b"not json at all")
        assert result.scope == "invoice:write"
        assert "invoice.total" not in result.args

    def test_a_missing_body_yields_no_args_and_no_denial(self, table: RouteTable) -> None:
        result = call(table, "POST", "/invoices", body=None)
        assert "invoice.total" not in result.args

    def test_a_json_array_body_yields_no_args(self, table: RouteTable) -> None:
        """`body.` paths address object keys; a top-level array has none."""
        result = call(table, "POST", "/invoices", body=b'[{"total": "1"}]')
        assert "invoice.total" not in result.args


class TestMappingVersion:
    """Spec 10 §8 — a mapping change alters what an existing caveat constrains."""

    def test_version_is_stable_for_the_same_config(self) -> None:
        assert RouteTable.from_config(CONFIG).mapping_version == (
            RouteTable.from_config(CONFIG).mapping_version
        )

    def test_version_changes_when_a_source_is_repointed(self) -> None:
        moved = json.loads(json.dumps(CONFIG))
        moved["routes"][2]["args"] = {"invoice.total": "body.grand_total"}
        assert (
            RouteTable.from_config(moved).mapping_version
            != RouteTable.from_config(CONFIG).mapping_version
        )


class TestConfigValidation:
    """A mapping error should fail at load, not at the first request that touches it."""

    @pytest.mark.parametrize(
        "rule",
        [
            {"method": "GET", "path": "/x", "scope": "a:b", "tool": "t", "args": {"k": "nope.y"}},
            {"method": "GET", "path": "/x", "scope": "a:b", "tool": "t", "args": {"k": "path.z"}},
            {"method": "GET", "path": "/x", "scope": "", "tool": "t", "args": {}},
        ],
        ids=["unknown-source-kind", "path-param-not-in-pattern", "empty-scope"],
    )
    def test_a_bad_rule_is_refused_at_load(self, rule: dict[str, object]) -> None:
        with pytest.raises(ValueError, match=r".+"):
            RouteTable.from_config({"routes": [rule], "default": {"action": "deny"}})


class TestDeclaredTypes:
    """Spec 10 §4.1 — the type is declared, never inferred from the text."""

    def test_a_plain_source_keeps_a_numeric_looking_string(self, table: RouteTable) -> None:
        """An account id of `0012` must not extract as 12, or a string caveat stops matching."""
        assert call(table, "GET", "/invoices/0012").args["invoice.id"] == "0012"

    def test_a_number_source_scales(self, table: RouteTable) -> None:
        assert call(table, "GET", "/search", query="limit=12").args["search.limit"] == 120_000

    def test_a_number_source_that_will_not_parse_is_refused(self, table: RouteTable) -> None:
        """Falling back to string would silently change what the caveat compares."""
        with pytest.raises(ExtractionError) as caught:
            call(table, "GET", "/search", query="limit=lots")
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_a_json_number_read_as_text_becomes_its_exact_decimal(self) -> None:
        config = {
            "routes": [
                {
                    "method": "POST",
                    "path": "/invoices",
                    "scope": "invoice:write",
                    "tool": "invoice_api",
                    "args": {"invoice.total": "body.total"},
                }
            ],
            "default": {"action": "deny"},
        }
        textual = RouteTable.from_config(config)
        result = call(textual, "POST", "/invoices", body=b'{"total": 0.3}')
        assert result.args["invoice.total"] == "0.3"

    def test_a_json_string_read_as_a_number_scales(self, table: RouteTable) -> None:
        """Clients differ on whether an amount is quoted; the mapping decides what it means."""
        body = b'{"amount": "25.5", "recipient": {"account_id": "a"}}'
        assert call(table, "POST", "/payments", body=body).args["payment.amount"] == 255_000


class TestFileLoading:
    """`from_file` is the config-driven half of the acceptance criterion."""

    def test_a_valid_file_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "routes.json"
        path.write_text(json.dumps(CONFIG), encoding="utf-8")
        loaded = RouteTable.from_file(path)
        assert loaded.mapping_version == RouteTable.from_config(CONFIG).mapping_version

    def test_invalid_json_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "routes.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            RouteTable.from_file(path)

    def test_a_top_level_array_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "routes.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON object"):
            RouteTable.from_file(path)

    def test_a_bad_rule_still_fails_at_load(self, tmp_path: Path) -> None:
        """Validation is not skipped just because the config arrived from disk."""
        path = tmp_path / "routes.json"
        path.write_text(
            json.dumps(
                {
                    "routes": [
                        {
                            "method": "GET",
                            "path": "/x",
                            "scope": "a:b",
                            "tool": "t",
                            "args": {"k": "nope.y"},
                        }
                    ],
                    "default": {"action": "deny"},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unknown source kind"):
            RouteTable.from_file(path)


class TestConfigValidationDetail:
    """Every guard in `from_config`, fired once.

    A validation branch that no test reaches is a branch nobody has read carefully — and
    these are the ones that turn a typo in a route file into a clear message rather than a
    surprise at the first request.
    """

    @staticmethod
    def _table(**over: object) -> RouteTable:
        rule = {"method": "GET", "path": "/x", "scope": "a:b", "tool": "t", "args": {}} | over
        return RouteTable.from_config({"routes": [rule], "default": {"action": "deny"}})

    @pytest.mark.parametrize(
        ("expression", "message"),
        [
            ("query.", "names no field"),
            ("body.a..b", "empty segment"),
            ("query.a.b", "single parameter"),
        ],
    )
    def test_source_expression_errors(self, expression: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            self._table(args={"k": expression})

    def test_routes_must_be_a_list(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            RouteTable.from_config({"routes": {"not": "a list"}})

    def test_default_action_must_be_known(self) -> None:
        with pytest.raises(ValueError, match=r"deny.*allow_unmapped"):
            RouteTable.from_config({"routes": [], "default": {"action": "maybe"}})

    @pytest.mark.parametrize("value", [0, -1, "big", True])
    def test_body_cap_must_be_a_positive_integer(self, value: object) -> None:
        with pytest.raises(ValueError, match="positive integer"):
            RouteTable.from_config({"routes": [], "max_extract_body_bytes": value})

    def test_a_rule_must_be_a_mapping(self) -> None:
        with pytest.raises(ValueError, match="must be a mapping"):
            RouteTable.from_config({"routes": ["not a rule"]})

    def test_path_must_be_absolute(self) -> None:
        with pytest.raises(ValueError, match="must start with"):
            self._table(path="relative")

    def test_args_must_be_a_mapping(self) -> None:
        with pytest.raises(ValueError, match=r"args must be a mapping"):
            self._table(args=["not", "a", "mapping"])

    def test_an_arg_label_must_be_a_string(self) -> None:
        with pytest.raises(ValueError, match="non-string label"):
            self._table(args={7: "path.id"})

    def test_an_arg_expression_must_be_a_string(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            self._table(args={"k": 7})

    @pytest.mark.parametrize("field", ["method", "path", "scope", "tool"])
    def test_required_fields_must_be_non_empty_strings(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
            self._table(**{field: ""})


class TestScalarEdges:
    """The remaining value-conversion branches."""

    @staticmethod
    def _table(expression: str) -> RouteTable:
        return RouteTable.from_config(
            {
                "routes": [
                    {
                        "method": "POST",
                        "path": "/x",
                        "scope": "a:b",
                        "tool": "t",
                        "args": {"k": expression},
                    }
                ],
                "default": {"action": "deny"},
            }
        )

    def test_a_boolean_read_as_text_becomes_json_spelling(self) -> None:
        assert call(self._table("body.flag"), "POST", "/x", body=b'{"flag": true}').args["k"] == (
            "true"
        )
        assert call(self._table("body.flag"), "POST", "/x", body=b'{"flag": false}').args["k"] == (
            "false"
        )

    def test_a_boolean_read_as_a_number_is_refused(self) -> None:
        """`bool` is an `int` subclass in Python, so without the guard `true` would be 10000."""
        with pytest.raises(ExtractionError) as caught:
            call(self._table("body.flag:number"), "POST", "/x", body=b'{"flag": true}')
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_a_path_through_a_scalar_resolves_to_nothing(self) -> None:
        """`body.a.b` where `a` is a string: there is no `b`, so the argument is absent."""
        result = call(self._table("body.a.b"), "POST", "/x", body=b'{"a": "scalar"}')
        assert "k" not in result.args

    def test_a_null_is_treated_as_absent(self) -> None:
        result = call(self._table("body.a"), "POST", "/x", body=b'{"a": null}')
        assert "k" not in result.args

    def test_an_empty_body_parses_to_nothing(self) -> None:
        """Distinct from `body=None`: the bytes arrive, they are just empty."""
        result = call(self._table("body.a"), "POST", "/x", body=b"")
        assert "k" not in result.args
