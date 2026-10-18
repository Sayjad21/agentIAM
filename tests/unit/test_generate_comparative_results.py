"""`docs/benchmarks/comparative.md` — AgentIAM against the designs it replaces (APICTA-08).

Every number the project had published was AgentIAM against AgentIAM (`research/lease-plan.md`
GAP-D). These are the first comparisons, and a comparison is easy to make flattering by
choosing what to leave out. So the rendering is pinned on the points a reviewer would check
first: the token-exchange baseline is called simulated and says how it was set up; every ratio
is computed from the measured numbers rather than written down; an overspend in either budget
design is reported as a failure, not a footnote; and an LLM call that could not be measured
says so instead of falling back to an assumed figure.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from scripts import generate_comparative_results as gen


def stats(median: float, p99: float, n: int = 500) -> dict[str, float]:
    return {"n": n, "median_us": median, "p99_us": p99}


DATA: dict[str, Any] = {
    "measured_at": "2026-10-18T00:00:00+00:00",
    "host": {"platform": "Linux-x86_64", "python": "3.12.10", "cpus": 8},
    "attenuation": {
        "in_process": stats(400.0, 900.0),
        "token_exchange": stats(2000.0, 6000.0),
    },
    "budget": {
        "peps": 3,
        "calls_per_pep": 300,
        "amount": "10.0000",
        "b1": {
            **stats(1500.0, 9000.0, 900),
            "calls_per_s": 600.0,
            "ledger_round_trips": 900,
            "refused": 0,
            "overspend": "0.0000",
        },
        "b3": {
            **stats(15.0, 3000.0, 900),
            "calls_per_s": 20000.0,
            "ledger_round_trips": 9,
            "refused": 0,
            "waits": 6,
            "overspend": "0.0000",
        },
    },
    "context": {
        "in_process_us": 500.0,
        "llm": {
            "status": "measured",
            "backend": "gemini",
            "model": "gemini-3.5-flash-lite",
            "n": 5,
            "median_ms": 800.0,
            "max_ms": 1200.0,
        },
    },
}


def render(**changes: Any) -> str:
    data = copy.deepcopy(DATA)
    for path, value in changes.items():
        node = data
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return gen.render(data)


class TestHonesty:
    def test_the_exchange_baseline_is_labelled_simulated_with_its_setup(self) -> None:
        text = render()
        assert "simulated OAuth2 token exchange" in text
        for detail in ("loopback", "keep-alive", "primary-key", "no TLS"):
            assert detail in text, detail

    def test_ratios_are_computed_from_the_numbers(self) -> None:
        assert "5.0\u00d7" in render()
        assert "10.0\u00d7" in render(attenuation__token_exchange=stats(4000.0, 6000.0))

    def test_no_average_appears(self) -> None:
        """`PLAN.md` §13.1: medians and percentiles only."""
        lowered = render().lower()
        assert "average" not in lowered
        assert "mean " not in lowered

    def test_the_host_is_stated(self) -> None:
        assert "Linux-x86_64" in render()


class TestBudget:
    def test_both_designs_are_reported_with_their_ledger_traffic(self) -> None:
        text = render()
        assert "B1" in text
        assert "B3" in text
        assert "| 900 |" in text
        assert "| 9 |" in text

    def test_an_overspend_is_a_failure_not_a_footnote(self) -> None:
        text = render(budget__b3__overspend="20.0000")
        assert "OVERSPENT" in text
        assert "20.0000" in text

    def test_no_overspend_is_stated_as_held(self) -> None:
        assert "OVERSPENT" not in render()
        assert "never exceeded the pool" in render()

    def test_spend_the_ledger_never_recorded_is_a_failure(self) -> None:
        """Overspend cannot see it: the books stay inside the pool while money leaks back."""
        text = render(budget__b3__spent="9000.0000", budget__b3__committed="4920.0000")
        assert "UNDER-RECORDED ৳4080.0000 of ৳9000.0000" in text
        assert "never exceeded the pool" not in text

    def test_spend_recorded_in_full_is_stated(self) -> None:
        text = render(budget__b3__spent="9000.0000", budget__b3__committed="9000.0000")
        assert "UNDER-RECORDED" not in text
        assert "recorded every taka spent" in text


class TestContext:
    def test_the_llm_share_is_computed(self) -> None:
        text = render()
        assert "gemini-3.5-flash-lite" in text
        assert "0.06%" in text  # 0.5 ms of 800 ms

    def test_an_unmeasured_llm_says_so_and_assumes_nothing(self) -> None:
        text = render(context__llm={"status": "unavailable", "reason": "GEMINI_API_KEY not set"})
        assert "not measured" in text
        assert "GEMINI_API_KEY not set" in text
        assert "1,200" not in text
        assert "1200" not in text


class TestCheck:
    def test_a_stale_file_fails(self, tmp_path: Path) -> None:
        source = tmp_path / "comparative.json"
        output = tmp_path / "comparative.md"
        source.write_text(json.dumps(DATA), encoding="utf-8")
        assert gen.main(["--source", str(source), "--output", str(output)]) == 0
        assert gen.main(["--check", "--source", str(source), "--output", str(output)]) == 0
        output.write_text(output.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
        assert gen.main(["--check", "--source", str(source), "--output", str(output)]) == 1

    def test_a_missing_measurement_is_not_rendered_as_a_document(self, tmp_path: Path) -> None:
        missing = tmp_path / "comparative.json"
        assert gen.main(["--source", str(missing), "--output", str(tmp_path / "x.md")]) == 1

    def test_the_committed_document_matches_its_measurement(self) -> None:
        committed = gen._OUTPUT.read_text(encoding="utf-8")
        assert committed == gen.render(json.loads(gen._SOURCE.read_text(encoding="utf-8")))
