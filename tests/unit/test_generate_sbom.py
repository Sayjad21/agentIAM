"""`scripts/generate_sbom.py`'s platform guard — TODO item 12.

The SBOM's component set is resolved from the *installed* environment, which is a measured,
deliberate choice (see that module's docstring: two independent fresh Ubuntu containers
produce byte-identical output, a long-lived local venv does not). The consequence is that
the committed file is only valid for one platform, CI's `ubuntu-latest`.

What was wrong was not that, but what the script *said* when run anywhere else. On Windows
it reported

    docs/evidence/sbom.json: OUT OF DATE.
    Re-run with --write and commit the update.

Both sentences are false there: the file is not out of date, and following the instruction
commits a 137-component Windows SBOM (`colorama`, `pywin32`) over the 136-component Linux
one (`uvloop`), which CI's security-scan job then rejects. A developer doing exactly what
the tool told them to would break the build.

These tests pin the behaviours that fix it, on *both* sides of the platform check, so
neither branch can regress on a machine that cannot reach the other. Nothing here runs
`cyclonedx-py`: the guard is a pure function of `platform`, which is what makes it testable
from either host.
"""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

import pytest

from scripts import generate_sbom

if TYPE_CHECKING:
    from pathlib import Path

_COMMITTED = '{"components": [{"bom-ref": "committed-on-linux"}]}'
_RENDERED_HERE = '{"components": []}'


def _sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rendered: str) -> Path:
    """Point the module at a throwaway SBOM and a fixed rendering."""
    monkeypatch.setattr(generate_sbom, "_generate_sbom", lambda: rendered)
    sbom = tmp_path / "sbom.json"
    sbom.write_text(_COMMITTED, encoding="utf-8")
    monkeypatch.setattr(generate_sbom, "SBOM_PATH", sbom)
    monkeypatch.setattr(generate_sbom, "REPO_ROOT", tmp_path)
    return sbom


class TestPlatformDetection:
    def test_linux_x86_64_is_the_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
        assert generate_sbom._on_reference_platform()

    @pytest.mark.parametrize(
        ("system", "machine"),
        [
            ("Windows", "AMD64"),
            ("Darwin", "arm64"),
            ("Linux", "aarch64"),  # right OS, wrong architecture — still not the reference
        ],
    )
    def test_anything_else_is_not(
        self, monkeypatch: pytest.MonkeyPatch, system: str, machine: str
    ) -> None:
        monkeypatch.setattr(platform, "system", lambda: system)
        monkeypatch.setattr(platform, "machine", lambda: machine)
        assert not generate_sbom._on_reference_platform()

    def test_the_note_names_both_platforms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The message must be actionable: a container log is its first reader."""
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform, "machine", lambda: "AMD64")
        note = generate_sbom._platform_note()
        assert "Windows" in note
        assert "Linux" in note


class TestOffReferencePlatform:
    """The behaviours that stop the tool from giving harmful advice."""

    @pytest.fixture(autouse=True)
    def _elsewhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(generate_sbom, "_on_reference_platform", lambda: False)

    def test_a_difference_is_reported_as_unchecked_not_as_a_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit 0, so `make security` can pass on the machine the repo is developed on."""
        _sandbox(monkeypatch, tmp_path, _RENDERED_HERE)
        monkeypatch.setattr("sys.argv", ["generate_sbom"])

        assert generate_sbom.main() == 0
        assert "NOT CHECKED" in capsys.readouterr().out

    def test_write_refuses_rather_than_committing_this_hosts_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The harmful action the old message invited. It must not succeed by accident."""
        sbom = _sandbox(monkeypatch, tmp_path, _RENDERED_HERE)
        monkeypatch.setattr("sys.argv", ["generate_sbom", "--write"])

        assert generate_sbom.main() == 1
        assert "refusing to overwrite" in capsys.readouterr().err
        assert sbom.read_text(encoding="utf-8") == _COMMITTED, "the committed file must survive"

    def test_force_is_the_documented_escape_hatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Refusing outright would leave no way to regenerate inside a non-Linux container."""
        sbom = _sandbox(monkeypatch, tmp_path, _RENDERED_HERE)
        monkeypatch.setattr("sys.argv", ["generate_sbom", "--write", "--force"])

        assert generate_sbom.main() == 0
        assert sbom.read_text(encoding="utf-8") == _RENDERED_HERE


class TestOnReferencePlatform:
    """CI's behaviour is unchanged — the guard must not weaken the gate where it applies."""

    @pytest.fixture(autouse=True)
    def _on_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(generate_sbom, "_on_reference_platform", lambda: True)

    def test_a_difference_still_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _sandbox(monkeypatch, tmp_path, _RENDERED_HERE)
        monkeypatch.setattr("sys.argv", ["generate_sbom"])

        assert generate_sbom.main() == 1
        assert "OUT OF DATE" in capsys.readouterr().err

    def test_a_match_passes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _sandbox(monkeypatch, tmp_path, _COMMITTED)
        monkeypatch.setattr("sys.argv", ["generate_sbom"])

        assert generate_sbom.main() == 0
        assert "up to date" in capsys.readouterr().out

    def test_write_needs_no_force_here(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sbom = _sandbox(monkeypatch, tmp_path, _RENDERED_HERE)
        monkeypatch.setattr("sys.argv", ["generate_sbom", "--write"])

        assert generate_sbom.main() == 0
        assert sbom.read_text(encoding="utf-8") == _RENDERED_HERE
