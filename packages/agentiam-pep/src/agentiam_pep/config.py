"""PEP configuration, and the timeout budget in particular — T-018.

One measured fact shapes most of this. **`httpx`'s transport-level `retries` covers
connection establishment only, and it multiplies the connect timeout.** With `retries=2`
and a 2 s timeout, a refused connection took **6.56 s** to surface — three attempts, not
one. A setting that reads as "give up after two seconds" gives up after six and a half,
and `worst_case_connect_s` exists so that number is visible rather than emergent.

Two consequences worth stating:

* Retries are safe here *because* they only cover connection establishment — nothing has
  been sent yet, so nothing is replayed. An application-level retry on a 5xx would be a
  different matter and must not be added for non-idempotent methods.
* The read timeout is the long one. Connecting should be quick; a slow *response* from a
  healthy upstream is ordinary. Reversing them produces a proxy that abandons slow
  endpoints and waits patiently for dead hosts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

import httpx

#: Prefix for every environment variable this reads.
ENV_PREFIX: Final = "AGENTIAM_PEP_"


def _positive(value: float, field: str) -> float:
    if value <= 0:
        raise ValueError(f"{field} must be positive, got {value}; httpx reads 0 as 'no timeout'")
    return value


@dataclass(frozen=True, slots=True)
class PepSettings:
    """Everything the gateway needs to start.

    Attributes:
        upstream_base_url: Where proxied requests go. No default — a proxy pointed
            somewhere by accident is worse than one that refuses to start.
        connect_timeout_s: Per *attempt*. See `worst_case_connect_s`.
        read_timeout_s: How long to wait for the upstream's response.
        connect_retries: Extra connection attempts. Safe to retry because nothing has been
            sent yet at that point.
        write_timeout_s: How long to wait while sending a request body.
        pool_timeout_s: How long to wait for a free connection from the pool.
        max_connections: Upper bound on the upstream pool.
        max_keepalive_connections: How many of those are kept warm.
        otel_exporter_endpoint: The OTEL collector's OTLP/HTTP traces endpoint, e.g.
            `http://localhost:4318/v1/traces`. `None` (the default) leaves tracing at the
            T-018 API-only no-op — unset in every unit test and benchmark, so `emitter.py`'s
            measured 5.58 µs figure is unaffected by T-049 existing.
    """

    upstream_base_url: str
    connect_timeout_s: float = 1.0
    read_timeout_s: float = 30.0
    write_timeout_s: float = 10.0
    pool_timeout_s: float = 5.0
    connect_retries: int = 2
    max_connections: int = 100
    max_keepalive_connections: int = 20
    otel_exporter_endpoint: str | None = None

    def __post_init__(self) -> None:
        """Validate at construction, so a bad value fails at startup rather than in flight."""
        if not self.upstream_base_url:
            raise ValueError("upstream_base_url is required")
        _positive(self.connect_timeout_s, "connect_timeout_s")
        _positive(self.read_timeout_s, "read_timeout_s")
        _positive(self.write_timeout_s, "write_timeout_s")
        _positive(self.pool_timeout_s, "pool_timeout_s")
        if self.connect_retries < 0:
            raise ValueError(f"connect_retries must be >= 0, got {self.connect_retries}")

    @property
    def worst_case_connect_s(self) -> float:
        """How long a dead upstream can hold a request before the client hears anything.

        `retries=N` means `N+1` attempts, each bounded by `connect_timeout_s`. Measured,
        not assumed: `retries=2` with a 2 s timeout took 6.56 s against a refused port.
        """
        return self.connect_timeout_s * (self.connect_retries + 1)

    @property
    def timeout(self) -> httpx.Timeout:
        """The four-part timeout httpx wants, rather than one number for everything."""
        return httpx.Timeout(
            connect=self.connect_timeout_s,
            read=self.read_timeout_s,
            write=self.write_timeout_s,
            pool=self.pool_timeout_s,
        )

    @property
    def limits(self) -> httpx.Limits:
        """Connection pooling — the hot path, so the pool is shared for the process."""
        return httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
        )

    @classmethod
    def from_env(cls) -> PepSettings:
        """Build from `AGENTIAM_PEP_*` variables.

        Raises:
            ValueError: If `AGENTIAM_PEP_UPSTREAM_BASE_URL` is unset, or a numeric
                variable does not parse.
        """
        upstream = os.environ.get(f"{ENV_PREFIX}UPSTREAM_BASE_URL")
        if not upstream:
            raise ValueError(f"{ENV_PREFIX}UPSTREAM_BASE_URL is required")

        def number(name: str, default: float) -> float:
            raw = os.environ.get(f"{ENV_PREFIX}{name.upper()}")
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{ENV_PREFIX}{name.upper()} must be a number, got {raw!r}"
                ) from exc

        return cls(
            upstream_base_url=upstream,
            connect_timeout_s=number("connect_timeout_s", 1.0),
            read_timeout_s=number("read_timeout_s", 30.0),
            write_timeout_s=number("write_timeout_s", 10.0),
            pool_timeout_s=number("pool_timeout_s", 5.0),
            connect_retries=int(number("connect_retries", 2)),
            max_connections=int(number("max_connections", 100)),
            max_keepalive_connections=int(number("max_keepalive_connections", 20)),
            otel_exporter_endpoint=os.environ.get(f"{ENV_PREFIX}OTEL_EXPORTER_ENDPOINT") or None,
        )

    def build_client(self) -> httpx.AsyncClient:
        """The shared upstream client: one pool for the process, retries at the transport."""
        return httpx.AsyncClient(
            base_url=self.upstream_base_url,
            timeout=self.timeout,
            limits=self.limits,
            transport=httpx.AsyncHTTPTransport(retries=self.connect_retries, limits=self.limits),
        )


__all__ = ["ENV_PREFIX", "PepSettings"]
