"""Fold the comparative benchmark JSON into `docs/benchmarks/comparative.md` — APICTA-08.

Same arrangement as `generate_benchmark_results.py`: `scripts/run_comparative_benchmarks.py`
measures and writes `comparative.json`, this renders it, nobody edits the Markdown, and
`--check` exits non-zero when the committed file is not what the committed JSON renders to.

**What is compared, and against what** (`research/lease-plan.md` GAP-D: every number until now
was AgentIAM against AgentIAM):

1. *Attenuation*: `attenuate()` in process against a **simulated** OAuth2 token exchange — a
   local HTTP endpoint doing one database lookup per call. Simulated, and said so on the page:
   it is the shape of RFC 8693 token exchange, not any vendor's implementation, and every
   choice in its setup favours it.
2. *Budget enforcement*: B1, one `SELECT ... FOR UPDATE` round trip to the ledger per call
   (the central-check shape), against B3, AgentIAM's lease reserve in memory — three
   concurrent PEPs each, one pool, the same Postgres.
3. *Overhead in context*: the PEP's in-process cost against a **measured** median LLM call
   from the configured backend, never the 1,200 ms the plan once assumed.

Every ratio is computed here from the measured numbers. `PLAN.md` §13.1 holds: medians and
percentiles, no averages.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_BENCH: Final = _REPO_ROOT / "docs" / "benchmarks"
_SOURCE: Final = _BENCH / "comparative.json"
_OUTPUT: Final = _BENCH / "comparative.md"

_HEADER: Final = """<!--
  GENERATED FILE — do not edit.

  Regenerate with:  make bench-compare     (measures, needs Docker and an LLM backend)
                    uv run python scripts/generate_comparative_results.py   (render only)
  Source:           docs/benchmarks/comparative.json
  Verify in CI:     uv run python scripts/generate_comparative_results.py --check
-->

# Comparative benchmarks

