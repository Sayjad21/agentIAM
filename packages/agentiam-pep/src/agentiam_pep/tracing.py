"""Installing a real OTEL SDK exporter — T-049.

`emitter.py`'s own docstring records the baseline: with only `opentelemetry-api` installed,
`trace.get_tracer(...)` hands back a `ProxyTracer`, every span is a `NonRecordingSpan`, and
nothing reaches anywhere. That measurement (5.58 µs, no SDK attached) is exactly the NFR-1
budget this module must not spend when it is not asked to run — so `configure_tracing` is a
no-op unless an OTLP endpoint is actually configured, which is true of every unit test,
every benchmark, and every deployment that has not set the one environment variable.

`ProxyTracer` is designed to be captured before a provider exists and to start delegating to
a real one the moment `trace.set_tracer_provider` is called — that is what lets
`emitter.py`'s module-level `_TRACER` (built at import time) start exporting real spans the
first time an app with tracing configured calls `create_app`.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

__all__ = ["configure_tracing"]

_SERVICE_NAME = "agentiam-pep"


def configure_tracing(*, endpoint: str | None, service_name: str = _SERVICE_NAME) -> bool:
    """Install a `TracerProvider` exporting to `endpoint` over OTLP/HTTP, once.

    Args:
        endpoint: The collector's traces endpoint, e.g.
            `http://localhost:4318/v1/traces`. `None` or empty means tracing stays the
            API-only no-op — the T-018 default, unchanged.
        service_name: `service.name` on every span's resource, so Tempo can tell PEP
            traces from control-plane ones once both export.

    Returns:
        Whether a provider was installed. `False` on a missing endpoint, and `False` when
        a real provider is already active — the OTEL API only honours the *first*
        `set_tracer_provider` call in a process and otherwise just logs a warning, and
        `create_app` can run more than once in one process (every test that builds an app
        does), so this check keeps that warning from firing on every call after the first.
    """
    if not endpoint:
        return False
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return False
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return True
