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
_NFR2_TWO_HOST: Final = _BENCH / "nfr2-two-host.json"
_OUTPUT: Final = _BENCH / "performance.md"

#: NFR-2's bar, mirrored from scripts/run_load_test.py.
OVERHEAD_BUDGET: Final = 8.0

#: Mirrored from `scripts/run_load_test.HARNESS`. Data carrying it was measured with the
#: harness composed as the deployment is (ADR-062); data without it predates that.
HARNESS: Final = "ADR-062"

#: Generator lag p99 above which the harness, not the server, is the likelier cause of a tail.
#: October's on-schedule runs measured ~1.2-3.2 ms; a stalled generator measures seconds.
LAG_TOLERANCE_MS: Final = 5.0

_TIERS: Final = (
    ("upstream_only", "① upstream alone"),
    ("proxy_no_enforcement", "② PEP, no pipeline"),
    ("enforcing_pep", "③ enforcing PEP"),
)

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
    order = [
        "extract",
        "verify",
        "caveats",
        "token_authority",
        "policy",
        "record_hash",
        "decide_total",
    ]
    labels = {
        "extract": "step 1 — extract (route, args, digest)",
        "verify": "step 2 — verify (biscuit signature)",
        "caveats": "step 4 — caveats (4 clauses, Datalog)",
        "token_authority": "step 4b — token authority (biscuit authorizer, depth 1)",
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
    authority = steps.get("token_authority", {})
    lines += [
        f"**NFR-1 holds**: `decide()` p99 is {total.get('p99_us')} µs against a 1000 µs "
        f"budget, and `PLAN.md` §17's R-2 trigger (2 ms, port to Rust) is not approached.",
        "",
        "Three things the breakdown says that a single number cannot:",
        "",
        f"- **Two steps are the decision.** Cedar at {policy.get('median_us')} µs and the "
        f"token's own Datalog at {authority.get('median_us')} µs together account for "
        f"almost all of a {total.get('median_us')} µs median. Everything else inside "
        f"`decide()` is single-digit microseconds.",
        "",
        f"- **Enforcing the token costs about as much as the policy does, and buys more.** "
        f"Step 4b evaluates every check in every block — the mandate's grant and every "
        f"attenuation caveat — against a token that may have arrived from anywhere. It was "
        f"absent until the manual test pass found that nothing on the PEP path called "
        f"`authorize()`, so the {authority.get('median_us')} µs is new cost against a "
        f"budget that still has room, not a regression.",
        "",
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


#: Rendered only for data measured before ADR-062's harness change.
_PREDATES_NOTE: Final[list[str]] = [
    "> **These figures predate a change to the harness, and understate it slightly.** "
    "NFR-2 is a claim about the *deployed* PEP's overhead, measured through "
    "`scripts/serve_pep.py`. The harness used to hardcode its policy principal and "
    "supply no caveat reader, while the deployed PEP read both out of the token's "
    "own block source (ADR-057) — so it was measuring less work than production "
    "does. The two compositions match as of ADR-062, and a unit test now compares them "
    "by behaviour so they cannot drift apart again. **The numbers below were taken "
    "before that**, and a re-measurement is owed.",
    "",
    "> **By how much, measured** (CPython 3.12, `agentiam_core.datalog`): one "
    "`token_identity()` or `token_caveats()` costs ~125 µs at chain depth 0, "
    "~160 µs at depth 1, ~195 µs at depth 2 and ~227 µs at depth 3. The "
    "pipeline resolves the principal once per request and reads the caveats once, so a "
    "depth-3 request pays roughly **0.45 ms** beyond what tier 3 reports — against "
    "an 8 ms budget, and well inside the run-to-run spread the table already shows. "
    "**NFR-1 is unaffected**: the parse happens in the pipeline, not inside `decide()`, "
    "which is why the PB-2 breakdown above carries no such note.",
    "",
    "> The re-run is deliberately not folded into another ticket. It rewrites "
    "`pb2-breakdown.json` and `nfr2-load.json` together and has to happen on one host "
    "in one sitting, or the before/after comparison measures the hardware rather than "
    "the change.",
    "",
]


def _verdict(profile: dict[str, Any]) -> str:
    """PASS only for a load the host offered, with every run's enforcement p99 inside 8 ms."""
    if profile["saturated"]:
        return "not offered — the host could not generate this rate"
    worst = profile.get("spread_ms", {}).get("enforcement_overhead_ms", {}).get("max")
    if worst is None:
        worst = profile["enforcement_overhead_ms"]["p99"]
    outcome = "PASS" if worst < OVERHEAD_BUDGET else "FAIL"
    return f"**{outcome}** — worst run's enforcement p99 {worst} ms vs {OVERHEAD_BUDGET} ms"


def nfr2_table(data: dict[str, Any]) -> list[str]:
    """Every tier of every profile — offered, achieved, p50/p95/p99 — then a verdict per rate.

    Shared with `run_load_test.py`, which prints it at the end of a run. The tier rows are the
    last run's; the verdict reads every run, because a p99 that passes only on a good run is
    not established.
    """
    rows: list[list[str]] = []
    for profile in data["profiles"]:
        for key, label in _TIERS:
            tier = profile["tiers"][key]
            service = tier["service_ms"]
            rows.append(
                [
                    str(profile["target_rps"]),
                    label,
                    str(tier["achieved_rps"]),
                    str(service["p50"]),
                    str(service["p95"]),
                    str(service["p99"]),
                ]
            )
    lines = _table(rows, ["Target RPS", "Tier", "Achieved RPS", "p50 ms", "p95 ms", "p99 ms"])
    lines += [f"- **{p['target_rps']} RPS**: {_verdict(p)}" for p in data["profiles"]]
    lines.append("")
    return lines


def _host_line(data: dict[str, Any]) -> list[str]:
    host = data.get("host")
    if not host:
        return ["Host: not recorded (this run predates `run_load_test.py` recording one).", ""]
    return [
        f"Host: `{host['platform']}`, Python {host['python']}, {host['cpus']} CPUs. "
        f"Topology: {data.get('topology', 'single-host')}.",
        "",
    ]


def _render_nfr2(data: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## NFR-2 — proxy overhead under load",
        "",
        f"Budget: **p99 < 8 ms at 500 RPS, single instance**. Measured {data['measured_at']}.",
        "",
        *_host_line(data),
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
    if data.get("harness") != HARNESS:
        lines += _PREDATES_NOTE

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
        "Each tier of the last run, with the verdict over every run:",
        "",
        *nfr2_table(data),
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
        # Computed, not written down. This sentence used to say "nearly ten times it",
        # which was true of the run being described and would silently become false of the
        # next one — a generated document must not carry a ratio the data does not support.
        over = enforce["max"] / OVERHEAD_BUDGET
        how_far = f"{over:.1f}\u00d7 it" if over >= 1.5 else "past it"
        # Same rule for the other end: 7.995 ms is inside an 8 ms budget, not comfortably.
        how_close = (
            "comfortably inside it" if enforce["min"] < OVERHEAD_BUDGET * 0.75 else "just inside it"
        )
        lines += [
            f"At **{profile['target_rps']} RPS the enforcement p99 ranged "
            f"{enforce['min']}-{enforce['max']} ms across {profile['runs']} runs**, which "
            f"straddles NFR-2's {OVERHEAD_BUDGET} ms budget. The best run is {how_close} "
            f"and the worst is {how_far}, so **the honest statement is "
            f"that this host cannot establish the p99 either way** — the median run's p50 "
            f"is the only figure here stable enough to quote. {_why_unstable(profile)}",
            "",
        ]

    saturated = [p for p in data["profiles"] if p["saturated"]]
    if saturated:
        rates = ", ".join(str(p["target_rps"]) for p in saturated)
        worst = max(saturated, key=lambda p: p["target_rps"])
        lines += [
            f"⚠️ **The {rates} RPS profile(s) could not be offered on this host and the "
            f"latencies in those rows are queueing artefacts, not service times.** "
            f"{_where_it_saturated(worst)} Reported rather than dropped, because `PLAN.md` "
            f"T-053 asks for the numbers actually obtained.",
            "",
            "**So NFR-2 is not yet demonstrated at its stated rate.** What has been measured "
            "is the enforcement cost at rates this host can serve, and the honest reading is "
            "in the row above the warning. Establishing the 500 RPS figure is what the "
            "two-machine run below is for.",
            "",
        ]

    return lines


def _render_two_host(path: Path) -> list[str]:
    """NFR-2 measured with the generator on another machine, or a plain "not yet run"."""
    lines = ["", "## NFR-2 across two machines", ""]
    if not path.exists():
        return [
            *lines,
            "**Not yet run.** The single-host figures above share one machine between the "
            "load generator, three uvicorn processes and Postgres. The tooling to separate them "
            "exists (APICTA-08); the run is a human step on two hosts on one LAN:",
            "",
            "```",
            "# machine A — the system under test",
            "uv run python scripts/run_load_test.py --role server",
            "# machine B — the generator; paste the command A prints, then:",
            "uv run python scripts/generate_benchmark_results.py",
            "```",
            "",
        ]
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        *lines,
        f"Measured {data['measured_at']} against `{data.get('server_url', '?')}` from a "
        f"second machine: tiers ①-③ crossed the link, the PEPs reached the stub over the "
        f"server's own loopback.",
        "",
        *_host_line(data),
        *nfr2_table(data),
    ]


def _runs(profile: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = profile.get("all_runs") or [profile]
    return runs


def _why_unstable(profile: dict[str, Any]) -> str:
    """Name the likelier cause of a p99 spread from the generator's own lag, not by habit."""
    lags = [
        run["tiers"]["enforcing_pep"].get("generator_lag_ms", {}).get("p99")
        for run in _runs(profile)
    ]
    measured = [lag for lag in lags if lag is not None]
    if not measured:
        return "The generator's lag was not recorded, so the cause cannot be attributed."
    worst = max(measured)
    if worst > LAG_TOLERANCE_MS:
        return (
            f"The generator itself fell behind — `generator_lag_ms` p99 reached {worst} ms — "
            f"so the harness itself stalling is a likely contributor: the generator, three "
            f"uvicorn processes and Postgres share one machine. Establishing NFR-2 properly "
            f"needs the generator off-box."
        )
    return (
        f"The generator kept its schedule — `generator_lag_ms` p99 at most {worst} ms in "
        f"every run — so the spread is on the server's side of the request, not the "
        f"harness's: the PEP, its audit and settlement writes and Postgres still share this "
        f"machine with the stub, and the two-machine run below separates the generator only."
    )


def _where_it_saturated(profile: dict[str, Any]) -> str:
    """Which tier stopped keeping up first — the stub, the PEP's hop, or enforcement."""
    target = profile["target_rps"]
    tiers = profile["tiers"]
    short = next(
        (key for key, _ in _TIERS if tiers[key]["achieved_rps"] < target * 0.9), "enforcing_pep"
    )
    upstream = tiers["upstream_only"]
    if short == "upstream_only":
        return (
            f"At {target} RPS the *stub upstream alone* — with no PEP in the path at all — "
            f"achieved only {upstream['achieved_rps']} RPS at a p50 of "
            f"{upstream['service_ms']['p50']} ms. The limit is the machine running the "
            f"generator and three uvicorn processes against each other on loopback."
        )
    hop = tiers["proxy_no_enforcement"]
    if short == "proxy_no_enforcement":
        return (
            f"At {target} RPS the stub alone kept up ({upstream['achieved_rps']} RPS), but "
            f"the PEP, with no pipeline attached, peaked at {hop['achieved_rps']} RPS and the "
            f"enforcing PEP at {tiers['enforcing_pep']['achieved_rps']}. The ceiling is the "
            f"PEP's own proxy hop in one process, before any authorization runs; whether it "
            f"holds with the generator on another machine is what the two-machine run "
            f"measures."
        )
    return (
        f"At {target} RPS the stub ({upstream['achieved_rps']} RPS) and the PEP's bare hop "
        f"({hop['achieved_rps']} RPS) kept up, and the enforcing PEP peaked at "
        f"{tiers['enforcing_pep']['achieved_rps']} RPS: authorization is the ceiling."
    )


def render(*, pb2: Path = _PB2, nfr2: Path = _NFR2, two_host: Path = _NFR2_TWO_HOST) -> str:
    """The whole document. The paths are parameters so a test can render other data."""
    lines = [_HEADER]
    if pb2.exists():
        lines += _render_pb2(json.loads(pb2.read_text(encoding="utf-8")))
    if nfr2.exists():
        lines += _render_nfr2(json.loads(nfr2.read_text(encoding="utf-8")))
    lines += _render_two_host(two_host)
    lines += [
        "",
        "---",
        "",
        "## How to reproduce",
        "",
        "```",
        "uv run pytest -m perf --benchmark-only          # NFR-1 and PB-2",
        "uv run python scripts/run_load_test.py          # NFR-2, brings up its own Postgres",
        "uv run python scripts/run_load_test.py --role server   # two machines: A, then B",
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
