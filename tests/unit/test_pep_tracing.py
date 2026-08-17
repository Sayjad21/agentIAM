"""`configure_tracing` — T-049.

Only its decision logic is tested here (no endpoint → no-op; a real provider already
installed → no-op), not real span export — `opentelemetry-exporter-otlp-proto-http` talking
to a live collector belongs to the manual/compose-level check T-049's ADR records, not a unit
test with no network. `trace._TRACER_PROVIDER`/`_TRACER_PROVIDER_SET_ONCE` are reset around
every test in this module (`opentelemetry`'s own test suite resets the same two globals the
same way) so nothing here leaks a real `TracerProvider` into the rest of the session — in
particular `test_pep_emitter.py`'s benchmark, which measures the documented "no SDK attached"
case and must keep measuring exactly that regardless of test order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agentiam_pep.tracing import configure_tracing

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_global_tracer_provider() -> Iterator[None]:
    """Undo whatever `set_tracer_provider` this module's tests do, unconditionally."""
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    yield
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False


class TestConfigureTracing:
    def test_no_endpoint_is_a_no_op(self) -> None:
        assert configure_tracing(endpoint=None) is False
        assert configure_tracing(endpoint="") is False
        assert not isinstance(trace.get_tracer_provider(), TracerProvider)

    def test_an_endpoint_installs_a_real_provider(self) -> None:
        installed = configure_tracing(endpoint="http://localhost:4318/v1/traces")
        assert installed is True
        assert isinstance(trace.get_tracer_provider(), TracerProvider)

    def test_a_second_call_is_a_no_op_even_with_an_endpoint(self) -> None:
        """`create_app` runs once per test that builds an app — this is why that's safe.

        OTEL's own API only honours the first `set_tracer_provider` call in a process and
        otherwise just warns; this guard is what keeps that warning from firing on every
        subsequent `create_app` call in a shared test process.
        """
        first = configure_tracing(endpoint="http://localhost:4318/v1/traces")
        second = configure_tracing(endpoint="http://localhost:4318/v1/traces")
        assert first is True
        assert second is False

    def test_the_service_name_is_on_the_resource(self) -> None:
        configure_tracing(endpoint="http://localhost:4318/v1/traces", service_name="test-svc")
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert provider.resource.attributes["service.name"] == "test-svc"
