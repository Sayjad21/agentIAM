"""`scripts/generate_evidence_pack.py` — T-055, `PLAN.md` §14.

Unlike `generate_chaos_results.py` / `generate_benchmark_results.py` / `generate_sbom.py`,
this script carries real hand-authored content (the invariants table, the PB-1..PB-12
coverage table, the A-01..A-33 red-team table) rather than only re-rendering numbers a test
already produced. That is exactly the kind of thing that silently drifts from the documents
it is supposed to summarize, so it gets a dedicated test file where its siblings do not.

Three things this file checks that matter more than "the script runs":

1. **Completeness** — every item `PLAN.md` §14.1 lists has a section, every invariant, every
   published-benchmark id, and every red-team attack id actually appears. A pack missing a
   row silently understates what was tested, which is the opposite of the point.
2. **No fabrication** — the drift model card, mutation testing, and OSS traction sections
   must say plainly that the underlying work has not happened, not imply otherwise.
3. **No drift from the sources it folds** — the chaos, performance, security-scan and
   threat-model content embedded in the pack must be the *actual* committed content, not a
   paraphrase that can quietly diverge from it.
"""

from __future__ import annotations

import subprocess  # nosec B404 - fixed argv, no shell, exercises this script's own CLI
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import generate_benchmark_results, generate_chaos_results, generate_evidence_pack

if TYPE_CHECKING:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "generate_evidence_pack.py"

pytestmark = pytest.mark.filterwarnings("ignore")


# --------------------------------------------------------------------------- render() shape


class TestRenderShape:
    def test_it_is_a_single_self_contained_html_document(self) -> None:
        html = generate_evidence_pack.render()
        assert html.lstrip().startswith("<!doctype html>")
        assert "<html" in html
        assert "</html>" in html
        # No external asset references — a judge must be able to open this file offline,
        # on a machine with no network, per DEMO.md F-1's whole framing. Embedded prose
        # (e.g. security-scan.md discussing a `http://127.0.0.1/healthz` probe) legitimately
        # mentions URLs as *text*; what must never appear is a tag that actually loads one.
        for pattern in ("<link ", "<script src=", "<img ", "@import", "url(http"):
            assert pattern not in html, f"found an external-asset pattern: {pattern!r}"

    def test_every_plan_14_1_section_is_present(self) -> None:
        html = generate_evidence_pack.render()
        for anchor in generate_evidence_pack.SECTION_IDS:
            assert f'id="{anchor}"' in html, f"missing section anchor {anchor!r}"

    def test_no_placeholder_text_leaked_into_the_output(self) -> None:
        html = generate_evidence_pack.render()
        for marker in ("TODO", "TBD", "FIXME", "XXX", "Lorem ipsum"):
            assert marker not in html, f"placeholder marker {marker!r} found in rendered pack"


# --------------------------------------------------------------------------- invariants


class TestInvariantsTable:
    def test_all_ten_invariants_are_present_with_their_property_test(self) -> None:
        html = generate_evidence_pack.render()
        for inv_id, _name, _mechanism, test_id in generate_evidence_pack.INVARIANTS:
            assert inv_id in html
            if test_id is not None:
                assert test_id in html

    def test_inv5_is_attributed_to_p10_the_stateful_machine(self) -> None:
        row = next(r for r in generate_evidence_pack.INVARIANTS if r[0] == "INV-5")
        assert row[3] == "P-10"

    def test_inv4_has_no_numbered_property_test(self) -> None:
        # Non-forgeability is verified by T-007's tamper/wrong-key/truncation unit tests,
        # not by a numbered P-xx property in spec 03 §6's own mapping table. Claiming a
        # P-id here would be citing a test that does not test this invariant.
        row = next(r for r in generate_evidence_pack.INVARIANTS if r[0] == "INV-4")
        assert row[3] is None

    def test_exactly_ten_invariants(self) -> None:
        assert len(generate_evidence_pack.INVARIANTS) == 10
        assert {row[0] for row in generate_evidence_pack.INVARIANTS} == {
            f"INV-{n}" for n in range(1, 11)
        }


# --------------------------------------------------------------------------- benchmarks


