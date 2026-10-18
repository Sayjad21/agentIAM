"""Drive NFR-2's load profiles end to end and write the numbers — T-053, `PLAN.md` §13.1.

Brings up everything the measurement needs, measures, tears it down, and commits the
result to `docs/benchmarks/`. One command, no prerequisites beyond Docker, because a
benchmark a judge cannot re-run is a claim rather than a measurement.

**It starts its own Postgres**, and that is not tidiness. The development host runs a
*native* Windows PostgreSQL alongside Docker's, both bound to 5432, and every connection
from Windows — `localhost`, `127.0.0.1` and `[::1]` alike — reaches the native one. So
`DATABASE_URL=...@localhost:5432/...` silently addresses a different database from the one
`make up` started, and the first symptom is an authentication error that looks like bad
credentials. An ephemeral port sidesteps the whole question and makes the run reproducible
on a machine that has no such collision.

**NFR-1 and NFR-2 are different numbers and are never added.** NFR-1 is the in-process
decision (`tests/perf/test_pipeline_breakdown.py`, ~145 µs median against a 1 ms budget).
NFR-2 is what this measures: *proxy overhead*, the wall-clock cost of a request through the
PEP minus what the same request costs against the upstream alone. Subtracting the baseline
is the whole point — a raw latency figure through the proxy includes the upstream's own
service time and Python's HTTP stack twice, neither of which is the PEP's overhead.
`PLAN.md` §1.5 is blunt about conflating the two, so the baseline is measured in the same
run, against the same stub, over the same transport.

**Two machines (APICTA-08).** On one host the generator, three uvicorn processes and Postgres
share a CPU, and the committed 100 RPS enforcement p99 ranged 1.75-74.7 ms across three runs.
So the run splits in two:

* ``--role server`` brings up Postgres, the stub, the passthrough PEP and the enforcing PEP on
  this machine, bound to every interface, writes the bearer token to a git-ignored file, prints
  the exact client command, and serves until interrupted.
* ``--role client --pep-url ... --passthrough-url ... --upstream-url ... --token-file ...``
  runs the same three tiers from another machine over the link between them, and writes
  ``nfr2-two-host.json`` — never the single-host file.

No ``--role`` is today's single-host run, unchanged. Every result records its topology, the
harness it was measured with and the host that measured it, because a latency figure without
the machine it came from cannot be compared with anything.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import signal
import socket
import statistics
import subprocess  # nosec B404
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_BENCH: Final = _REPO_ROOT / "docs" / "benchmarks"
_RESULTS: Final = _BENCH / "nfr2-load.json"
_RESULTS_TWO_HOST: Final = _BENCH / "nfr2-two-host.json"

#: Where `--role server` leaves the bearer token. Matches `.gitignore`'s
#: `docs/benchmarks/.perf-profile-*`: a live token must never be committed.
SERVER_TOKEN_FILE: Final = _BENCH / ".perf-profile-two-host-token"

#: Recorded on every result. ADR-062 made this harness compose the PEP exactly as the
#: deployment does; `performance.md` drops its "these figures predate" note only for data that
#: carries this.
HARNESS: Final = "ADR-062"

#: `PLAN.md` T-053, reduced scope: two profiles, not three. 1000 RPS is deferred (§21).
PROFILES: Final[tuple[int, ...]] = (100, 500)

#: NFR-2's bar.
OVERHEAD_BUDGET_MS: Final = 8.0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without starting Docker."""
    parser = argparse.ArgumentParser(
        prog="run_load_test", description="Measure NFR-2 (PEP proxy overhead) under load."
    )
    parser.add_argument(
        "--seconds", type=float, default=20.0, help="How long to hold each profile."
    )
    parser.add_argument(
        "--profiles",
        "--rps",
        dest="profiles",
        type=int,
        nargs="+",
        default=list(PROFILES),
        help="Target request rates. PLAN.md T-053 names 100 and 500.",
    )
    parser.add_argument(
        "--role",
        choices=("server", "client"),
        default=None,
        help="Split the run across two machines; omitted, everything runs here.",
    )
    parser.add_argument(
        "--bind",
        default=None,
        help="Server: the interface to listen on. Default 0.0.0.0 for --role server, "
        "127.0.0.1 otherwise.",
    )
    parser.add_argument(
        "--advertise",
        default=None,
        help="Server: the address the client should dial. Default: this host's primary IPv4.",
    )
    parser.add_argument(
        "--tools-workers", type=int, default=1, help="uvicorn workers for the stub upstream."
    )
    client = parser.add_argument_group("client (--role client)")
    client.add_argument("--pep-url", default=None, help="The enforcing PEP, tier 3.")
    client.add_argument("--passthrough-url", default=None, help="The PEP with no pipeline, tier 2.")
    client.add_argument("--upstream-url", default=None, help="The stub alone, tier 1.")
    client.add_argument("--token-file", type=Path, default=None, help="The server's token file.")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port.")
    parser.add_argument(
        "--flamegraph",
        type=Path,
        default=_BENCH / "flamegraph.svg",
        help=(
            "Where to write the flame graph sampled from the enforcing PEP during the "
            "first (unsaturated) profile. Pass an empty string to skip profiling."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help=(
            "Runs per profile. PLAN.md §13.1 asks for at least three with the variance "
            "reported, and it is not ceremony here: p99 enforcement overhead has been "
            "observed between 5.7 and 56 ms across runs on this host."
        ),
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="Default: nfr2-load.json, or nfr2-two-host.json for --role client.",
    )
    return parser


_CLIENT_FLAGS: Final = ("pep_url", "passthrough_url", "upstream_url", "token_file")


def validate(args: argparse.Namespace) -> None:
    """Refuse a flag combination that would measure something other than what it says.

    Raises:
        SystemExit: Through `argparse`, with the reason.
    """
    parser = build_parser()
    given = [flag for flag in _CLIENT_FLAGS if getattr(args, flag) is not None]
    if args.role == "client":
        missing = [flag for flag in _CLIENT_FLAGS if getattr(args, flag) is None]
        if missing:
            parser.error(
                "--role client needs " + ", ".join("--" + m.replace("_", "-") for m in missing)
            )
    elif given:
        parser.error(
            ", ".join("--" + g.replace("_", "-") for g in given) + " only apply to --role client"
        )
    if args.bind is None:
        args.bind = "0.0.0.0" if args.role == "server" else "127.0.0.1"  # noqa: S104  # nosec B104


def results_path(args: argparse.Namespace) -> Path:
    """Where this run writes. A two-machine run never overwrites the single-host figures."""
    if args.results is not None:
        return Path(args.results)
    return _RESULTS_TWO_HOST if args.role == "client" else _RESULTS


def client_profile(args: argparse.Namespace) -> dict[str, str]:
    """The three tiers' addresses and the token, from the client's flags.

    Raises:
        SystemExit: The token file is empty.
    """
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        build_parser().error(f"{args.token_file} holds no token")
    return {
        "base_url": str(args.pep_url).rstrip("/"),
        "passthrough_url": str(args.passthrough_url).rstrip("/"),
        "upstream_url": str(args.upstream_url).rstrip("/"),
        "token": token,
    }


def client_command(
    *, advertise: str, pep_port: int, passthrough_port: int, tools_port: int, token_file: Path
) -> str:
    """The command `--role server` prints for the other machine to run."""
    return (
        "uv run python scripts/run_load_test.py --role client"
        f" --pep-url http://{advertise}:{pep_port}"
        f" --passthrough-url http://{advertise}:{passthrough_port}"
        f" --upstream-url http://{advertise}:{tools_port}"
        f" --token-file {token_file}"
    )


def primary_address() -> str:
    """This host's primary IPv4 — the address another machine on the LAN would dial.

    A UDP `connect` picks the outbound interface without sending a packet; 192.0.2.1 is
    TEST-NET-1 (RFC 5737), so nothing is ever addressed to a real host. Falls back to loopback
    on a machine with no route at all.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            return str(probe.getsockname()[0])
        except OSError:
            return "127.0.0.1"


def _host() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for(
    url: str, *, timeout: float = 90.0, process: subprocess.Popen[bytes] | None = None
) -> None:
    """Block until `url` answers, or give up.

    Watches `process` as well as the clock: a server that died on startup will never
    answer, and waiting the full timeout to discover that hides the traceback it already
    printed behind ninety seconds of silence.

    Raises:
        RuntimeError: The server exited, or never became ready.
    """
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"{url}: the server exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310  # nosec B310
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.25)
    raise RuntimeError(f"{url} never became ready within {timeout} s")


def main(argv: Sequence[str] | None = None) -> int:
    """Run every profile and write the results. Returns the process exit code."""
    # Line-buffered even when redirected: `--role server > log` otherwise held the client
    # command it prints in a block buffer until the server exited — measured, five minutes
    # of an empty log while the servers were up and waiting.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    validate(args)
    results_file = results_path(args)

    if args.role == "client":
        profile = client_profile(args)
        results = _measure(
            profile, args.profiles, args.seconds, repeat=args.repeat, topology="two-host"
        )
        results["server_url"] = profile["base_url"]
        return _write(results, results_file)

    with _bring_up(args) as (profile, server_pid):
        if args.role == "server":
            return _serve(args, profile)
        results = _measure(
            profile,
            args.profiles,
            args.seconds,
            flamegraph=args.flamegraph,
            server_pid=server_pid,
            repeat=args.repeat,
            topology="single-host",
        )
    return _write(results, results_file)


def _write(results: dict[str, Any], path: Path) -> int:
    from scripts.generate_benchmark_results import nfr2_table

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("\n" + "\n".join(nfr2_table(results)))
    print(f"wrote {path}")
    return 0


def _serve(args: argparse.Namespace, profile: dict[str, Any]) -> int:
    """Hand the client what it needs, then hold the servers up until interrupted."""
    advertise = args.advertise or primary_address()
    SERVER_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SERVER_TOKEN_FILE.write_text(profile["token"] + "\n", encoding="utf-8")
    print(f"\nserving on {args.bind}; advertised as {advertise}")
    print(f"token written to {SERVER_TOKEN_FILE} — copy it to the client machine")
    print("on the client, run:\n")
    print(
        "  "
        + client_command(
            advertise=advertise,
            pep_port=profile["ports"]["pep"],
            passthrough_port=profile["ports"]["passthrough"],
            tools_port=profile["ports"]["tools"],
            token_file=SERVER_TOKEN_FILE,
        )
    )
    print("\nCtrl-C here when it has finished.")
    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    try:
        while not stop:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        SERVER_TOKEN_FILE.unlink(missing_ok=True)
    return 0


@contextlib.contextmanager
def _bring_up(args: argparse.Namespace) -> Iterator[tuple[dict[str, Any], int]]:
    """Postgres, the stub, the passthrough PEP and the enforcing PEP, torn down on exit.

    Yields the profile the load generator reads — every tier's address and the token — and
    the enforcing PEP's pid, for the flame graph.
    """
    from alembic import command
    from alembic.config import Config
    from testcontainers.community.postgres import PostgresContainer

    package_root = _REPO_ROOT / "packages" / "agentiam-controlplane"
    port = args.port or _free_port()
    bind = args.bind

    with PostgresContainer(
        image="postgres:16-alpine",
        driver="asyncpg",
        username="agentiam",
        password="agentiam",  # noqa: S106 - throwaway, lives for one run
        dbname="agentiam",
    ) as container:
        database_url = container.get_connection_url()
        cfg = Config(str(package_root / "alembic.ini"))
        cfg.set_main_option(
            "script_location",
            str(package_root / "src" / "agentiam_controlplane" / "db" / "migrations"),
        )
        cfg.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(cfg, "head")
        print(f"database ready on an ephemeral port; PEP will serve on :{port}")

        tools_port = _free_port()
        passthrough_port = _free_port()
        profile_path = (
            _REPO_ROOT / "docs" / "benchmarks" / f".perf-profile-{uuid.uuid4().hex[:8]}.json"
        )
        # The PEPs reach the stub over this machine's loopback whatever `bind` is: in a
        # two-machine run only the *client's* hops cross the link, as they would in production.
        tools = subprocess.Popen(  # noqa: S603  # nosec B603
            [
                sys.executable,
                str(_REPO_ROOT / "scripts" / "serve_tools.py"),
                "--host",
                bind,
                "--port",
                str(tools_port),
                "--workers",
                str(args.tools_workers),
            ],
            cwd=str(_REPO_ROOT),
        )
        passthrough = subprocess.Popen(  # noqa: S603  # nosec B603
            [
                sys.executable,
                str(_REPO_ROOT / "scripts" / "serve_pep.py"),
                "--no-enforce",
                "--host",
                bind,
                "--port",
                str(passthrough_port),
                "--database-url",
                database_url,
                "--profile",
                str(profile_path) + ".passthrough",
                "--upstream",
                f"http://127.0.0.1:{tools_port}",
            ],
            cwd=str(_REPO_ROOT),
        )
        server = subprocess.Popen(  # noqa: S603  # nosec B603
            [
                sys.executable,
                str(_REPO_ROOT / "scripts" / "serve_pep.py"),
                "--seed",
                "--host",
                bind,
                "--port",
                str(port),
                "--database-url",
                database_url,
                "--profile",
                str(profile_path),
                "--upstream",
                f"http://127.0.0.1:{tools_port}",
            ],
            cwd=str(_REPO_ROOT),
        )
        try:
            _wait_for(f"http://127.0.0.1:{tools_port}/invoices/inv_001", process=tools)
            _wait_for(f"http://127.0.0.1:{passthrough_port}/healthz", process=passthrough)
            _wait_for(f"http://127.0.0.1:{port}/healthz", process=server)
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["base_url"] = f"http://127.0.0.1:{port}"
            profile["upstream_url"] = f"http://127.0.0.1:{tools_port}"
            profile["passthrough_url"] = f"http://127.0.0.1:{passthrough_port}"
            profile["ports"] = {"pep": port, "passthrough": passthrough_port, "tools": tools_port}
            yield profile, server.pid
        finally:
            for process in (server, passthrough, tools):
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
                    process.kill()
            profile_path.unlink(missing_ok=True)
            Path(str(profile_path) + ".passthrough").unlink(missing_ok=True)


def _start_pyspy(pid: int, output: Path, seconds: float) -> subprocess.Popen[bytes] | None:
    """Sample the PEP while it is under load, writing a flame graph.

    **Attached by PID, never spawned.** `py-spy record -- <command>` fails on this host
    with *"Failed to find python version from target process"*; attaching to an
    already-running interpreter works and produced 222 samples with 0 errors in a probe.
    So the server is started first and sampled second, which is also the only ordering
    that profiles it *under load* rather than during startup.
    """
    binary = _pyspy_binary()
    if binary is None:  # pragma: no cover - only where py-spy is not installed
        print("  (flame graph skipped: py-spy not found; `uv add --dev py-spy`)")
        return None

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.Popen(  # noqa: S603  # nosec B603
            [
                str(binary),
                "record",
                "--pid",
                str(pid),
                # The venv's `python.exe` is a launcher: `Popen.pid` is the shim, and
                # reading a Python version out of *that* fails with "Failed to find python
                # version from target process". Following children lands on the interpreter
                # actually running the server. Attaching to the same server by its
                # interpreter PID by hand works, which is what isolated this.
                "--subprocesses",
                "--duration",
                str(int(seconds)),
                "--format",
                "flamegraph",
                "--output",
                str(output),
            ],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:  # pragma: no cover - a missing profiler is not a failed benchmark
        print(f"  (flame graph skipped: {exc})")
        return None


def _pyspy_binary() -> Path | None:
    """Locate `py-spy`, which is an executable rather than an importable module.

    `python -m py_spy` does not work: the wheel ships a Rust binary and no Python package,
    so the module import fails and the subprocess dies with a traceback nobody reads. The
    interpreter's own `Scripts`/`bin` directory is checked before `PATH` so a run inside
    the project's virtualenv profiles with that virtualenv's copy.
    """
    import shutil

    for candidate in (
        Path(sys.executable).parent / "py-spy.exe",
        Path(sys.executable).parent / "py-spy",
    ):
        if candidate.exists():
            return candidate
    found = shutil.which("py-spy")
    return Path(found) if found else None


def _spread(values: list[float]) -> dict[str, float]:
    """Min / median / max across runs — `PLAN.md` §13.1's "variance reported".

    Not a standard deviation: with three runs it would be a number with no meaning, and
    §13.1 bans averages from latency tables in any case. The range is what a reader needs
    to know whether one quoted figure was lucky.
    """
    ordered = sorted(values)
    return {
        "min": round(ordered[0], 3),
        "median": round(statistics.median(ordered), 3),
        "max": round(ordered[-1], 3),
    }


def _measure(
    profile: dict[str, Any],
    rates: list[int],
    seconds: float,
    *,
    flamegraph: Path | None = None,
    server_pid: int | None = None,
    repeat: int = 1,
    topology: str = "single-host",
) -> dict[str, Any]:
    """Run the baseline and every profile, and return the whole result."""
    from tests.perf.driver import drive  # local import: needs the repo on sys.path

    base_url = profile["base_url"]
    token = profile["token"]
    upstream = profile["upstream_url"]
    passthrough = profile["passthrough_url"]

    results: dict[str, Any] = {
        "measured_at": datetime.now(UTC).isoformat(),
        "topology": topology,
        "harness": HARNESS,
        "host": _host(),
        "nfr2_budget_ms": OVERHEAD_BUDGET_MS,
        "note": (
            "Three tiers measured at *every* rate, over the same loopback transport, so "
            "each subtraction compares like with like. (1) the stub upstream alone; "
            "(2) the same request through the PEP in T-018 transport mode with no pipeline "
            "attached; (3) through the enforcing PEP. (2)-(1) is what a proxy hop costs on "
            "this host and is not AgentIAM's doing; (3)-(2) is what authorization costs and "
            "is the number T-053 is actually about. Measuring the tiers at one fixed rate "
            "and the profiles at another produced negative overheads on the first run — the "
            "baseline was saturated and the profile was not. Latency is `service_ms` (send "
            "to response); `scheduled_ms` is the coordinated-omission-aware figure and "
            "`generator_lag_ms` says how far the generator itself fell behind, quoted so a "
            "reader can judge whether the run measured the server or the harness. "
            "`achieved_rps` matters as much as any latency here: where it falls far short "
            "of the target, the host could not offer the load and every latency in that row "
            "is a queueing artefact. None of this is NFR-1, the in-process decision, which "
            "is measured separately in tests/perf/test_pipeline_breakdown.py."
        ),
        "profiles": [],
    }

    if flamegraph and server_pid:
        # A pass of its own, before anything is counted. py-spy samples the PEP it attaches
        # to, and that costs latency: the profiled run's 100 RPS enforcement p99 was 14.2 ms
        # where the two unprofiled runs gave 7.0 and 7.7 ms. At the first rate, which is the
        # unsaturated one: a flame graph of a queue is a picture of waiting, not of work.
        print(f"\n=== flame graph: {rates[0]} RPS for {seconds:.0f} s, not counted ===")
        spy = _start_pyspy(server_pid, flamegraph, seconds)
        drive(f"{base_url}/proxy/invoices/inv_001", token=token, rps=rates[0], seconds=seconds)
        if spy is not None:
            code = spy.wait(timeout=120)
            if code == 0 and flamegraph.exists():
                print(f"  flame graph written to {flamegraph}")
                with contextlib.suppress(ValueError):
                    results["flamegraph"] = str(flamegraph.relative_to(_REPO_ROOT))
            else:
                # Loudly, and with the profiler's own words: a silently absent flame graph is
                # an acceptance criterion that quietly went missing.
                said = spy.stdout.read().decode(errors="replace").strip() if spy.stdout else ""
                print(f"  !! flame graph FAILED (py-spy exit {code}): {said}")

    profiles: list[dict[str, Any]] = []
    for rps in rates:
        print(f"\n=== {rps} RPS ===")
        runs: list[dict[str, Any]] = []
        last: dict[str, Any] = {}

        for attempt in range(1, repeat + 1):
            print(f"  run {attempt}/{repeat}")
            print("    tier 1 — the stub upstream alone")
            baseline = drive(f"{upstream}/invoices/inv_001", token=None, rps=rps, seconds=seconds)
            print(
                f"      achieved {baseline.achieved_rps} RPS  p50 {baseline.percentile(50)} ms  "
                f"p99 {baseline.percentile(99)} ms"
            )

            print("    tier 2 — through the PEP, no pipeline attached")
            transport = drive(
                f"{passthrough}/proxy/invoices/inv_001", token=None, rps=rps, seconds=seconds
            )
            print(
                f"      achieved {transport.achieved_rps} RPS  p50 {transport.percentile(50)} ms "
                f" p99 {transport.percentile(99)} ms"
            )

            print("    tier 3 — through the enforcing PEP")
            sample = drive(
                f"{base_url}/proxy/invoices/inv_001", token=token, rps=rps, seconds=seconds
            )
            print(
                f"      achieved {sample.achieved_rps} RPS  p50 {sample.percentile(50)} ms  "
                f"p99 {sample.percentile(99)} ms"
            )

            total_p99 = round(sample.percentile(99) - baseline.percentile(99), 3)
            total_p50 = round(sample.percentile(50) - baseline.percentile(50), 3)
            enforce_p99 = round(sample.percentile(99) - transport.percentile(99), 3)
            enforce_p50 = round(sample.percentile(50) - transport.percentile(50), 3)
            hop_p50 = round(transport.percentile(50) - baseline.percentile(50), 3)
            hop_p99 = round(transport.percentile(99) - baseline.percentile(99), 3)
            print(
                f"      total overhead p50 {total_p50} / p99 {total_p99} ms  "
                f"= hop {hop_p50}/{hop_p99} + enforcement {enforce_p50}/{enforce_p99}"
            )

            offered = min(baseline.achieved_rps, transport.achieved_rps, sample.achieved_rps)
            last = {
                "run": attempt,
                "saturated": offered < rps * 0.9,
                "tiers": {
                    "upstream_only": baseline.as_dict(),
                    "proxy_no_enforcement": transport.as_dict(),
                    "enforcing_pep": sample.as_dict(),
                },
                "proxy_hop_ms": {"p50": hop_p50, "p99": hop_p99},
                "enforcement_overhead_ms": {"p50": enforce_p50, "p99": enforce_p99},
                "total_overhead_ms": {"p50": total_p50, "p99": total_p99},
            }
            runs.append(last)

        # §13.1 asks for the variance, not a representative run — and on this host the
        # spread is the most informative thing in the table.
        spread = {
            key: _spread([run[key][stat] for run in runs])
            for key, stat in (
                ("enforcement_overhead_ms", "p99"),
                ("proxy_hop_ms", "p99"),
                ("total_overhead_ms", "p99"),
            )
        }
        spread["enforcement_overhead_p50_ms"] = _spread(
            [run["enforcement_overhead_ms"]["p50"] for run in runs]
        )
        saturated = any(run["saturated"] for run in runs)
        worst_enforce = max(run["enforcement_overhead_ms"]["p99"] for run in runs)
        total_worst = max(run["total_overhead_ms"]["p99"] for run in runs)
        print(
            f"  across {repeat} run(s): enforcement p99 "
            f"{spread['enforcement_overhead_ms']['min']}-"
            f"{spread['enforcement_overhead_ms']['max']} ms"
        )

        profiles.append(
            {
                "target_rps": rps,
                "runs": repeat,
                "saturated": saturated,
                "spread_ms": spread,
                "all_runs": runs,
                "tiers": last["tiers"],
                "proxy_hop_ms": last["proxy_hop_ms"],
                "enforcement_overhead_ms": last["enforcement_overhead_ms"],
                "total_overhead_ms": last["total_overhead_ms"],
                "meets_nfr2_total": total_worst < OVERHEAD_BUDGET_MS and not saturated,
                "meets_nfr2_enforcement": worst_enforce < OVERHEAD_BUDGET_MS and not saturated,
            }
        )

    results["profiles"] = profiles
    return results


if __name__ == "__main__":
    sys.exit(main())
