"""Serve the stub upstream on its own port — T-053's baseline.

NFR-2 is *proxy overhead*, which is a subtraction: what a request costs through the PEP
minus what the same request costs without it. That second number needs the upstream
reachable on its own, over the same transport, in its own process — otherwise the baseline
and the measurement differ in more ways than the one being measured.

The stub tools (T-004) are deliberately trivial: they exist so that a load test of the PEP
measures the PEP. If the upstream had real work to do, both numbers would grow by the same
amount and the *difference* would still be right, but the ratio a reader takes away from
the table would be meaningless.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from `main` so it can be tested without binding a port."""
    parser = argparse.ArgumentParser(
        prog="serve_tools", description="Serve the M4 stub tools for load-test baselines."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until interrupted. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    import uvicorn

    from agentiam_demo.tools import create_tools_app

    print(f"stub tools on http://{args.host}:{args.port}")
    uvicorn.run(
        create_tools_app(),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
