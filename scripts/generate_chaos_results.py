"""Fold the chaos runs' JSON into `docs/benchmarks/chaos-results.md` — T-052, `PLAN.md` §13.2.

*"Every chaos run emits a JSON result; `docs/benchmarks/chaos-results.md` is regenerated
from those. That table in the submission is worth more than any prose claim about
robustness."*

Which is only true if the table cannot drift from the runs. So the JSON files under
`docs/benchmarks/chaos/` are the artifact and this file renders them; the Markdown is
never edited by hand, and the header says so to whoever opens it next.

The five deferred scenarios are printed too, as **not run**, with their deferral reason.
A results table listing five of twelve rows and saying nothing about the other seven
invites the reader to assume they passed.

`--check` re-renders and exits non-zero if the committed Markdown differs, so CI can catch
a results file that was edited by hand or left stale after a run.
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
_RESULTS_DIR: Final = _REPO_ROOT / "docs" / "benchmarks" / "chaos"
_OUTPUT: Final = _REPO_ROOT / "docs" / "benchmarks" / "chaos-results.md"

#: `PLAN.md` §13.2's twelve, and which of them T-052 is scoped to run. The five in scope
#: come from `ROADMAP.md` line 287; the rest are deferred in `PLAN.md` §21 item 10.
SCENARIOS: Final[list[tuple[str, str, str, bool]]] = [
    (
        "CH-1",
        "Kill Postgres for 30 s",
        "PEPs spend leases, then fail closed; recovery is clean; invariant holds",
        True,
    ),
    ("CH-2", "Kill Redis for 30 s", "revocation falls back to pull; leases unaffected", False),
    (
        "CH-3",
        "SIGKILL one PEP of three",
        "its lease strands <= TTL then reclaims; others unaffected",
        True,
    ),
    ("CH-4", "Partition PEP<->ledger", "bounded spend, then fail closed", True),
    ("CH-5", "500 ms latency on the ledger", "top-ups slow, decisions unaffected", False),
    ("CH-6", "Packet loss 10%", "retries with backoff; no double-spend", False),
    (
        "CH-7",
        "Clock skew +60 s on one PEP",
        "tolerance honoured; no spurious denials or expiries",
        False,
    ),
    ("CH-8", "Ollama down", "template fallback; no hot-path impact", True),
    ("CH-9", "Embedding service down", "strict scopes escalate, log-only allows", False),
    ("CH-10", "Rolling restart under load", "zero dropped requests; invariant holds", True),
    ("CH-11", "Postgres connection-pool exhaustion", "graceful 503; fail closed", False),
    ("CH-12", "Disk full on the audit ledger", "requests denied; alert raised", False),
]

_HEADER: Final = """<!--
  GENERATED FILE — do not edit.

  Regenerate with:  uv run python scripts/generate_chaos_results.py
  Source of truth:  docs/benchmarks/chaos/*.json, written by tests/chaos/ (T-052)
  Verify in CI:     uv run python scripts/generate_chaos_results.py --check
-->

# Chaos results

`PLAN.md` §13.2 defines twelve scenarios. **T-052 is scoped to five** — CH-1, CH-3, CH-4,
CH-8 and CH-10 (`ROADMAP.md` line 287); the other seven are deferred in `PLAN.md` §21 item
10, with "nightly CI after submission" as their resumption trigger. They are listed below as
*not run* rather than omitted, because a table showing five rows and no mention of the rest
reads as twelve passes to anyone skimming it.

The **invariant checker (T-016) runs as a sidecar throughout every scenario**, sweeping the
ledger on a timer. It records three outcomes per sweep, not two: *held*, *violated*, and
*unavailable* — the last being a sweep that could not run at all, which is the honest state
during CH-1, when Postgres is genuinely stopped. Folding "unavailable" into "held" would
report a green run for a database that was not there.

Every scenario runs against real Postgres in a container, a real biscuit chain, the real
Cedar engine, the real lease ledger and the real audit chain. Where a mechanism stands in
for the one `PLAN.md` names, or an expectation is not met, it says so in that scenario's
notes rather than in a footnote.
"""


def load_results() -> dict[str, dict[str, Any]]:
    """Every result file, keyed by scenario id."""
    if not _RESULTS_DIR.is_dir():
        return {}
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(_RESULTS_DIR.glob("*.json")):
        results[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return results


def _key(scenario: str) -> str:
    """`CH-1` in the plan is `CH-01` on disk, so files sort in scenario order."""
    number = scenario.split("-")[1]
    return f"CH-{int(number):02d}"


def _verdict(result: dict[str, Any]) -> str:
    invariant = result["invariant"]
    if invariant["violated"]:
        return "**VIOLATED**"
    if invariant["held"] == 0:
        return "inconclusive — no sweep completed"
    return "held"


def _sidecar_cell(result: dict[str, Any]) -> str:
    inv = result["invariant"]
    cell = f"{inv['held']} held / {inv['violated']} violated"
    if inv["unavailable"]:
        cell += f" / {inv['unavailable']} unavailable"
    return cell


def render(results: dict[str, dict[str, Any]]) -> str:
    """The whole Markdown document."""
    lines: list[str] = [_HEADER, "", "## Summary", ""]
    lines.append("| ID | Scenario | Expected | Invariant sweeps | Verdict |")
    lines.append("|---|---|---|---|---|")

    for scenario, title, expected, in_scope in SCENARIOS:
        result = results.get(_key(scenario))
        if result is None:
            status = "not run — deferred (`PLAN.md` §21)" if not in_scope else "**not run**"
            lines.append(f"| {scenario} | {title} | {expected} | — | {status} |")
            continue
        lines.append(
            f"| {scenario} | {title} | {expected} | {_sidecar_cell(result)} | {_verdict(result)} |"
        )

    extra = sorted(set(results) - {_key(s) for s, _, _, _ in SCENARIOS})
    if extra:
        lines += [
            "",
            "Sub-scenarios — additional runs that probe one aspect of a scenario above:",
            "",
            "| Run | Title | Invariant sweeps | Verdict |",
            "|---|---|---|---|",
        ]
        for key in extra:
            result = results[key]
            lines.append(
                f"| {result['scenario']} | {result['title']} | "
                f"{_sidecar_cell(result)} | {_verdict(result)} |"
            )

    for key in sorted(results):
        lines += _detail(results[key])

    lines.append("")
    return "\n".join(lines)


def _detail(result: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "---",
        "",
        f"## {result['scenario']} — {result['title']}",
        "",
        f"**Expected:** {result['expected']}",
        "",
        f"Run `{result['run_id']}` at `{result['started_at']}`, "
        f"{result['duration_s']} s. Verdict: **{result['verdict']}**.",
        "",
    ]

    inv = result["invariant"]
    lines += [
        f"Invariant sidecar, sweeping every {inv['interval_s']} s: **{inv['samples']} sweeps** "
        f"— {inv['held']} held, {inv['violated']} violated, {inv['unavailable']} unavailable.",
        "",
    ]
    if inv["violations"]:
        lines += ["Violations:", ""]
        lines += [f"- `{violation}`" for violation in inv["violations"]]
        lines.append("")
    if inv["unavailable_reasons"]:
        lines += ["Sweeps that could not run:", ""]
        lines += [f"- `{reason}`" for reason in inv["unavailable_reasons"]]
        lines.append("")

    if result["measurements"]:
        lines += ["| Measurement | Value |", "|---|---|"]
        lines += [
            f"| `{name}` | {_format(value)} |" for name, value in result["measurements"].items()
        ]
        lines.append("")

    if result["loads"]:
        lines += [
            "| Load | Sent | 2xx | Dropped | Statuses | Reason codes | p50 ms | p99 ms |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for load in result["loads"]:
            latency = load["latency_ms"]
            lines.append(
                f"| {load['label']} | {load['sent']} | {load['ok']} | {load['dropped']} | "
                f"{_format(load['by_status'])} | {_format(load['by_reason']) or '—'} | "
                f"{latency['p50']} | {latency['p99']} |"
            )
        lines.append("")

    if result["timeline"]:
        lines += ["Timeline:", ""]
        lines += [f"- `{entry['t_s']:>7.3f} s`  {entry['event']}" for entry in result["timeline"]]
        lines.append("")

    if result["notes"]:
        lines += ["**Notes:**", ""]
        lines += [f"- {note}" for note in result["notes"]]
        lines.append("")

    return lines


def _format(value: Any) -> str:  # noqa: ANN401 - renders whatever a scenario measured
    """Render one measurement for a Markdown cell."""
    if isinstance(value, dict):
        return ", ".join(f"`{k}`: {v}" for k, v in value.items())
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"`{value}`"


def main(argv: Sequence[str] | None = None) -> int:
    """Render the results table. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="generate_chaos_results",
        description="Regenerate docs/benchmarks/chaos-results.md from the chaos runs' JSON.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the committed file differs from what would be written.",
    )
    args = parser.parse_args(argv)

    results = load_results()
    rendered = render(results)

    if args.check:
        if not _OUTPUT.exists():
            print(f"{_OUTPUT} does not exist; run without --check", file=sys.stderr)
            return 1
        current = _OUTPUT.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"{_OUTPUT} is stale or hand-edited. Regenerate with:\n"
                f"  uv run python scripts/generate_chaos_results.py",
                file=sys.stderr,
            )
            return 1
        print(f"{_OUTPUT} matches {len(results)} result file(s)")
        return 0

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {_OUTPUT} from {len(results)} result file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
