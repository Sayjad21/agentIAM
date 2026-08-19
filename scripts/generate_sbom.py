"""Regenerate ``docs/evidence/sbom.json`` from the resolved workspace — T-054.

The SBOM is the third-party dependency inventory the submission's evidence pack has to
carry (`PLAN.md` §14 item 8). Committed rather than only produced in CI so a judge can
open it without a workflow rerun, and so this repository's `--check` habit
(``chaos-results.md``, ``performance.md``) extends to security evidence too.

Two commands under one entry point:

* ``python scripts/generate_sbom.py`` — regenerate the file. Fails if the SBOM would
  change without ``--write``.
* ``python scripts/generate_sbom.py --write`` — regenerate and overwrite the committed
  file.

The output is CycloneDX 1.5 JSON produced by ``cyclonedx-py environment`` with
``--output-reproducible``, so ``serialNumber`` and timestamps are elided and diffs
against the committed file are meaningful. Format version and component count are
printed for the CI job summary.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SBOM_PATH = REPO_ROOT / "docs" / "evidence" / "sbom.json"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        cmd,
        check=True,
        text=True,
        capture_output=True,
    )


def _python_binary() -> str:
    """Path to the venv's Python. The SBOM must describe the same interpreter tests use."""
    candidates = [
        REPO_ROOT / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _cyclonedx_binary() -> str:
    """Path to ``cyclonedx-py``. Prefer the venv's copy so the invocation is hermetic."""
    for candidate in (
        REPO_ROOT / ".venv" / "bin" / "cyclonedx-py",
        REPO_ROOT / ".venv" / "Scripts" / "cyclonedx-py.exe",
    ):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("cyclonedx-py")
    if found is None:
        print(
            "cyclonedx-py not found (install with `uv sync`; T-054 pins it as a dev dep)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return found


def _generate_sbom() -> str:
    """Return the CycloneDX 1.5 JSON string for the current venv, in reproducible form."""
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False, encoding="utf-8") as tmp:
        out_path = Path(tmp.name)
    try:
        _run(
            [
                _cyclonedx_binary(),
                "environment",
                "--output-reproducible",
                "--sv",
                "1.5",
                "--of",
                "json",
                "-o",
                str(out_path),
                _python_binary(),
            ]
        )
        parsed = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)

    # `--output-reproducible` only strips `serialNumber`/timestamps — it does not
    # guarantee a stable *order* for the `components`/`dependencies` arrays, and
    # `sort_keys=True` below sorts each JSON object's own keys, never array element
    # order. `cyclonedx-py environment` enumerates installed packages via
    # `importlib.metadata`, whose order follows filesystem/site-packages layout — not
    # deterministic across two separately-built venvs. Measured: byte-identical output
    # across repeated runs against the *same* venv, but a real diff (component order
    # only, no content difference) between a local venv and a fresh one, which is
    # exactly what CI builds every run. Sorting both arrays explicitly closes that gap.
    parsed["components"] = sorted(parsed.get("components", []), key=lambda c: c["bom-ref"])
    parsed["dependencies"] = sorted(parsed.get("dependencies", []), key=lambda d: d["ref"])

    return json.dumps(parsed, indent=2, sort_keys=True) + "\n"


def main() -> int:
    """Regenerate the SBOM; return 0 if the committed file already matches."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Overwrite the committed SBOM. Without this, exits non-zero if it would change.",
    )
    args = parser.parse_args()

    rendered = _generate_sbom()
    parsed = json.loads(rendered)
    print(
        f"SBOM: CycloneDX {parsed.get('specVersion', '?')}, "
        f"{len(parsed.get('components', []))} components"
    )

    SBOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    if args.write or not SBOM_PATH.exists():
        SBOM_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {SBOM_PATH.relative_to(REPO_ROOT)}")
        return 0

    committed = SBOM_PATH.read_text(encoding="utf-8")
    if committed == rendered:
        print(f"{SBOM_PATH.relative_to(REPO_ROOT)}: up to date")
        return 0

    print(
        f"{SBOM_PATH.relative_to(REPO_ROOT)}: OUT OF DATE.\n"
        "Re-run with --write and commit the update.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
