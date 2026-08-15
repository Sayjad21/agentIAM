"""The PEP gateway: an ASGI app that reverse-proxies to an upstream — T-018.

> **This gateway does not enforce anything yet.** T-018 is the transport; the decision
> pipeline is T-019 and the scope extractor is T-020. Until those land, every request is
> forwarded, token or no token. `/readyz` reports `enforcing: false` so the gap is visible
> at runtime rather than only in this docstring, and
> `tests/unit/test_pep_app.py::TestEnforcementIsNotWiredYet` fails the moment it changes —
> which is the intended way to notice that it did.
>
> A component called a *policy enforcement point* that forwards everything is worse than
> no gateway, because it looks like protection.

Three things about the forwarding path were settled by measurement rather than by reading
(see `headers.py` and `config.py` for the numbers):

1. The upstream response is read with **`aiter_raw()`**. Reading decoded bytes while
   forwarding the upstream's `content-encoding` produces a hard client-side
   `RemoteProtocolError`.
2. `date`, `server` and `content-length` are stripped, or the client gets duplicates and a
   length describing a body that is not being sent.
3. Bodies stream in both directions. Nothing is buffered, so a large upload or a slow
   event stream costs the PEP a constant amount of memory.

**Trailers are not handled, and cannot be on this stack.** See ADR-020.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client import generate_latest as render_metrics

from agentiam_core.errors import ReasonCode
from agentiam_pep.config import PepSettings
from agentiam_pep.headers import filter_request_headers, filter_response_headers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: Where proxied traffic enters. `PLAN.md` §8: `ANY /proxy/{upstream_path}`.
PROXY_PREFIX: Final = "/proxy"

#: Flipped by T-019 when the decision pipeline is wired in. Reported on `/readyz`.
ENFORCING: Final = False


def _build_metrics(registry: CollectorRegistry) -> tuple[Counter, Histogram]:
    """Per-app collectors on their own registry.

    Not the global default registry: two apps in one test process — which is exactly what
    the test suite does — would otherwise collide on duplicate collector names.
    """
    requests = Counter(
        "agentiam_pep_requests_total",
        "Requests handled by the PEP.",
        labelnames=("method", "status"),
        registry=registry,
    )
    latency = Histogram(
        "agentiam_pep_upstream_latency_seconds",
        "Time spent waiting for the upstream to begin responding.",
        labelnames=("method",),
        registry=registry,
    )
    return requests, latency


def create_app(
    *,
    settings: PepSettings,
    upstream_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the gateway.

    Args:
        settings: Upstream address, timeouts, pool limits.
        upstream_client: The client used for upstream calls. Injected by tests so the
            upstream can be an in-process ASGI app; built from `settings` otherwise. Also
            the seam T-021 uses to attach a lease pool.

    Returns:
        A FastAPI application.
    """
    client = upstream_client or settings.build_client()
    registry = CollectorRegistry()
    requests_total, upstream_latency = _build_metrics(registry)

    app = FastAPI(
        title="AgentIAM PEP",
        summary="Policy enforcement point. Transport only until T-019.",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness. Deliberately checks nothing external.

        A liveness probe that depends on an upstream turns one outage into a restart loop.
        """
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness, reporting only what it actually verifies.

        Right now that is "the upstream client exists" — the process can serve. It does
        not dial the upstream: a readiness probe that fails on someone else's outage
        removes this instance from rotation for a fault it cannot fix.
        """
        return JSONResponse(
            {
                "status": "ready",
                "enforcing": ENFORCING,
                "checks": {
                    "upstream_client": client is not None,
                    "upstream_base_url": settings.upstream_base_url,
                },
            }
        )

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        """Prometheus exposition for this app's registry."""
        return PlainTextResponse(render_metrics(registry).decode(), media_type=CONTENT_TYPE_LATEST)

    @app.api_route(
        PROXY_PREFIX + "/{upstream_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        # The handler returns a `Response` subclass directly, so there is no schema to
        # derive. Without this FastAPI tries to build a Pydantic model from the union
        # annotation and refuses to start the app at all.
        response_model=None,
    )
    async def proxy(upstream_path: str, request: Request) -> StreamingResponse | JSONResponse:
        """Forward one request upstream and stream the answer back."""
        target = httpx.URL(path="/" + upstream_path, query=request.url.query.encode() or None)
        upstream_request = client.build_request(
            request.method,
            target,
            headers=filter_request_headers(request.headers.items()),
            content=request.stream(),
        )

        method = request.method
        try:
            with upstream_latency.labels(method).time():
                upstream_response = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            requests_total.labels(method, "504").inc()
            return _gateway_error(504, "the upstream did not respond in time")
        except httpx.HTTPError:
            requests_total.labels(method, "502").inc()
            return _gateway_error(502, "the upstream could not be reached")

        requests_total.labels(method, str(upstream_response.status_code)).inc()

        async def body() -> AsyncIterator[bytes]:
            # `aiter_raw`, never `aiter_bytes`: the bytes must reach the client exactly as
            # the upstream sent them, because `content-encoding` is forwarded with them.
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()

        response = StreamingResponse(body(), status_code=upstream_response.status_code)
        # Assigned after construction rather than passed in, because Starlette's `headers=`
        # takes a mapping and `Set-Cookie` legitimately repeats — a mapping keeps only the
        # last one, which silently drops every cookie but one.
        response.raw_headers = [
            (name.encode(), value.encode())
            for name, value in filter_response_headers(
                [(k.decode(), v.decode()) for k, v in upstream_response.headers.raw]
            )
        ]
        return response

    return app


def _gateway_error(status: int, detail: str) -> JSONResponse:
    """Fail closed with a reason code from the closed set (rule 5, `PLAN.md` §6.9)."""
    return JSONResponse(
        {"reason_code": ReasonCode.UPSTREAM_ERROR.value, "detail": detail},
        status_code=status,
    )


__all__ = ["ENFORCING", "PROXY_PREFIX", "create_app"]
