"""An in-process TCP fault proxy — the partition mechanism for CH-4 and CH-8 (T-052).

`PLAN.md` §13.2 names toxiproxy for CH-4. This is a deliberate substitution, recorded as
ADR-049, for three reasons that were checked rather than assumed:

* `testcontainers`' community modules were enumerated at the version this repo pins and
  there is **no toxiproxy module** — it would mean a bare `DockerContainer` plus an HTTP
  control client, a new dependency, against Rule 7.
* The development host is Windows with Docker Desktop, where testcontainers already needs
  `TESTCONTAINERS_RYUK_DISABLED` to publish a port at all. One more container in the path
  is one more thing that fails differently on the two platforms the suite must run on.
* A chaos scenario is only worth running if its fault is *exactly* reproducible. Sixty
  lines of `asyncio` that this repo owns gives a cut that lands on a known byte boundary;
  a sidecar reached over HTTP does not.

What it costs is stated plainly: this severs a **TCP** path, not a network. It cannot drop
individual packets, reorder them, or corrupt them, so CH-6 (packet loss) could not be built
on it. CH-6 is deferred (`PLAN.md` §21) and CH-4 does not need it.

**The two cut modes are not interchangeable, and the difference is the point.**

`RESET` closes established connections and refuses new ones: what a client sees when the
process on the far end is gone. It surfaces in milliseconds.

`BLACKHOLE` **freezes** established connections rather than closing them, and accepts new
ones without ever answering: what a client sees when a *network* is partitioned. Nothing
surfaces until a timeout fires, and it is the mode that finds code which awaits on the hot
path. CH-4 uses `BLACKHOLE` for exactly that reason — `RESET` would let a bug that blocks
the event loop pass, because the block would be over before anything could measure it.

**The freeze is the whole reason this class has a gate rather than just a flag.** The first
draft closed established connections in both modes, and a probe against real Postgres
showed `connections_blackholed == 0` after a cut: SQLAlchemy's pool handed back its dropped
connection, asyncpg raised `ConnectionDoesNotExistError` in **1 ms**, and no new connection
was ever opened to reach the black hole. The mode was inert and the test would have passed
without ever exercising a partition. A partition does not send a FIN — it stops delivering.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum, unique
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

__all__ = ["FaultMode", "FaultProxy"]

#: Copy buffer. Postgres wire messages are small; this only bounds one read.
_CHUNK = 65536


@unique
class FaultMode(StrEnum):
    """What the proxy does with traffic right now."""

    # S105 reads the name `PASS` as a password. It is the *pass-through* mode.
    PASS = "pass"  # noqa: S105
    RESET = "reset"
    BLACKHOLE = "blackhole"


class FaultProxy:
    """A TCP proxy on 127.0.0.1 that can be cut and healed mid-test.

    Bind it in front of Postgres, hand the client the proxy's address, and the test owns
    the exact moment the ledger becomes unreachable.
    """

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        """Point the proxy at an upstream. Nothing listens until `start()`."""
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.mode = FaultMode.PASS
        self.connections_accepted = 0
        self.connections_forwarded = 0
        self.connections_refused = 0
        self.connections_blackholed = 0
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        self._live: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        #: Connections accepted *while* black-holed. They were never joined to an upstream,
        #: so healing cannot rescue them — `heal()` drops them instead. See its docstring.
        self._blackholed: set[asyncio.StreamWriter] = set()
        #: Set while traffic may flow. `_pump` waits on it before every write, so clearing
        #: it freezes established connections without closing them — see the module note.
        #: Created lazily in `start()`: an `asyncio.Event` built outside a running loop is
        #: fine on 3.12, but the proxy has no reason to exist before one is running.
        self._gate: asyncio.Event | None = None

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> FaultProxy:
        """Listen on an ephemeral port and return self."""
        self._gate = asyncio.Event()
        self._gate.set()
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        sockets = self._server.sockets
        assert sockets, "asyncio.start_server returned a server with no socket"
        self._port = int(sockets[0].getsockname()[1])
        return self

    async def aclose(self) -> None:
        """Stop listening and drop every connection in flight.

        **Order matters, and the obvious order deadlocks.** Since Python 3.12,
        `Server.wait_closed()` waits for every connection handler to finish, so closing the
        server first and awaiting it hangs for as long as any pump is still copying — which,
        after a black hole, is forever. Measured: a CH-4 teardown that never returned.

        So the connections go first: sockets closed, handlers cancelled and collected, and
        only then the listener. `wait_closed()` is bounded even so — a proxy that will not
        shut down must fail its scenario, not wedge the session that was running it.
        """
        self._drop_established()
        for task in list(self._live):
            task.cancel()
        if self._live:
            await asyncio.gather(*self._live, return_exceptions=True)
            self._live.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception, TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            self._server = None

    async def __aenter__(self) -> FaultProxy:
        """Start listening."""
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop listening."""
        await self.aclose()

    # -- address ------------------------------------------------------------------------

    @property
    def port(self) -> int:
        """The listening port.

        Raises:
            RuntimeError: The proxy has not been started.
        """
        if self._port is None:
            raise RuntimeError("the proxy is not listening; call start() first")
        return self._port

    def rewrite(self, url: str) -> str:
        """Return `url` with its host and port replaced by this proxy's.

        Takes the whole DSN rather than a host/port pair so a caller cannot accidentally
        keep half of the original address — which is how a "partition" test ends up
        talking straight to the database and passing for the wrong reason.
        """
        scheme, _, rest = url.partition("://")
        credentials, _, hostpart = rest.rpartition("@")
        _, _, tail = hostpart.partition("/")
        prefix = f"{credentials}@" if credentials else ""
        return f"{scheme}://{prefix}127.0.0.1:{self.port}/{tail}"

    # -- faults -------------------------------------------------------------------------

    def cut(self, mode: FaultMode = FaultMode.BLACKHOLE) -> None:
        """Sever the path, in the manner `mode` names.

        `RESET` drops established connections as well as refusing new ones: a pooled
        connection opened before the cut would otherwise keep working, and the fault would
        only exist for code that happened to reconnect.

        `BLACKHOLE` deliberately does **not** drop them. It stops the pumps and leaves the
        sockets open, because closing them is what made the first draft of this class
        untestable — see the module docstring.

        Raises:
            ValueError: `mode` is `PASS`, which is `heal()`'s job, not a fault.
        """
        if mode is FaultMode.PASS:
            raise ValueError("cut() needs a fault mode; use heal() to restore PASS")
        self.mode = mode
        if mode is FaultMode.BLACKHOLE:
            self._gate_ref.clear()
        else:
            self._drop_established()

    def heal(self) -> None:
        """Restore forwarding, unfreeze what was mid-flight, and drop what cannot be saved.

        Two populations, and they need opposite treatment.

        A connection **established before** the cut is joined to a real upstream socket and
        merely stalled: setting the gate resumes its pumps and the held bytes flow. That is
        the faithful model of a partition ending.

        A connection accepted **during** the cut has no upstream at all — the black-hole
        branch never dialled one. Setting the gate does nothing for it and it would wait
        forever. Dropping it is both the only option and the honest one: in a real
        partition that client's SYN never arrived, so its connect attempt was always
        doomed, and it finds that out when the path comes back.

        Found the hard way. Without this, a top-up that started during CH-4's partition
        never returned, `LeasePool` kept `topping_up` set — its single-flight guard is
        cleared in a `finally` that never ran — and the PEP could not top up again even
        after the network came back. The scenario hung instead of recovering, which is a
        bug in the fault injector wearing the costume of a product defect.
        """
        self.mode = FaultMode.PASS
        self._gate_ref.set()
        for writer in list(self._blackholed):
            with contextlib.suppress(Exception):
                writer.close()
        self._blackholed.clear()

    @property
    def _gate_ref(self) -> asyncio.Event:
        if self._gate is None:
            raise RuntimeError("the proxy is not listening; call start() first")
        return self._gate

    def _drop_established(self) -> None:
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
        self._writers.clear()
        self._blackholed.clear()

    # -- plumbing -----------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Registered here rather than in the forwarding branch alone: `aclose()` cancels
        # everything in `_live`, and a black-holed handler parked in `reader.read()` is
        # exactly the one that keeps `Server.wait_closed()` from ever returning.
        task = asyncio.current_task()
        if task is not None:
            self._live.add(task)
            task.add_done_callback(self._live.discard)
        try:
            await self._serve(reader, writer)
        except asyncio.CancelledError:
            return

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections_accepted += 1
        mode = self.mode

        if mode is FaultMode.RESET:
            self.connections_refused += 1
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        if mode is FaultMode.BLACKHOLE:
            # Accepted, never answered. The client's timeout is the only thing that ends
            # this, which is the whole point of the mode.
            self.connections_blackholed += 1
            self._writers.add(writer)
            self._blackholed.add(writer)
            try:
                await reader.read()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                self._writers.discard(writer)
                self._blackholed.discard(writer)
                with contextlib.suppress(Exception):
                    writer.close()
            return

        try:
            up_reader, up_writer = await asyncio.open_connection(
                self.upstream_host, self.upstream_port
            )
        except OSError:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        self.connections_forwarded += 1
        self._writers.add(writer)
        self._writers.add(up_writer)
        try:
            await asyncio.gather(
                self._pump(reader, up_writer, self._gate_ref),
                self._pump(up_reader, writer, self._gate_ref),
                return_exceptions=True,
            )
        finally:
            self._writers.discard(writer)
            self._writers.discard(up_writer)
            for side in (writer, up_writer):
                with contextlib.suppress(Exception):
                    side.close()

    @staticmethod
    async def _pump(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter, gate: asyncio.Event
    ) -> None:
        """Copy one direction, stalling on `gate` so a black hole freezes rather than closes."""
        try:
            while True:
                chunk = await reader.read(_CHUNK)
                if not chunk:
                    return
                # Awaited *after* the read and *before* the write, so a cut can land in the
                # middle of a message and the bytes are held rather than dropped.
                await gate.wait()
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError, RuntimeError):
            return
