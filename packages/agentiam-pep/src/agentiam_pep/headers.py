"""Which headers cross the proxy hop, and which must not — T-018.

Forwarding everything verbatim is the default mistake, and it fails in three different
ways depending on the header. All three were measured against a real uvicorn upstream
before this module was written.

* **`date`, `server`** — the emitting server sets them, so forwarding the upstream's
  gives the client **two of each**.
* **`content-length`** — the proxy re-frames the body as chunked, so the upstream's length
  describes something it is not sending. Combined with a compressed upstream and a
  decoding read, this surfaces as a hard client error:
  ``RemoteProtocolError: peer closed connection without sending complete message body
  (received 0 bytes, expected 52)``.
* **hop-by-hop fields** — connection-specific by definition (RFC 9110 §7.6.1); forwarding
  `Connection` or `Transfer-Encoding` describes the wrong connection.

`content-encoding` deliberately **does** survive, because `proxy.py` reads the upstream
with `aiter_raw()` and forwards the bytes untouched. Dropping it would be the same bug in
reverse: a client trusting the headers would hand gzip to its JSON parser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

#: RFC 9110 §7.6.1. Connection-specific, so meaningless past one hop.
HOP_BY_HOP: Final = frozenset(
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

#: Set by whichever server actually emits the response. Forwarding duplicates them.
_SERVER_OWNED: Final = frozenset({"date", "server"})

#: Re-derived from the body actually sent, in both directions.
_FRAMING: Final = frozenset({"content-length"})

#: The client's `Host` names the proxy, not the upstream. httpx sets the right one from
#: the target URL; forwarding the original routes a virtual-hosted upstream to the wrong
#: site.
_REQUEST_ONLY: Final = frozenset({"host"})


def _without(headers: Iterable[tuple[str, str]], excluded: frozenset[str]) -> list[tuple[str, str]]:
    """Drop `excluded` names, case-insensitively, preserving order and duplicates.

    A list of pairs rather than a mapping throughout, because `Set-Cookie` legitimately
    repeats and collapsing it into one comma-joined value silently breaks sessions.
    """
    return [(name, value) for name, value in headers if name.lower() not in excluded]


def filter_request_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers safe to forward from the client to the upstream.

    `Authorization` survives — it carries the token, and from T-019 the decision pipeline
    reads it. A filter that swallowed it would break AgentIAM rather than merely break
    proxying.
    """
    return _without(headers, HOP_BY_HOP | _FRAMING | _REQUEST_ONLY)


def filter_response_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers safe to forward from the upstream back to the client."""
    return _without(headers, HOP_BY_HOP | _FRAMING | _SERVER_OWNED)


__all__ = ["HOP_BY_HOP", "filter_request_headers", "filter_response_headers"]
