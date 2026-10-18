"""`performance.md`'s NFR-2 rendering — the per-tier table and the two-machine section (APICTA-08).

The document is generated, so what it may say is decided by the data rather than by whoever
last edited it. Three rules are pinned here: a pass needs a load the host could actually offer;
the note saying the figures predate ADR-062's harness change appears only while they do; and a
two-machine section that has never been run says so instead of being absent.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import generate_benchmark_results as gen


def series(p50: float, p95: float, p99: float) -> dict[str, float]:
    return {"p50": p50, "p95": p95, "p99": p99, "max": p99, "median": p50}


def tier(achieved: float, p50: float, p95: float, p99: float) -> dict[str, Any]:
    return {"achieved_rps": achieved, "service_ms": series(p50, p95, p99)}


def profile(
    rps: int, *, enforce_p99: tuple[float, float], saturated: bool = False
) -> dict[str, Any]:
    tiers = {
        "upstream_only": tier(rps, 1.0, 1.5, 2.0),
        "proxy_no_enforcement": tier(rps, 2.0, 2.5, 3.0),
        "enforcing_pep": tier(rps * (0.5 if saturated else 1), 3.0, 3.5, 3.0 + enforce_p99[1]),
    }
    low, high = enforce_p99
    return {
        "target_rps": rps,
        "runs": 3,
        "saturated": saturated,
        "tiers": tiers,
        "spread_ms": {
            "enforcement_overhead_ms": {"min": low, "median": high, "max": high},
            "enforcement_overhead_p50_ms": {"min": 1.0, "median": 1.0, "max": 1.0},
        },
        "enforcement_overhead_ms": {"p50": 1.0, "p99": high},
    }


def results(*profiles: dict[str, Any], harness: str | None = gen.HARNESS) -> dict[str, Any]:
    data: dict[str, Any] = {"measured_at": "2026-10-18T00:00:00+00:00", "profiles": list(profiles)}
    if harness is not None:
        data["harness"] = harness
        data["host"] = {"platform": "Linux-x86_64", "python": "3.12.10", "cpus": 8}
        data["topology"] = "single-host"
    return data


class TestTheTierTable:
    def test_every_tier_has_achieved_and_three_percentiles(self) -> None:
        text = "\n".join(gen.nfr2_table(results(profile(100, enforce_p99=(1.0, 2.0)))))
        for label in ("upstream alone", "PEP, no pipeline", "enforcing PEP"):
            assert label in text
        assert "| 100 | ① upstream alone | 100 | 1.0 | 1.5 | 2.0 |" in text

    def test_a_profile_inside_the_budget_on_every_run_passes(self) -> None:
        text = "\n".join(gen.nfr2_table(results(profile(500, enforce_p99=(1.0, 7.9)))))
        assert "PASS" in text
        assert "FAIL" not in text

    def test_one_run_over_the_budget_fails_the_profile(self) -> None:
        """The worst run decides: a p99 that passes only on a good run is not established."""
        text = "\n".join(gen.nfr2_table(results(profile(100, enforce_p99=(1.0, 8.5)))))
        assert "FAIL" in text

    def test_a_load_the_host_could_not_offer_is_never_a_pass(self) -> None:
        text = "\n".join(
            gen.nfr2_table(results(profile(500, enforce_p99=(0.1, 0.2), saturated=True)))
        )
        assert "PASS" not in text
        assert "not offered" in text


class TestTheHarnessNote:
    def test_it_is_shown_while_the_figures_predate_adr_062(self) -> None:
        text = "\n".join(gen._render_nfr2(results(profile(100, enforce_p99=(1, 2)), harness=None)))
        assert "predate a change to the harness" in text

    def test_it_is_gone_once_they_do_not(self) -> None:
        text = "\n".join(gen._render_nfr2(results(profile(100, enforce_p99=(1, 2)))))
        assert "predate a change to the harness" not in text

    def test_the_host_is_stated(self) -> None:
        text = "\n".join(gen._render_nfr2(results(profile(100, enforce_p99=(1, 2)))))
        assert "Linux-x86_64" in text
        assert "8 CPUs" in text


class TestTwoMachines:
    @pytest.fixture
    def files(self, tmp_path: Path) -> dict[str, Path]:
        single = tmp_path / "nfr2-load.json"
        single.write_text(json.dumps(results(profile(100, enforce_p99=(1, 2)))), encoding="utf-8")
        return {"nfr2": single, "two_host": tmp_path / "nfr2-two-host.json"}

    def test_a_run_never_made_says_so(self, files: dict[str, Path]) -> None:
        text = gen.render(pb2=Path("/nonexistent"), nfr2=files["nfr2"], two_host=files["two_host"])
        assert "## NFR-2 across two machines" in text
        assert "Not yet run" in text
        assert "--role server" in text

    def test_a_run_made_is_rendered_from_its_own_file(self, files: dict[str, Path]) -> None:
        two = copy.deepcopy(results(profile(500, enforce_p99=(4.0, 6.0))))
        two["topology"] = "two-host"
        two["server_url"] = "http://10.0.0.5:18080"
        files["two_host"].write_text(json.dumps(two), encoding="utf-8")
        text = gen.render(pb2=Path("/nonexistent"), nfr2=files["nfr2"], two_host=files["two_host"])
        section = text.split("## NFR-2 across two machines", 1)[1]
        assert "Not yet run" not in section
        assert "| 500 |" in section
        assert "PASS" in section

    def test_the_committed_document_is_what_the_defaults_render(self) -> None:
        assert gen._OUTPUT.read_text(encoding="utf-8") == gen.render()


def lagging(data: dict[str, Any], tier_key: str, lag_p99: float) -> dict[str, Any]:
    for p in data["profiles"]:
        p["tiers"][tier_key]["generator_lag_ms"] = series(lag_p99 / 2, lag_p99, lag_p99)
    return data


class TestTheNarrativeFollowsTheData:
    """The paragraphs under the table were written for one run and outlived it.

    In October's data the stub alone reached 497.5 RPS and the generator kept its schedule —
    and the page still said the stub fell short and the harness was stalling. Both sentences
    are now chosen by the numbers.
    """

    def test_a_generator_on_schedule_is_not_blamed(self) -> None:
        data = results(profile(100, enforce_p99=(1.0, 12.0)))
        for key, _ in gen._TIERS:
            lagging(data, key, 1.2)
        text = "\n".join(gen._render_nfr2(data))
        assert "kept its schedule" in text
        assert "harness itself stalling" not in text

    def test_a_generator_that_fell_behind_is_named(self) -> None:
        data = results(profile(100, enforce_p99=(1.0, 12.0)))
        for key, _ in gen._TIERS:
            lagging(data, key, 40.0)
        assert "harness itself stalling" in "\n".join(gen._render_nfr2(data))

    def test_saturation_names_the_tier_that_fell_short(self) -> None:
        data = results(profile(500, enforce_p99=(100.0, 200.0), saturated=True))
        data["profiles"][0]["tiers"]["proxy_no_enforcement"]["achieved_rps"] = 360.0
        text = "\n".join(gen._render_nfr2(data))
        assert "PEP, with no pipeline attached, peaked at 360.0 RPS" in text
        assert "not anything in AgentIAM" not in text

    def test_saturation_of_the_stub_itself_blames_the_host(self) -> None:
        data = results(profile(500, enforce_p99=(100.0, 200.0), saturated=True))
        data["profiles"][0]["tiers"]["upstream_only"]["achieved_rps"] = 138.0
        text = "\n".join(gen._render_nfr2(data))
        assert "*stub upstream alone*" in text
        assert "138.0 RPS" in text

    def test_a_best_run_at_the_edge_is_not_called_comfortable(self) -> None:
        data = results(profile(100, enforce_p99=(7.995, 9.4)))
        text = "\n".join(gen._render_nfr2(data))
        assert "just inside it" in text
        assert "comfortably" not in text
