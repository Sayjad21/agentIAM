"""The PEP gateway — proxying, health, and metrics (`agentiam_pep.app`), T-018.

The upstream is a real ASGI app reached through `httpx.ASGITransport`, so these run under
`make test` with no Docker while still exercising the actual request/response path.

One test is the exception and binds a real port, because `ASGITransport` coalesces a
response body into a single chunk even when the app genuinely streams — measured, against
the upstream directly with no proxy involved. A chunk-count assertion made through it
would pass just as happily against a proxy that read the whole body into memory, so
streaming is tested over a socket or not at all.

**T-018 does not enforce anything.** The decision pipeline is T-019 and the scope
extractor is T-020; until they land this gateway forwards whatever it is given. That is a
deliberate, temporary state and `TestEnforcementIsNotWiredYet` exists to keep it from
becoming a quiet one — a policy enforcement point that enforces nothing is worse than no
gateway at all, because it looks like protection.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import threading
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from agentiam_pep.app import create_app
from agentiam_pep.config import PepSettings

PAYLOAD = b"a compressible payload " * 40


# --------------------------------------------------------------------------- upstream
async def echo(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse(
        {
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "body": body.decode(),
            "headers": dict(request.headers),
        }
    )


async def slow_chunks() -> AsyncIterator[bytes]:
    for i in range(3):
        yield f"chunk-{i};".encode()
        await asyncio.sleep(0.05)


async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(slow_chunks(), media_type="text/plain")


async def gzipped(request: Request) -> Response:
    body = gzip.compress(PAYLOAD)
    return Response(
        body,
        media_type="text/plain",
        headers={"content-encoding": "gzip", "content-length": str(len(body))},
    )


async def teapot(request: Request) -> PlainTextResponse:
    return PlainTextResponse("no", status_code=418, headers={"X-Reason": "teapot"})


async def cookies(request: Request) -> PlainTextResponse:
    response = PlainTextResponse("ok")
    response.raw_headers.append((b"set-cookie", b"a=1"))
    response.raw_headers.append((b"set-cookie", b"b=2"))
    return response


UPSTREAM = Starlette(
    routes=[
        Route("/stream", stream),
        Route("/gz", gzipped),
        Route("/teapot", teapot),
        Route("/cookies", cookies),
        Route("/{path:path}", echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    ]
)


def _free_port() -> int:
    """Ask the OS for an unused port rather than guessing one and racing CI."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _BackgroundServer:
    """Run an ASGI app on a real socket for the duration of a `with` block."""

    def __init__(self, app: object, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")  # type: ignore[arg-type]
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> _BackgroundServer:
        self._thread.start()
        deadline = time.perf_counter() + 10.0
        while not self._server.started and time.perf_counter() < deadline:
            time.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("uvicorn did not start within 10s")
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10.0)


@pytest.fixture
def settings() -> PepSettings:
    return PepSettings(upstream_base_url="http://upstream.test")


@pytest.fixture
async def client(settings: PepSettings) -> AsyncIterator[httpx.AsyncClient]:
    """A client pointed at the PEP, whose own upstream client is mounted on `UPSTREAM`."""
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=UPSTREAM),
        base_url=settings.upstream_base_url,
        timeout=settings.timeout,
    )
    app = create_app(settings=settings, upstream_client=upstream_client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://pep.test"
    ) as pep:
        yield pep
    await upstream_client.aclose()


