"""Serve the stub upstream on its own port — T-053's baseline.

NFR-2 is *proxy overhead*, which is a subtraction: what a request costs through the PEP
minus what the same request costs without it. That second number needs the upstream
reachable on its own, over the same transport, in its own process — otherwise the baseline
and the measurement differ in more ways than the one being measured.

The stub tools (T-004) are deliberately trivial: they exist so that a load test of the PEP
measures the PEP. If the upstream had real work to do, both numbers would grow by the same
amount and the *difference* would still be right, but the ratio a reader takes away from
the table would be meaningless.

**`--workers N`** (APICTA-08) runs N uvicorn processes, for a two-machine run where the stub
must keep up with a generator on another host. Measured on the Fedora development host it
changes nothing at the rates NFR-2 names: one worker served 490 RPS of a 500 RPS target at
p50 1.2 ms, and at a 1,000 RPS target one worker and four both stalled near 410-460 RPS with
the *generator's* scheduling lag at 5-7 s p99 — the limit was the single-process load
generator, not this. The August results' 138 RPS came from a run that recorded no host,
which is why `run_load_test.py` now records one.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The app, as uvicorn's `factory` import string: with `workers > 1` every worker process
#: imports and builds its own copy, which an app object cannot be passed for.
APP: Final = "agentiam_demo.tools:create_tools_app"


def _workers(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"--workers must be at least 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without binding a port."""
    parser = argparse.ArgumentParser(
        prog="serve_tools", description="Serve the M4 stub tools for load-test baselines."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--workers", type=_workers, default=1, help="uvicorn worker processes (default 1)."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until interrupted. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    import uvicorn

    print(f"stub tools on http://{args.host}:{args.port} ({args.workers} worker(s))")
    uvicorn.run(
        APP,
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
