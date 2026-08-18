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
"""

from __future__ import annotations

import argparse
import json
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
    from collections.abc import Sequence

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_BENCH: Final = _REPO_ROOT / "docs" / "benchmarks"
_RESULTS: Final = _BENCH / "nfr2-load.json"

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
        type=int,
        nargs="+",
        default=list(PROFILES),
        help="Target request rates. PLAN.md T-053 names 100 and 500.",
    )
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
    parser.add_argument("--results", type=Path, default=_RESULTS)
    return parser


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
    args = build_parser().parse_args(argv)

    from alembic import command
    from alembic.config import Config
    from testcontainers.community.postgres import PostgresContainer

    package_root = _REPO_ROOT / "packages" / "agentiam-controlplane"
    port = args.port or _free_port()

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
        tools = subprocess.Popen(  # noqa: S603  # nosec B603
            [
                sys.executable,
                str(_REPO_ROOT / "scripts" / "serve_tools.py"),
                "--port",
                str(tools_port),
            ],
            cwd=str(_REPO_ROOT),
        )
        passthrough = subprocess.Popen(  # noqa: S603  # nosec B603
            [
                sys.executable,
                str(_REPO_ROOT / "scripts" / "serve_pep.py"),
                "--no-enforce",
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
            profile["upstream_url"] = f"http://127.0.0.1:{tools_port}"
            profile["passthrough_url"] = f"http://127.0.0.1:{passthrough_port}"
            results = _measure(
                profile,
                args.profiles,
                args.seconds,
                flamegraph=args.flamegraph,
                server_pid=server.pid,
                repeat=args.repeat,
            )
        finally:
            for process in (server, passthrough, tools):
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
                    process.kill()
            profile_path.unlink(missing_ok=True)
            Path(str(profile_path) + ".passthrough").unlink(missing_ok=True)

    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {args.results}")
    return 0


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
) -> dict[str, Any]:
    """Run the baseline and every profile, and return the whole result."""
    from tests.perf.driver import drive  # local import: needs the repo on sys.path

    base_url = profile["base_url"]
    token = profile["token"]
    upstream = profile["upstream_url"]
    passthrough = profile["passthrough_url"]

    results: dict[str, Any] = {
        "measured_at": datetime.now(UTC).isoformat(),
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
            # Profiled once, on the first run of the first (unsaturated) profile: the later
            # ones queue, and a flame graph of a queue is a picture of waiting, not of work.
            spy = None
            if flamegraph and server_pid and rps == rates[0] and attempt == 1:
                spy = _start_pyspy(server_pid, flamegraph, seconds)
            sample = drive(
                f"{base_url}/proxy/invoices/inv_001", token=token, rps=rps, seconds=seconds
            )
            if spy is not None and flamegraph is not None:
                code = spy.wait(timeout=120)
                if code == 0 and flamegraph.exists():
                    print(f"      flame graph written to {flamegraph}")
                    results["flamegraph"] = str(flamegraph.relative_to(_REPO_ROOT))
                else:
                    # Loudly, and with the profiler's own words: a silently absent flame
                    # graph is an acceptance criterion that quietly went missing.
                    said = spy.stdout.read().decode(errors="replace").strip() if spy.stdout else ""
                    print(f"      !! flame graph FAILED (py-spy exit {code}): {said}")
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
