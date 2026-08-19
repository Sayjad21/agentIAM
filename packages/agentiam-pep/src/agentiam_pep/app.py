"""The PEP gateway: an ASGI app that reverse-proxies to an upstream — T-018.

> **Enforcement is on when a `pipeline` is supplied, and off when it is not.** Built without
> one, this is the T-018 transport: every request is forwarded, token or no token, and
> `/readyz` reports `enforcing: false` so the gap is visible at runtime rather than only in a
> docstring. Built with one (T-023), every proxied request runs the ten steps first and a
> refusal never reaches the upstream.
>
> The flag is derived from the wiring rather than declared, because a constant saying
> `enforcing: true` next to a gateway that forwards everything is precisely the failure
> `STATUS.md` gap 11 recorded — a component called a *policy enforcement point* that looks
> like protection and is not.

Three things about the forwarding path were settled by measurement rather than by reading
(see `headers.py` and `config.py` for the numbers):

1. The upstream response is read with **`aiter_raw()`**. Reading decoded bytes while
   forwarding the upstream's `content-encoding` produces a hard client-side
   `RemoteProtocolError`.
2. `date`, `server` and `content-length` are stripped, or the client gets duplicates and a
   length describing a body that is not being sent.
3. Bodies stream in both directions. Nothing is buffered here, so a large upload or a slow
   event stream costs the PEP a constant amount of memory.

   **Conditional as of T-020** (ADR-023): a route whose mapping reads a `body.` argument
   must parse the body to authorize the request, so extraction buffers it — bounded by
   `max_extract_body_bytes`, 1 MiB by default, over which the request is denied rather than
   read. Routes with no `body.` source are unaffected. Measured: `Request.json()` then
   `stream()` replays the body byte-identically, but the reverse order raises
   `RuntimeError: Stream consumed`, so extraction must precede forwarding.

**Trailers are not handled, and cannot be on this stack.** See ADR-020.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client import generate_latest as render_metrics

from agentiam_core.errors import ReasonCode
from agentiam_pep.config import PepSettings
from agentiam_pep.headers import filter_request_headers, filter_response_headers
from agentiam_pep.pipeline import Authorized, Refused
from agentiam_pep.tracing import configure_tracing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import Lifespan

    from agentiam_pep.pipeline import Pipeline

#: Where proxied traffic enters. `PLAN.md` §8: `ANY /proxy/{upstream_path}`.
PROXY_PREFIX: Final = "/proxy"


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
    pipeline: Pipeline | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Build the gateway.

    Args:
        settings: Upstream address, timeouts, pool limits.
        upstream_client: The client used for upstream calls. Injected by tests so the
            upstream can be an in-process ASGI app; built from `settings` otherwise.
        pipeline: The ten steps (T-023). Supplied means the gateway enforces; omitted means
            it is the T-018 transport and says so on `/readyz`.
        lifespan: Startup/shutdown for whatever the caller assembled around this app —
            the emitter's drain task, the settlement queue, the revocation consumer, the
            lease pool's release. Supplied by the deployment composition root
            (`scripts/pep_service.py`, T-056); `None` everywhere else, which is why every
            existing test and benchmark is unaffected. This is a parameter rather than
            `@app.on_event` because the latter is deprecated as of FastAPI 0.141, and
            because *what* to start belongs to whoever built the object graph, not here.

    Returns:
        A FastAPI application.
    """
    configure_tracing(endpoint=settings.otel_exporter_endpoint)
    client = upstream_client or settings.build_client()
    enforcing = pipeline is not None
    registry = CollectorRegistry()
    requests_total, upstream_latency = _build_metrics(registry)

    app = FastAPI(
        title="AgentIAM PEP",
        summary="Policy enforcement point.",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
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
                "enforcing": enforcing,
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
        """Authorize, then forward one request upstream and stream the answer back."""
        # T-049: opened before `authorize()` runs, so its first line — `current_trace_id()`
        # — has a real span to read, and left open across the upstream call below so that
        # call lands in the same trace rather than an unrelated one. A no-op (`span is
        # None`) when there's no pipeline (T-018 mode) or tracing is off.
        span_cm = pipeline.request_span() if pipeline is not None else contextlib.nullcontext(None)
        with span_cm as span:
            authorized: Authorized | None = None
            if pipeline is not None:
                # Read the body only when some route maps a `body.` argument; otherwise the
                # stream is left untouched and the proxy hop stays constant-memory
                # (ADR-023). Measured: `json()` then `stream()` replays fine, the reverse
                # raises.
                request_body = await request.body() if _reads_body(request) else None
                outcome = await pipeline.authorize(
                    method=request.method,
                    path="/" + upstream_path,
                    query_string=request.url.query,
                    headers=list(request.headers.items()),
                    body=request_body,
                )
                if isinstance(outcome, Refused):
                    if span is not None:
                        span.set_attribute("agentiam.outcome", "refuse")
                        span.set_attribute("agentiam.reason_code", outcome.reason_code.value)
                    requests_total.labels(request.method, str(outcome.status)).inc()
                    return JSONResponse(outcome.body(), status_code=outcome.status)
                authorized = outcome
                if span is not None:
                    span.set_attribute("agentiam.outcome", "allow")
                    span.set_attribute("agentiam.scope", authorized.extraction.scope)

            target = httpx.URL(path="/" + upstream_path, query=request.url.query.encode() or None)
            upstream_request = client.build_request(
                request.method,
                target,
                headers=filter_request_headers(request.headers.items()),
                content=request.stream(),
            )

            method = request.method
            upstream_span_cm = (
                pipeline.child_span("agentiam.upstream_call")
                if pipeline is not None
                else contextlib.nullcontext(None)
            )
            with upstream_span_cm as upstream_span:
                if upstream_span is not None:
                    upstream_span.set_attribute("http.request.method", method)
                    upstream_span.set_attribute("url.path", "/" + upstream_path)
                try:
                    with upstream_latency.labels(method).time():
                        upstream_response = await client.send(upstream_request, stream=True)
                except httpx.TimeoutException:
                    requests_total.labels(method, "504").inc()
                    if upstream_span is not None:
                        upstream_span.set_attribute("agentiam.upstream_error", "timeout")
                    return _gateway_error(504, "the upstream did not respond in time")
                except httpx.HTTPError:
                    requests_total.labels(method, "502").inc()
                    if upstream_span is not None:
                        upstream_span.set_attribute("agentiam.upstream_error", "unreachable")
                    return _gateway_error(502, "the upstream could not be reached")

                if upstream_span is not None:
                    upstream_span.set_attribute(
                        "http.response.status_code", upstream_response.status_code
                    )

            requests_total.labels(method, str(upstream_response.status_code)).inc()

            async def body() -> AsyncIterator[bytes]:
                # `aiter_raw`, never `aiter_bytes`: the bytes must reach the client exactly
                # as the upstream sent them, because `content-encoding` is forwarded with
                # them.
                try:
                    async for chunk in upstream_response.aiter_raw():
                        yield chunk
                finally:
                    await upstream_response.aclose()

            if authorized is not None and pipeline is not None:
                # Step 9. The reserved amount is settled as-is: reading the upstream's own
                # reported charge would mean buffering its body, and a tool that
                # over-reports is clamped by the ledger anyway (spec 04 §4.4's G2).
                pipeline.settle(authorized)

            response = StreamingResponse(body(), status_code=upstream_response.status_code)
            # Assigned after construction rather than passed in, because Starlette's
            # `headers=` takes a mapping and `Set-Cookie` legitimately repeats — a mapping
            # keeps only the last one, which silently drops every cookie but one.
            response.raw_headers = [
                (name.encode(), value.encode())
                for name, value in filter_response_headers(
                    [(k.decode(), v.decode()) for k, v in upstream_response.headers.raw]
                )
            ]
            return response

    return app


def _reads_body(request: Request) -> bool:
    """Whether extraction needs this request's body.

    Only methods that carry one, and only when it is JSON — a form upload through a route
    whose caveats do not constrain its body is a legitimate request that must keep streaming
    (spec 10 §6).
    """
    if request.method in {"GET", "HEAD", "DELETE", "OPTIONS"}:
        return False
    content_type = request.headers.get("content-type", "")
    return "json" in content_type.lower()


def _gateway_error(status: int, detail: str) -> JSONResponse:
    """Fail closed with a reason code from the closed set (rule 5, `PLAN.md` §6.9)."""
    return JSONResponse(
        {"reason_code": ReasonCode.UPSTREAM_ERROR.value, "detail": detail},
        status_code=status,
    )


__all__ = ["PROXY_PREFIX", "create_app"]
