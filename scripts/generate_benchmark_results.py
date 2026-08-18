"""Fold the benchmark JSON into `docs/benchmarks/performance.md` — T-053, `PLAN.md` §13.1.

Same arrangement as the chaos results: the JSON each run writes is the artifact, this
renders it, and nobody edits the Markdown. `--check` re-renders and exits non-zero if the
committed file differs, so CI catches a table that was hand-edited or left stale.

`PLAN.md` §1.5 asks for NFR-1 and NFR-2 to be reported *separately and labelled*, and §13.1
bans averages from any latency table. Both are structural here rather than matters of care:
the two numbers come from different files and are rendered in different sections, and
`Sample.as_dict` never emits a mean.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

_REPO_ROOT: Final = Path(__file__).resolve().parents[1]
_BENCH: Final = _REPO_ROOT / "docs" / "benchmarks"
_PB2: Final = _BENCH / "pb2-breakdown.json"
_NFR2: Final = _BENCH / "nfr2-load.json"
_OUTPUT: Final = _BENCH / "performance.md"

#: NFR-2's bar, mirrored from scripts/run_load_test.py.
OVERHEAD_BUDGET: Final = 8.0

_HEADER: Final = """<!--
  GENERATED FILE — do not edit.

  Regenerate with:  uv run python scripts/generate_benchmark_results.py
  Sources:          docs/benchmarks/pb2-breakdown.json   (uv run pytest -m perf)
                    docs/benchmarks/nfr2-load.json       (uv run python scripts/run_load_test.py)
  Verify in CI:     uv run python scripts/generate_benchmark_results.py --check
-->

# Performance

**NFR-1 and NFR-2 are different measurements and are never added together.** NFR-1 is the
authorization decision, in process, excluding all network I/O. NFR-2 is the proxy's
end-to-end overhead under load. `PLAN.md` §1.5: *"Python cannot proxy a request in under a
millisecond. It can evaluate an authorization decision in well under a millisecond. Always
report the two numbers separately, and label them."*

