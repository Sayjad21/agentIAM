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
import subprocess
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
_RESULTS: Final = _REPO_ROOT / "docs" / "benchmarks" / "nfr2-load.json"

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
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
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
        tools = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(_REPO_ROOT / "scripts" / "serve_tools.py"),
                "--port",
                str(tools_port),
            ],
            cwd=str(_REPO_ROOT),
        )
        passthrough = subprocess.Popen(  # noqa: S603
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
        server = subprocess.Popen(  # noqa: S603
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
            results = _measure(profile, args.profiles, args.seconds)
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


def _measure(profile: dict[str, Any], rates: list[int], seconds: float) -> dict[str, Any]:
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
        print("  tier 1 — the stub upstream alone")
        baseline = drive(f"{upstream}/invoices/inv_001", token=None, rps=rps, seconds=seconds)
        print(
            f"    achieved {baseline.achieved_rps} RPS  p50 {baseline.percentile(50)} ms  "
            f"p99 {baseline.percentile(99)} ms"
        )

        print("  tier 2 — through the PEP, no pipeline attached")
        transport = drive(
            f"{passthrough}/proxy/invoices/inv_001", token=None, rps=rps, seconds=seconds
        )
        print(
            f"    achieved {transport.achieved_rps} RPS  p50 {transport.percentile(50)} ms  "
            f"p99 {transport.percentile(99)} ms"
        )

        print("  tier 3 — through the enforcing PEP")
        sample = drive(f"{base_url}/proxy/invoices/inv_001", token=token, rps=rps, seconds=seconds)
        print(
            f"    achieved {sample.achieved_rps} RPS  p50 {sample.percentile(50)} ms  "
            f"p99 {sample.percentile(99)} ms"
        )

        total_p99 = round(sample.percentile(99) - baseline.percentile(99), 3)
        total_p50 = round(sample.percentile(50) - baseline.percentile(50), 3)
        enforce_p99 = round(sample.percentile(99) - transport.percentile(99), 3)
        enforce_p50 = round(sample.percentile(50) - transport.percentile(50), 3)
        hop_p50 = round(transport.percentile(50) - baseline.percentile(50), 3)
        hop_p99 = round(transport.percentile(99) - baseline.percentile(99), 3)
        print(
            f"    total overhead p50 {total_p50} / p99 {total_p99} ms  "
            f"= hop {hop_p50}/{hop_p99} + enforcement {enforce_p50}/{enforce_p99}"
        )

        # A row whose achieved rate is far below target is measuring a queue, not a server.
        offered = min(baseline.achieved_rps, transport.achieved_rps, sample.achieved_rps)
        saturated = offered < rps * 0.9

        profiles.append(
            {
                "target_rps": rps,
                "saturated": saturated,
                "tiers": {
                    "upstream_only": baseline.as_dict(),
                    "proxy_no_enforcement": transport.as_dict(),
                    "enforcing_pep": sample.as_dict(),
                },
                "proxy_hop_ms": {"p50": hop_p50, "p99": hop_p99},
                "enforcement_overhead_ms": {"p50": enforce_p50, "p99": enforce_p99},
                "total_overhead_ms": {"p50": total_p50, "p99": total_p99},
                "meets_nfr2_total": total_p99 < OVERHEAD_BUDGET_MS and not saturated,
                "meets_nfr2_enforcement": enforce_p99 < OVERHEAD_BUDGET_MS and not saturated,
            }
        )

    results["profiles"] = profiles
    return results


if __name__ == "__main__":
    sys.exit(main())