class TestBenchmarkCoverageTable:
    def test_all_twelve_pb_ids_present(self) -> None:
        html = generate_evidence_pack.render()
        for pb_id, _measurement, _status in generate_evidence_pack.BENCHMARKS:
            assert pb_id in html

    def test_exactly_twelve_benchmarks_and_ids_match_plan(self) -> None:
        assert len(generate_evidence_pack.BENCHMARKS) == 12
        assert {row[0] for row in generate_evidence_pack.BENCHMARKS} == {
            f"PB-{n}" for n in range(1, 13)
        }

    def test_pb1_and_pb2_are_reported_measured(self) -> None:
        by_id = {row[0]: row[2] for row in generate_evidence_pack.BENCHMARKS}
        assert by_id["PB-1"].startswith("measured")
        assert by_id["PB-2"].startswith("measured")

    def test_the_performance_report_is_embedded_verbatim(self) -> None:
        html = generate_evidence_pack.render()
        expected = generate_benchmark_results.render()
        assert generate_evidence_pack._escape(expected) in html


# --------------------------------------------------------------------------- chaos


class TestChaosSection:
    def test_the_chaos_results_are_embedded_verbatim(self) -> None:
        html = generate_evidence_pack.render()
        expected = generate_chaos_results.render(generate_chaos_results.load_results())
        assert generate_evidence_pack._escape(expected) in html

    def test_all_twelve_ch_ids_are_mentioned(self) -> None:
        # The embedded chaos-results.md already lists all twelve (five run, seven
        # deferred) — this just confirms the embedding did not truncate anything.
        html = generate_evidence_pack.render()
        for n in range(1, 13):
            assert f"CH-{n}" in html


# --------------------------------------------------------------------------- security scan


class TestSecurityScanSection:
    def test_the_security_scan_report_is_embedded_verbatim(self) -> None:
        html = generate_evidence_pack.render()
        expected = generate_evidence_pack._SECURITY_SCAN.read_text(encoding="utf-8")
        assert generate_evidence_pack._escape(expected) in html

    def test_the_sbom_component_count_is_reported(self) -> None:
        import json

        html = generate_evidence_pack.render()
        sbom = json.loads(generate_evidence_pack._SBOM.read_text(encoding="utf-8"))
        idx = html.find('id="security-scan"')
        assert idx != -1
        section = html[idx : idx + 3000]
        assert str(len(sbom["components"])) in section
        assert sbom["specVersion"] in section

    def test_a_missing_sbom_says_so_rather_than_erroring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(generate_evidence_pack, "_SBOM", tmp_path / "does-not-exist.json")
        html = generate_evidence_pack.render()
        idx = html.find('id="security-scan"')
        assert idx != -1
        section = html[idx : idx + 3000]
        assert "SBOM not found" in section


# --------------------------------------------------------------------------- red team


class TestRedTeamTable:
    def test_all_thirty_three_attack_ids_present(self) -> None:
        html = generate_evidence_pack.render()
        ids = {row[0] for row in generate_evidence_pack.RED_TEAM}
        assert ids == {f"A-{n:02d}" for n in range(1, 34)}
        for attack_id in ids:
            assert attack_id in html

    def test_tm19_and_tm20_are_present_alongside_the_a_ids(self) -> None:
        html = generate_evidence_pack.render()
        assert "TM-19" in html
        assert "TM-20" in html

    def test_accepted_risks_match_the_threat_model(self) -> None:
        # threat-model.md §7: the three accepted risks are bearer replay (TM-01 / A-06),
        # slow-drift evasion (TM-11 / A-27), and agent-reported amounts (TM-23 / A-19).
        accepted = {row[0] for row in generate_evidence_pack.RED_TEAM if row[3] == "accepted risk"}
        assert accepted == {"A-06", "A-19", "A-27"}

    def test_partially_mitigated_matches_the_threat_model(self) -> None:
        partial = {
            row[0] for row in generate_evidence_pack.RED_TEAM if row[3] == "partially mitigated"
        }
        assert partial == {"A-12", "A-32"}

    def test_no_row_claims_a_status_outside_the_closed_set(self) -> None:
        allowed = {"mitigated", "partially mitigated", "accepted risk"}
        for row in generate_evidence_pack.RED_TEAM:
            assert row[3] in allowed, f"{row[0]} has an unrecognised verdict {row[3]!r}"


# --------------------------------------------------------------------------- honesty sections


