"""TM-26 — the PEP must never authorize a value the upstream will not act on.

The gateway forwards the original bytes, so two parsers read every request: ours and the
upstream's. Where they disagree about what the request *says*, a caveat is enforced against
a value that never executes, and the decision record is honest about a request that did not
happen.

**Measured** for the query string `amount=1&amount=999999`:

    dict(QueryParams(...))     -> '999999'   (last)
    urllib parse_qsl[0]        -> '1'        (first)
    Go net/http, Java          -> first
    PHP                        -> last

So `amount <= 5000` checked against `1` passes while a Go upstream executes `999999`. The
extractor's answer is to never pick a winner: ambiguity is refused (spec 10 §5.2).

These are the tests behind that claim. They are marked `security` because they are the
red-team case, not an ordinary unit test — the guard is only worth anything if it is shown
to fire.
"""

from __future__ import annotations

import json

import pytest

from agentiam_core.errors import ReasonCode
from agentiam_pep.extractor import Extraction, ExtractionError, RouteTable, extract

pytestmark = pytest.mark.security

CONFIG: dict[str, object] = {
    "routes": [
        {
            "method": "GET",
            "path": "/search",
            "scope": "search:read",
            "tool": "search_api",
            "args": {"search.limit": "query.limit:number"},
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
            "method": "POST",
            "path": "/email/send",
            "scope": "email:send",
            "tool": "email_api",
            "args": {"email.request_id": "header.x-request-id"},
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


class TestRepeatedQueryParameters:
    def test_a_repeated_mapped_parameter_is_refused(self, table: RouteTable) -> None:
        """The attack: two values, and the PEP cannot know which one executes."""
        with pytest.raises(ExtractionError) as caught:
            call(table, "GET", "/search", query="limit=1&limit=999999")
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_refusal_does_not_depend_on_the_order(self, table: RouteTable) -> None:
        """Neither first-wins nor last-wins is safe, so neither ordering may slip through."""
        for query in ("limit=999999&limit=1", "limit=1&limit=999999"):
            with pytest.raises(ExtractionError):
                call(table, "GET", "/search", query=query)

    def test_identical_repeated_values_are_still_refused(self, table: RouteTable) -> None:
        """`limit=1&limit=1` is unambiguous in value but not in what a parser will do with it.

        Some parsers concatenate rather than choose. Refusing every repetition keeps the rule
        one sentence long, and a legitimate client does not send it twice.
        """
        with pytest.raises(ExtractionError):
            call(table, "GET", "/search", query="limit=1&limit=1")

    def test_a_repeated_unmapped_parameter_is_allowed(self, table: RouteTable) -> None:
        """Nothing constrains it, so it is not ambiguous in any way that matters (spec 10 §5.2).

        The PEP is an authorization point, not a general request sanitizer. Denying here would
        break ordinary clients — repeated filter parameters are normal — for no security gain.
        """
        result = call(table, "GET", "/search", query="limit=5&tag=a&tag=b")
        assert result.args["search.limit"] == 50_000

    def test_a_single_value_still_works(self, table: RouteTable) -> None:
        """The guard must not deny the ordinary case."""
        assert call(table, "GET", "/search", query="limit=5").args["search.limit"] == 50_000


class TestDuplicateJsonKeys:
    def test_a_duplicate_mapped_key_is_refused(self, table: RouteTable) -> None:
        """`json.loads` silently keeps the last; `object_pairs_hook` can see both (measured)."""
        body = b'{"amount": "1", "recipient": {"account_id": "a"}, "amount": "999999"}'
        with pytest.raises(ExtractionError) as caught:
            call(table, "POST", "/payments", body=body)
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_a_duplicate_nested_key_on_a_mapped_path_is_refused(self, table: RouteTable) -> None:
        body = b'{"amount": "1", "recipient": {"account_id": "a", "account_id": "b"}}'
        with pytest.raises(ExtractionError):
            call(table, "POST", "/payments", body=body)

    def test_a_duplicate_key_off_the_mapped_paths_is_allowed(self, table: RouteTable) -> None:
        body = b'{"amount": "1", "recipient": {"account_id": "a"}, "memo": "x", "memo": "y"}'
        result = call(table, "POST", "/payments", body=body)
        assert result.args["payment.amount"] == 10_000

    def test_a_duplicate_intermediate_object_is_refused(self, table: RouteTable) -> None:
        """Two `recipient` objects: which one holds the account the upstream will pay?"""
        body = (
            b'{"amount": "1", "recipient": {"account_id": "a"}, "recipient": {"account_id": "b"}}'
        )
        with pytest.raises(ExtractionError):
            call(table, "POST", "/payments", body=body)


class TestRepeatedHeaders:
    def test_a_repeated_mapped_header_is_refused(self, table: RouteTable) -> None:
        with pytest.raises(ExtractionError) as caught:
            call(
                table,
                "POST",
                "/email/send",
                headers=[("x-request-id", "a"), ("x-request-id", "b")],
                body=b"{}",
            )
        assert caught.value.reason is ReasonCode.MALFORMED_REQUEST

    def test_header_names_are_matched_case_insensitively(self, table: RouteTable) -> None:
        """HTTP header names are case-insensitive, so `X-Request-Id` must collide with the rule."""
        with pytest.raises(ExtractionError):
            call(
                table,
                "POST",
                "/email/send",
                headers=[("X-Request-Id", "a"), ("x-request-id", "b")],
                body=b"{}",
            )

    def test_a_repeated_unmapped_header_is_allowed(self, table: RouteTable) -> None:
        result = call(
            table,
            "POST",
            "/email/send",
            headers=[("x-request-id", "a"), ("accept", "x"), ("accept", "y")],
            body=b"{}",
        )
        assert result.args["email.request_id"] == "a"


class TestTheGuardIsLoadBearing:
    """A guard never seen to fire is not a guard.

    Each test above asserts a denial. This one asserts the *consequence* of removing the
    guard, so the tests above cannot pass vacuously: with ambiguity permitted, the extracted
    value would depend on the parser, and that is precisely the divergence TM-26 describes.
    """

    def test_the_two_candidate_values_are_both_reachable(self) -> None:
        """Whichever rule the extractor picked, some real upstream would disagree with it."""
        import urllib.parse

        from starlette.datastructures import QueryParams

        qs = "limit=1&limit=999999"
        last = dict(QueryParams(qs))["limit"]
        first = urllib.parse.parse_qsl(qs)[0][1]

        assert last == "999999"
        assert first == "1"
        assert last != first, "if these agreed there would be no threat to mitigate"

    def test_duplicate_json_keys_are_detectable(self) -> None:
        """The refusal is only implementable because the duplicates survive parsing."""
        seen: list[tuple[str, object]] = []

        def keep_all(pairs: list[tuple[str, object]]) -> dict[str, object]:
            seen.extend(pairs)
            return dict(pairs)

        collapsed = json.loads('{"amount": 1, "amount": 999999}')
        json.loads('{"amount": 1, "amount": 999999}', object_pairs_hook=keep_all)

        assert collapsed == {"amount": 999999}, "the plain parse silently loses one"
        assert len(seen) == 2, "object_pairs_hook must see both for the guard to be possible"
