"""Header filtering for the reverse proxy (`agentiam_pep.headers`) — T-018.

A proxy that forwards every header verbatim is subtly broken, and the breakage is not
uniform: some headers are merely duplicated, one produces a hard protocol error.

Measured while building this, against a real uvicorn upstream:

* Forwarding the upstream's `date` and `server` gives the client **two of each**, because
  the proxy's own server adds its own.
* Forwarding `content-length` while re-framing the body as chunked describes a body that
  is not being sent. With a compressed upstream and a decoding read, the client fails with
  `RemoteProtocolError: peer closed connection without sending complete message body
  (received 0 bytes, expected 52)`.

So these tests are about *what must not survive the hop*, and each exclusion has a reason
rather than a citation.
"""

from __future__ import annotations

import pytest

from agentiam_pep.headers import (
    HOP_BY_HOP,
    filter_request_headers,
    filter_response_headers,
)


class TestHopByHopSet:
    def test_it_is_the_rfc_9110_set(self) -> None:
        """The connection-specific fields, which by definition do not cross a hop."""
        assert HOP_BY_HOP == frozenset(
            {
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailer",
                "transfer-encoding",
                "upgrade",
            }
        )

    def test_it_is_lowercase(self) -> None:
        """Comparisons are done lowercased; a capitalised entry would never match."""
        assert all(name == name.lower() for name in HOP_BY_HOP)


class TestResponseHeaders:
    def test_hop_by_hop_headers_are_dropped(self) -> None:
        filtered = filter_response_headers(
            [("connection", "keep-alive"), ("transfer-encoding", "chunked"), ("x-ok", "1")]
        )
        assert filtered == [("x-ok", "1")]

    def test_content_length_is_dropped(self) -> None:
        """The proxy re-frames the body as chunked, so upstream's length is a lie.

        This is the one that fails loudly rather than subtly — see the module docstring.
        """
        assert filter_response_headers([("content-length", "52")]) == []

    def test_date_and_server_are_dropped(self) -> None:
        """The emitting server sets these. Forwarding them gives the client two of each."""
        assert filter_response_headers([("date", "x"), ("server", "upstream")]) == []

    def test_content_encoding_survives(self) -> None:
        """The body is forwarded byte-for-byte with `aiter_raw`, so it is still encoded.

        Dropping this while forwarding compressed bytes would be the same bug in reverse:
        a client that trusts the headers would hand gzip to its JSON parser.
        """
        assert filter_response_headers([("content-encoding", "gzip")]) == [
            ("content-encoding", "gzip")
        ]

    def test_content_type_survives(self) -> None:
        assert ("content-type", "application/json") in filter_response_headers(
            [("content-type", "application/json")]
        )

    def test_set_cookie_survives_more_than_once(self) -> None:
        """`Set-Cookie` is the header that must not be collapsed into one value."""
        filtered = filter_response_headers([("set-cookie", "a=1"), ("set-cookie", "b=2")])
        assert filtered == [("set-cookie", "a=1"), ("set-cookie", "b=2")]

    def test_matching_is_case_insensitive(self) -> None:
        assert filter_response_headers([("Content-Length", "5"), ("CONNECTION", "close")]) == []

    def test_order_is_preserved(self) -> None:
        """Some clients care, and there is no reason to disturb it."""
        given = [("x-a", "1"), ("x-b", "2"), ("x-c", "3")]
        assert filter_response_headers(given) == given


class TestRequestHeaders:
    def test_hop_by_hop_headers_are_dropped(self) -> None:
        assert filter_request_headers([("connection", "close"), ("x-ok", "1")]) == [("x-ok", "1")]

    def test_host_is_dropped(self) -> None:
        """The upstream's host, not the proxy's, and httpx sets it from the target URL.

        Forwarding the client's `Host` sends the upstream a name for a different server —
        which for a virtual-hosted upstream routes the request to the wrong place.
        """
        assert filter_request_headers([("host", "pep.internal")]) == []

    def test_content_length_is_dropped(self) -> None:
        """Httpx re-derives it from the body it actually sends."""
        assert filter_request_headers([("content-length", "17")]) == []

    def test_authorization_survives(self) -> None:
        """The token has to reach the upstream — and, from T-019, the decision pipeline.

        This is the header the entire project is about; a filter that swallowed it would
        break AgentIAM rather than merely break proxying.
        """
        assert filter_request_headers([("authorization", "Bearer abc")]) == [
            ("authorization", "Bearer abc")
        ]

    def test_content_type_survives(self) -> None:
        assert filter_request_headers([("content-type", "application/json")]) == [
            ("content-type", "application/json")
        ]

    @pytest.mark.parametrize("name", sorted(HOP_BY_HOP))
    def test_every_hop_by_hop_name_is_actually_filtered(self, name: str) -> None:
        """Parametrised so adding a name to the set without handling it fails here."""
        assert filter_request_headers([(name, "x")]) == []
        assert filter_response_headers([(name, "x")]) == []