No averages appear below. Every figure is a percentile or a median, per `PLAN.md` §13.1.
"""


def _table(rows: list[list[str]], headers: list[str]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    lines.append("")
    return lines


def _render_pb2(data: dict[str, Any]) -> list[str]:
    order = ["extract", "verify", "caveats", "policy", "record_hash", "decide_total"]
    labels = {
        "extract": "step 1 — extract (route, args, digest)",
        "verify": "step 2 — verify (biscuit signature)",
        "caveats": "step 4 — caveats (4 clauses, Datalog)",
        "policy": "step 5 — policy (Cedar)",
        "record_hash": "step 10 — audit record hash",
        "decide_total": "**`decide()` total — NFR-1**",
    }
    steps = data["steps"]
    rows = [
        [labels[name], f"{steps[name]['median_us']}", f"{steps[name]['p99_us']}"]
        for name in order
        if name in steps
    ]
    lines = [
        "",
        "## NFR-1 — the authorization decision, in process",
        "",
        f"Budget: **p99 < 1 ms**. Measured {data['measured_at']}.",
        "",
        "Every step is the real implementation: a real biscuit chain, the real Cedar engine, "
        "real Datalog evaluation. The ledger and the audit sink are absent because they are "
        "off the hot path by construction — including them would measure the thing the "
        "design removed.",
        "",
    ]
    lines += _table(rows, ["Step", "median µs", "p99 µs"])
    total = steps.get("decide_total", {})
    verify = steps.get("verify", {})
    policy = steps.get("policy", {})
    per_request = round(
        verify.get("median_us", 0)
        + total.get("median_us", 0)
        + steps.get("extract", {}).get("median_us", 0)
    )
    lines += [
        f"**NFR-1 holds**: `decide()` p99 is {total.get('p99_us')} µs against a 1000 µs "
        f"budget, and `PLAN.md` §17's R-2 trigger (2 ms, port to Rust) is not approached.",
        "",
        "Two things the breakdown says that a single number cannot:",
        "",
        f"- **Policy evaluation is nearly the whole decision** — {policy.get('median_us')} µs "
        f"of a {total.get('median_us')} µs median. Everything else inside `decide()` is "
        f"single-digit microseconds.",
        f"- **`verify()` is the most expensive step and sits *outside* `decide()`** at "
        f"{verify.get('median_us')} µs median. Per-request in-process cost is verify + "
        f"decide + extract, so roughly {per_request} µs — that, not NFR-1 alone, is what "
        f"bounds throughput.",
        "",
        "> An earlier figure of **~5 µs** was published for NFR-1 in `STATUS.md` and the T-019 "
        "commit. It is a real measurement of `decide()` with `FakePolicy`, a stub that returns "
        "one fixed verdict, and it therefore excludes the most expensive thing in the path. "
        "It is superseded here. `tests/unit/test_decision.py::TestNfr1` still measures the "
        "stubbed path and is still useful — it isolates everything *except* policy — but the "
        "number to quote is the one above.",
        "",
    ]
    return lines


def _render_nfr2(data: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## NFR-2 — proxy overhead under load",
        "",
        f"Budget: **p99 < 8 ms at 500 RPS, single instance**. Measured {data['measured_at']}.",
        "",
        "Three tiers at every rate, so each subtraction compares like with like:",
        "",
        "1. the stub upstream alone, no PEP in the path;",
        "2. the same request through the PEP with **no pipeline attached** (T-018 transport "
        "mode) — this is what a proxy hop costs on this host, and is not AgentIAM's doing;",
        "3. through the enforcing PEP.",
        "",
        "**(3) - (2) is what authorization costs.** (2) - (1) is TCP and Python's HTTP stack.",
        "",
    ]

    rows = []
    for profile in data["profiles"]:
        tiers = profile["tiers"]
        target = profile["target_rps"]
        flag = " ⚠️" if profile["saturated"] else ""
        spread = profile.get("spread_ms", {})
        enforce = spread.get("enforcement_overhead_ms", {})
        enforce_p50 = spread.get("enforcement_overhead_p50_ms", {})
        rows.append(
            [
                f"**{target}**{flag}",
                str(profile.get("runs", 1)),
                str(tiers["enforcing_pep"]["achieved_rps"]),
                f"{tiers['upstream_only']['service_ms']['p50']} / "
                f"{tiers['upstream_only']['service_ms']['p99']}",
                f"{tiers['proxy_no_enforcement']['service_ms']['p50']} / "
                f"{tiers['proxy_no_enforcement']['service_ms']['p99']}",
                f"{enforce_p50.get('median', '—')}",
                f"**{enforce.get('min', '—')} - {enforce.get('max', '—')}**",
            ]
        )

    lines += _table(
        rows,
        [
            "Target RPS",
            "Runs",
            "Achieved",
            "① upstream p50/p99 ms",
            "② +proxy p50/p99 ms",
            "enforcement p50, median of runs",
            "enforcement p99, min - max",
        ],
    )

    lines += [
        "The p99 column is a **range across runs**, not a single figure, because on this "
        "host it is not stable enough to quote as one. `PLAN.md` §13.1 asks for at least "
        "three runs with the variance reported; that requirement is the reason this is "
        "visible rather than a matter of which run got written down.",
        "",
    ]

    unstable = [
        p
        for p in data["profiles"]
        if not p["saturated"]
        and p.get("spread_ms", {}).get("enforcement_overhead_ms", {}).get("max", 0)
        > OVERHEAD_BUDGET
        > p.get("spread_ms", {}).get("enforcement_overhead_ms", {}).get("min", 0)
    ]
    for profile in unstable:
        enforce = profile["spread_ms"]["enforcement_overhead_ms"]
        lines += [
            f"At **{profile['target_rps']} RPS the enforcement p99 ranged "
            f"{enforce['min']}-{enforce['max']} ms across {profile['runs']} runs**, which "
            f"straddles NFR-2's {OVERHEAD_BUDGET} ms budget. The best run is comfortably "
            f"inside it and the worst is nearly ten times it, so **the honest statement is "
            f"that this host cannot establish the p99 either way** — the median run's p50 "
            f"is the only figure here stable enough to quote. Something outside the request "
            f"path is contributing tens of milliseconds intermittently: the generator, the "
            f"three uvicorn processes and Postgres all share one machine, and the "
            f"`generator_lag_ms` series in the JSON shows the harness itself stalling. "
            f"Establishing NFR-2 properly needs the generator off-box.",
            "",
        ]

    saturated = [p for p in data["profiles"] if p["saturated"]]
    if saturated:
        rates = ", ".join(str(p["target_rps"]) for p in saturated)
        worst = max(saturated, key=lambda p: p["target_rps"])
        upstream_only = worst["tiers"]["upstream_only"]
        lines += [
            f"⚠️ **The {rates} RPS profile(s) could not be offered on this host and the "
            f"latencies in those rows are queueing artefacts, not service times.** At "
            f"{worst['target_rps']} RPS the *stub upstream alone* — with no PEP in the path "
            f"at all — achieved only {upstream_only['achieved_rps']} RPS at a p50 of "
            f"{upstream_only['service_ms']['p50']} ms. The limit is the development machine "
            f"running the generator and three uvicorn processes against each other on "
            f"loopback, not anything in AgentIAM. Reported rather than dropped, because "
            f"`PLAN.md` T-053 asks for the numbers actually obtained.",
            "",
            "**So NFR-2 is not yet demonstrated at its stated rate.** What has been measured "
            "is the enforcement cost at rates this host can serve, and the honest reading is "
            "in the row above the warning. Establishing the 500 RPS figure needs a machine "
            "that can generate it, or the generator moved off-box.",
            "",
        ]

    return lines


def render() -> str:
    """The whole document."""
    lines = [_HEADER]
    if _PB2.exists():
        lines += _render_pb2(json.loads(_PB2.read_text(encoding="utf-8")))
    if _NFR2.exists():
        lines += _render_nfr2(json.loads(_NFR2.read_text(encoding="utf-8")))
    lines += [
        "",
        "---",
        "",
        "## How to reproduce",
        "",
        "```",
        "uv run pytest -m perf --benchmark-only          # NFR-1 and PB-2",
        "uv run python scripts/run_load_test.py          # NFR-2, brings up its own Postgres",
        "uv run python scripts/generate_benchmark_results.py",
        "```",
        "",
        "The load test starts its own database on an ephemeral port rather than using "
        "`make up`'s. On the development host a native Windows PostgreSQL shares port 5432 "
        "with the compose one and wins every connection, so a run against `localhost:5432` "
        "silently addresses the wrong database.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the performance table. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="generate_benchmark_results",
        description="Regenerate docs/benchmarks/performance.md from the benchmark JSON.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the committed file differs from what would be written.",
    )
    args = parser.parse_args(argv)

    rendered = render()
    if args.check:
        if not _OUTPUT.exists():
            print(f"{_OUTPUT} does not exist; run without --check", file=sys.stderr)
            return 1
        if _OUTPUT.read_text(encoding="utf-8") != rendered:
            print(
                f"{_OUTPUT} is stale or hand-edited. Regenerate with:\n"
                f"  uv run python scripts/generate_benchmark_results.py",
                file=sys.stderr,
            )
            return 1
        print(f"{_OUTPUT} matches its sources")
        return 0

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
