"""A drift check must not run in a job that regenerates what it checks.

This has now bitten twice, in the same shape both times.

**`performance.md`** (STATUS gap 24, TODO item 13). Its `--check` renders from the committed
`pb2-breakdown.json` / `nfr2-load.json`, and the `quality` job's benchmark step rewrites that
JSON. The check was placed in `evidence-pack` — a job that never runs a benchmark — precisely
so it could not invalidate its own input. That reasoning is written out at length in
`ci.yml`, and it is correct.

**`chaos-results.md`** (TODO item 22). The same reasoning was never applied. Its `--check` ran
inside the `chaos` job, immediately after `pytest -m chaos` — and the scenarios *rewrite*
`docs/benchmarks/chaos/*.json` as they run: a fresh `run_id`, a new `started_at`, a different
`duration_s`, different event timings. So the step compared the committed Markdown against
JSON that had just changed underneath it, and could never pass. Every nightly run failed on
it, and nothing said so on a push, because the `chaos` job only runs on a schedule.

So the rule gets a test rather than a comment. The comment existed and was not generalised;
an assertion is what makes the next generator inherit it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

#: What each drift check reads, and the step commands that *rewrite* those inputs. A job
#: containing both is a job whose check is comparing against a moving target.
#:
#: Keyed by the generator invoked with `--check`; the values are substrings of the `run:`
#: commands that regenerate its sources.
_REGENERATORS: dict[str, tuple[str, ...]] = {
    "generate_chaos_results.py": ("pytest -m chaos",),
    "generate_benchmark_results.py": ("pytest -m perf", "run_load_test.py"),
}


def _jobs() -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return dict(parsed["jobs"])


def _commands(job: dict[str, Any]) -> list[str]:
    return [str(step.get("run", "")) for step in job.get("steps", [])]


@pytest.mark.parametrize(("generator", "regenerators"), sorted(_REGENERATORS.items()))
def test_a_drift_check_does_not_share_a_job_with_its_regenerator(
    generator: str, regenerators: tuple[str, ...]
) -> None:
    """The rule itself. Both incidents are instances of exactly this."""
    for name, job in _jobs().items():
        commands = _commands(job)
        checks = [c for c in commands if generator in c and "--check" in c]
        if not checks:
            continue
        clashes = [r for r in regenerators for c in commands if r in c]
        assert not clashes, (
            f"job {name!r} runs {clashes} and then byte-checks {generator}. Those steps "
            f"rewrite the very files the check renders from, so it can never pass. Move "
            f"the check to a job that only reads them — `evidence-pack` is where the other "
            f"two live."
        )


@pytest.mark.parametrize("generator", sorted(_REGENERATORS))
def test_every_drift_check_still_runs_somewhere(generator: str) -> None:
    """Moving a check out of the wrong job must not mean deleting it.

    The failure this guards against is the tempting fix for the one above: a check that
    cannot pass is easy to remove, and the document then drifts silently — which is the
    state gap 24 described for `performance.md` before item 13.
    """
    jobs = _jobs()
    homes = [
        name
        for name, job in jobs.items()
        if any(generator in c and "--check" in c for c in _commands(job))
    ]
    assert homes, f"nothing runs {generator} --check any more"


def test_the_drift_checks_run_on_a_push_not_only_on_a_schedule() -> None:
    """A check that only runs nightly reports drift a day after it lands.

    `chaos-results.md`'s check used to sit in the `chaos` job, which is
    `if: schedule || workflow_dispatch` — so even had it been able to pass, a stale table
    would have gone unnoticed until the next night. Its new home has no such condition.
    """
    jobs = _jobs()
    for generator in _REGENERATORS:
        for name, job in jobs.items():
            if not any(generator in c and "--check" in c for c in _commands(job)):
                continue
            assert "if" not in job, (
                f"{generator} --check lives in job {name!r}, which is conditional "
                f"({job.get('if')!r}). Drift would be reported late or not at all."
            )