class TestTransparentProxying:
    async def test_get_reaches_the_upstream(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/proxy/erp/invoices")
        assert response.status_code == 200
        assert response.json()["path"] == "/erp/invoices"
        assert response.json()["method"] == "GET"

    async def test_the_proxy_prefix_is_stripped(self, client: httpx.AsyncClient) -> None:
        """The upstream sees its own path, not the PEP's routing prefix."""
        assert (await client.get("/proxy/a/b/c")).json()["path"] == "/a/b/c"

    async def test_the_query_string_survives(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/proxy/x?a=1&b=2")).json()["query"] == "a=1&b=2"

    async def test_post_carries_its_body(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/proxy/x", content=b'{"amount": 1200}')
        assert response.json()["body"] == '{"amount": 1200}'

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def test_every_method_is_proxied(self, client: httpx.AsyncClient, method: str) -> None:
        response = await client.request(method, "/proxy/x", content=b"body")
        assert response.status_code == 200
        assert response.json()["method"] == method

    async def test_the_upstream_status_code_is_preserved(self, client: httpx.AsyncClient) -> None:
        """Including the ones a naive proxy would flatten into 200 or 502."""
        response = await client.get("/proxy/teapot")
        assert response.status_code == 418
        assert response.headers["x-reason"] == "teapot"

    async def test_the_authorization_header_reaches_the_upstream(
        self, client: httpx.AsyncClient
    ) -> None:
        """The header the whole project is about."""
        response = await client.get("/proxy/x", headers={"Authorization": "Bearer tok"})
        assert response.json()["headers"]["authorization"] == "Bearer tok"


class TestHeaderHygiene:
    async def test_date_and_server_are_not_duplicated(self, client: httpx.AsyncClient) -> None:
        """Measured against a live uvicorn pair: forwarding these gives the client two."""
        response = await client.get("/proxy/x")
        names = [name.decode().lower() for name, _ in response.headers.raw]
        assert names.count("date") <= 1
        assert names.count("server") <= 1

    async def test_multiple_set_cookie_headers_survive_separately(
        self, client: httpx.AsyncClient
    ) -> None:
        """Collapsing these into one comma-joined value silently breaks sessions."""
        response = await client.get("/proxy/cookies")
        cookie_values = [
            value.decode()
            for name, value in response.headers.raw
            if name.decode().lower() == "set-cookie"
        ]
        assert cookie_values == ["a=1", "b=2"]

    async def test_the_client_host_is_not_forwarded(self, client: httpx.AsyncClient) -> None:
        """A virtual-hosted upstream would route on it and answer as the wrong site."""
        response = await client.get("/proxy/x", headers={"Host": "pep.test"})
        assert response.json()["headers"]["host"] == "upstream.test"


class TestBodyIntegrity:
    async def test_a_compressed_response_arrives_intact(self, client: httpx.AsyncClient) -> None:
        """The measured failure case for T-018.

        Reading the upstream with a decoding iterator while forwarding its
        `content-encoding` and `content-length` produced, against a real server,
        `RemoteProtocolError: peer closed connection without sending complete message body
        (received 0 bytes, expected 52)`. Reading raw is what makes this pass.
        """
        response = await client.get("/proxy/gz")
        assert response.status_code == 200
        assert response.content == PAYLOAD

    async def test_a_streamed_body_arrives_in_pieces_over_a_real_socket(self) -> None:
        """Streaming is an acceptance criterion, and it needs a real server to test.

        `httpx.ASGITransport` — which every other test here uses — coalesces the response
        body into a single chunk even when the app genuinely streams. Measured, against
        the upstream app directly with no proxy in the way: **1 chunk**. So a chunk-count
        assertion through `ASGITransport` cannot tell a streaming proxy from a buffering
        one, and would pass just as happily if the PEP read the whole body into memory.

        Over a real socket the same upstream produced three chunks spaced ~0.25 s apart.
        That is what this asserts, at the cost of being the one test in the file that
        binds a port.
        """
        upstream_port = _free_port()
        pep_port = _free_port()

        upstream_server = _BackgroundServer(UPSTREAM, upstream_port)
        pep_app = create_app(
            settings=PepSettings(upstream_base_url=f"http://127.0.0.1:{upstream_port}")
        )
        pep_server = _BackgroundServer(pep_app, pep_port)

        with upstream_server, pep_server:
            arrivals: list[float] = []
            started = time.perf_counter()
            async with httpx.AsyncClient(timeout=10.0) as client:
                async with client.stream(
                    "GET", f"http://127.0.0.1:{pep_port}/proxy/stream"
                ) as response:
                    async for chunk in response.aiter_raw():
                        if chunk:
                            arrivals.append(time.perf_counter() - started)

        assert len(arrivals) > 1, f"the whole body arrived at once: {arrivals}"
        assert arrivals[-1] - arrivals[0] > 0.05, (
            f"chunks arrived together, so the proxy buffered: {arrivals}"
        )

    async def test_a_streamed_body_is_complete(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/proxy/stream")
        assert response.text == "chunk-0;chunk-1;chunk-2;"


class TestUpstreamFailure:
    async def test_an_unreachable_upstream_becomes_502(self, settings: PepSettings) -> None:
        """Fail closed with a gateway error, never a 200 with an empty body."""

        async def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url=settings.upstream_base_url
        )
        app = create_app(settings=settings, upstream_client=upstream_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://pep.test"
        ) as pep:
            response = await pep.get("/proxy/x")
        await upstream_client.aclose()

        assert response.status_code == 502
        assert response.json()["reason_code"] == "UPSTREAM_ERROR"

    async def test_a_timeout_becomes_504(self, settings: PepSettings) -> None:
        async def hang(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(hang), base_url=settings.upstream_base_url
        )
        app = create_app(settings=settings, upstream_client=upstream_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://pep.test"
        ) as pep:
            response = await pep.get("/proxy/x")
        await upstream_client.aclose()

        assert response.status_code == 504
        assert response.json()["reason_code"] == "UPSTREAM_ERROR"

    async def test_a_gateway_error_names_a_reason_code(self, settings: PepSettings) -> None:
        """Rule 5: every failure path carries one of the closed set (`PLAN.md` §6.9)."""
        from agentiam_core.errors import ReasonCode

        async def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url=settings.upstream_base_url
        )
        app = create_app(settings=settings, upstream_client=upstream_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://pep.test"
        ) as pep:
            body = (await pep.get("/proxy/x")).json()
        await upstream_client.aclose()

        assert body["reason_code"] in {code.value for code in ReasonCode}


class TestOperationalEndpoints:
    async def test_healthz_is_liveness_only(self, client: httpx.AsyncClient) -> None:
        """Liveness must not depend on anything external, or a restart loop follows."""
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readyz_reports_what_it_actually_checks(self, client: httpx.AsyncClient) -> None:
        """A readiness probe that claims more than it verifies is a lie with a green tick."""
        body = (await client.get("/readyz")).json()
        assert body["status"] == "ready"
        assert "checks" in body

    async def test_metrics_is_prometheus_exposition(self, client: httpx.AsyncClient) -> None:
        await client.get("/proxy/x")
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "agentiam_pep_requests_total" in response.text

    async def test_the_request_counter_moves(self, client: httpx.AsyncClient) -> None:
        before = (await client.get("/metrics")).text
        await client.get("/proxy/x")
        after = (await client.get("/metrics")).text
        assert before != after

    async def test_operational_endpoints_are_not_proxied(self, client: httpx.AsyncClient) -> None:
        """`/healthz` belongs to the PEP. Forwarding it would report on the wrong process."""
        assert (await client.get("/healthz")).json().get("path") is None


class TestEnforcementIsNotWiredYet:
    """T-018 proxies; it does not decide. Pinned so T-019 has to change it on purpose.

    A gateway that forwards everything while being called a *policy enforcement point* is
    worse than no gateway: it looks like protection. These tests fail the moment
    enforcement lands, which is the intended way to find out that it did.
    """

    async def test_a_request_with_no_token_is_still_proxied(
        self, client: httpx.AsyncClient
    ) -> None:
        assert (await client.get("/proxy/x")).status_code == 200

    async def test_the_app_says_so_on_readyz(self, client: httpx.AsyncClient) -> None:
        """Visible at runtime, not only in a docstring nobody reads in production."""
        assert (await client.get("/readyz")).json()["enforcing"] is False