AgentIAM against the designs it replaces, on one host, in one run. Medians and percentiles
only (`PLAN.md` §13.1). Every ratio below is computed from the measured figures."""


def _ratio(numerator: float, denominator: float) -> str:
    return f"{numerator / denominator:.1f}\u00d7" if denominator else "—"


def _ms(microseconds: float) -> str:
    return f"{microseconds / 1000:.3f}"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return [*lines, ""]


def _attenuation(data: dict[str, Any]) -> list[str]:
    local = data["in_process"]
    exchange = data["token_exchange"]
    return [
        "",
        "## 1. Delegating authority: offline attenuation vs a token exchange",
        "",
        "A parent hands a child a narrower credential. AgentIAM appends a block to the "
        "parent's biscuit in process (`attenuate()`); the alternative asks an authorization "
        "server for a new token.",
        "",
        *_table(
            ["Path", "Calls", "median ms", "p99 ms"],
            [
                [
                    "`attenuate()`, in process",
                    str(local["n"]),
                    _ms(local["median_us"]),
                    _ms(local["p99_us"]),
                ],
                [
                    "simulated OAuth2 token exchange",
                    str(exchange["n"]),
                    _ms(exchange["median_us"]),
                    _ms(exchange["p99_us"]),
                ],
            ],
        ),
        f"The exchange's median is **{_ratio(exchange['median_us'], local['median_us'])}** "
        f"the in-process median, and its p99 "
        f"{_ratio(exchange['p99_us'], local['p99_us'])}.",
        "",
        "**This is a simulated OAuth2 token exchange, and its setup favours it:** an HTTP "
        "endpoint on loopback, one keep-alive connection reused for every call, one "
        "primary-key `SELECT` per call against the same Postgres container, no TLS, and an "
        "issued token that is a random string rather than a signed JWT. A real authorization "
        "server adds TLS, a signature, a network hop and its own policy evaluation. The "
        "in-process side is the real `attenuate()` producing a real child biscuit, which is "
        "verified once to confirm it is narrower and valid.",
        "",
    ]


_DESIGNS: Final = (
    "**B1** checks and debits the ledger inside one row-locking transaction per call "
    "(`FOR UPDATE`) — the central-check shape. **B3** is AgentIAM: each PEP reserves from a "
    "lease in memory, settles through the deployed settlement queue, and the ledger sees only "
    "lease acquisitions, releases and batched commits. Latency is per call, including any "
    "wait for a lease top-up."
)


def _budget_row(label: str, stats: dict[str, Any]) -> list[str]:
    return [
        label,
        str(stats["n"]),
        _ms(stats["median_us"]),
        _ms(stats["p99_us"]),
        str(stats["calls_per_s"]),
        str(stats["ledger_round_trips"]),
        str(stats["refused"]),
    ]


def _budget(data: dict[str, Any]) -> list[str]:
    b1, b3 = data["b1"], data["b3"]
    lines = [
        "",
        "## 2. Enforcing a shared budget: a ledger round trip per call vs leases",
        "",
        f"{data['peps']} PEPs spend from one pool at once, {data['calls_per_pep']} calls of "
        f"৳{data['amount']} each, closed loop. " + _DESIGNS,
        "",
        *_table(
            [
                "Design",
                "Calls allowed",
                "median ms",
                "p99 ms",
                "calls/s",
                "ledger round trips",
                "refused",
            ],
            [_budget_row("B1 — FOR UPDATE per call", b1), _budget_row("B3 — lease reserve", b3)],
        ),
        f"B3's median is {_ratio(b1['median_us'], b3['median_us'])} faster than B1's, and it "
        f"made {b3['ledger_round_trips']} ledger round trips where B1 made "
        f"{b1['ledger_round_trips']}. B3's p99 is where its cost went: the calls that waited "
        f"for a lease top-up ({b3.get('waits', 0)} waits across the run).",
        "",
    ]
    clean = True
    for label, stats in (("B1", b1), ("B3", b3)):
        overspend = Decimal(str(stats["overspend"]))
        if overspend > 0:
            clean = False
            lines += [f"**{label} OVERSPENT the pool by ৳{overspend}.** This run is invalid.", ""]
        unrecorded = _unrecorded(stats)
        if unrecorded > 0:
            clean = False
            lines += [
                f"**{label}'s ledger UNDER-RECORDED ৳{unrecorded} of ৳{stats['spent']} spent** "
                f"(committed ৳{stats['committed']}). Settlements reached the ledger after their "
                f"lease had been released and were rejected, so that money went back to the "
                f"pool as available — spendable twice. `STATUS.md` gap 38; the latency figures "
                f"above stand, the accounting does not.",
                "",
            ]
    if clean:
        lines += [
            "Both designs **never exceeded the pool and recorded every taka spent**: after "
            "each run the ledger's `committed` equalled the sum of allowed calls, and "
            "`committed + leased` stayed within the total. B3's spend is committed "
            "asynchronously by settlement; the comparison is of what each design costs per "
            "authorized call.",
            "",
        ]
    return lines


def _unrecorded(stats: dict[str, Any]) -> Decimal:
    """Spend the ledger never recorded; zero for data from before the field existed."""
    if "spent" not in stats or "committed" not in stats:
        return Decimal(0)
    return Decimal(str(stats["spent"])) - Decimal(str(stats["committed"]))


def _context(data: dict[str, Any]) -> list[str]:
    in_process_ms = data["in_process_us"] / 1000
    llm = data["llm"]
    lines = [
        "",
        "## 3. The PEP's cost beside the call it guards",
        "",
        f"The PEP's in-process cost per request is **{in_process_ms:.3f} ms** — `verify()` + "
        f"`decide()` + extraction, medians from `pb2-breakdown.json` (see `performance.md`).",
        "",
    ]
    if llm.get("status") != "measured":
        return [
            *lines,
            f"The LLM call it would sit beside was **not measured** on this run: "
            f"{llm.get('reason', 'no reason recorded')}. No figure is assumed in its place.",
            "",
        ]
    share = in_process_ms / llm["median_ms"] * 100
    return [
        *lines,
        f"A median LLM call from the configured backend (`{llm['backend']}`, resolved to "
        f"`{llm['model']}`) took **{llm['median_ms']:.0f} ms** over {llm['n']} calls "
        f"(slowest {llm['max_ms']:.0f} ms), each a short structured-output request of the "
        f"shape the demo agent makes, timed end to end including any retry the client made.",
        "",
        f"So the PEP adds **{share:.2f}%** to an agent step that calls the model once and then "
        f"one tool — the demo agent's shape. A step making several tool calls per model call "
        f"pays the PEP's cost once per tool call.",
        "",
    ]


def render(data: dict[str, Any]) -> str:
    """The whole document, from one measurement."""
    host = data["host"]
    lines = [
        _HEADER,
        "",
        f"Measured {data['measured_at']} on `{host['platform']}`, Python {host['python']}, "
        f"{host['cpus']} CPUs.",
        *_attenuation(data["attenuation"]),
        *_budget(data["budget"]),
        *_context(data["context"]),
    ]
    return "\n".join(lines)


def load(path: Path = _SOURCE) -> dict[str, Any]:
    """The committed measurement."""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def main(argv: Sequence[str] | None = None) -> int:
    """Render `comparative.md`, or check it. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="generate_comparative_results",
        description="Regenerate docs/benchmarks/comparative.md from comparative.json.",
    )
    parser.add_argument("--check", action="store_true", help="Exit 1 if the file is stale.")
    parser.add_argument("--source", type=Path, default=_SOURCE)
    parser.add_argument("--output", type=Path, default=_OUTPUT)
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"{args.source} does not exist; run `make bench-compare`", file=sys.stderr)
        return 1
    rendered = render(load(args.source))
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(
                f"{args.output} is stale or hand-edited. Regenerate with:\n"
                f"  uv run python scripts/generate_comparative_results.py",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} matches its source")
        return 0
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