class TestHonestyAboutWhatIsNotBuilt:
    def test_drift_model_card_says_it_is_deferred(self) -> None:
        html = generate_evidence_pack.render()
        assert "T-034" in html
        assert "T-035" in html
        assert "deferred" in html.lower()

    def test_mutation_testing_says_it_has_not_run(self) -> None:
        html = generate_evidence_pack.render()
        assert "mutation" in html.lower()
        assert "not" in html.lower()  # weak on its own; the strong check is the next test

    def test_mutation_testing_section_states_no_run_exists(self) -> None:
        html = generate_evidence_pack.render()
        idx = html.find('id="coverage-mutation"')
        assert idx != -1
        section = html[idx : idx + 2000]
        assert "mutmut" in section.lower()
        assert "run has been committed" in section.lower()

    def test_oss_traction_says_not_applicable(self) -> None:
        html = generate_evidence_pack.render()
        idx = html.find('id="oss-traction"')
        assert idx != -1
        section = html[idx : idx + 2000]
        assert "not applicable" in section.lower() or "pre-release" in section.lower()

    def test_audit_transcript_does_not_claim_a_live_tamper_demo(self) -> None:
        html = generate_evidence_pack.render()
        idx = html.find('id="audit-transcript"')
        assert idx != -1
        section = html[idx : idx + 3000]
        # It must point at the real, already-proven tests rather than assert a transcript
        # exists when no live database was involved in generating this file.
        assert "test_audit_chain.py" in section
        assert "verify_audit_chain.py" in section


# --------------------------------------------------------------------------- IP statement


class TestIpStatement:
    def test_mentions_the_100_percent_bd_and_apache_claims(self) -> None:
        html = generate_evidence_pack.render()
        idx = html.find('id="ip-statement"')
        assert idx != -1
        section = html[idx : idx + 3000]
        assert "Apache-2.0" in section
        assert "100%" in section


# --------------------------------------------------------------------------- escaping


class TestEscaping:
    def test_pre_helper_escapes_html_metacharacters(self) -> None:
        dangerous = '<script>alert("x")</script> & < > "quoted"'
        escaped = generate_evidence_pack._escape(dangerous)
        assert "<script>" not in escaped
        assert "&lt;script&gt;" in escaped
        assert "&amp;" in escaped


# --------------------------------------------------------------------------- --check semantics


class TestCheckMode:
    def test_check_fails_when_no_file_exists_yet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(generate_evidence_pack, "_OUTPUT", tmp_path / "evidence-pack.html")
        assert generate_evidence_pack.main(["--check"]) == 1

    def test_check_passes_once_written_and_fails_after_a_hand_edit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "evidence-pack.html"
        monkeypatch.setattr(generate_evidence_pack, "_OUTPUT", output)

        assert generate_evidence_pack.main([]) == 0
        assert output.exists()
        assert generate_evidence_pack.main(["--check"]) == 0

        output.write_text(output.read_text(encoding="utf-8") + "\n<!-- tampered -->\n")
        assert generate_evidence_pack.main(["--check"]) == 1

    def test_write_mode_produces_the_same_bytes_as_render(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "evidence-pack.html"
        monkeypatch.setattr(generate_evidence_pack, "_OUTPUT", output)
        generate_evidence_pack.main([])
        assert output.read_text(encoding="utf-8") == generate_evidence_pack.render()


class TestRunsFromAnyWorkingDirectory:
    """The CLI must work when invoked as a plain subprocess, not only under pytest.

    `python scripts/generate_evidence_pack.py` puts `scripts/`, not the repo root, at
    sys.path[0] -- pytest's own `pythonpath = ["."]` config is what makes the module
    import cleanly under this test suite, and that config does not apply outside pytest.
    Without the module's own sys.path bootstrap this failed with `ModuleNotFoundError:
    No module named 'scripts'` the first time it was run as a real subprocess from a
    directory other than the repo root -- a real bug, not a hypothetical one, and the
    reason this test exists rather than trusting the import at the top of this file.
    """

    def test_the_cli_runs_when_invoked_from_an_unrelated_directory(self, tmp_path: Path) -> None:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(_SCRIPT), "--check"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode in (0, 1), result.stderr
        assert "ModuleNotFoundError" not in result.stderr


class TestLineEndingsAreStableAcrossPlatforms:
    """The pack is compared byte-for-byte, so it must not depend on the OS that wrote it.

    `write_text`'s platform default would emit CRLF on Windows.
    """

    def test_written_bytes_are_lf_only_on_every_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "evidence-pack.html"
        monkeypatch.setattr(generate_evidence_pack, "_OUTPUT", output)
        generate_evidence_pack.main([])
        assert b"\r\n" not in output.read_bytes()

    def test_check_rejects_a_crlf_copy_of_the_same_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard this pins: with universal-newline reads, a CRLF file decodes back to
        # LF and compares equal, so `--check` would pass on a file whose bytes are not
        # what the script writes. Removing `newline=""` from the read makes this go green
        # for the wrong reason, which is exactly what it is here to catch.
        output = tmp_path / "evidence-pack.html"
        monkeypatch.setattr(generate_evidence_pack, "_OUTPUT", output)
        rendered = generate_evidence_pack.render()
        output.write_bytes(rendered.replace("\n", "\r\n").encode("utf-8"))
        assert generate_evidence_pack.main(["--check"]) == 1
