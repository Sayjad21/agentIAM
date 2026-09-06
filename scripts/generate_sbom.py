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

**Regenerate this only from a fresh ``uv sync`` on Linux x86_64 (matching CI's
``ubuntu-latest``), not from an arbitrary local venv or OS.** Measured directly, twice,
after this file first shipped with a check that had actually been silently broken for
three tickets: (1) a long-lived local venv can be missing ``cdx:python:package:required-
extra`` properties a genuinely fresh install has, for reasons not fully root-caused —
confirmed present in both a from-scratch Ubuntu container and this repository's own
``pyproject.toml``/``uv.lock`` inputs, confirmed *absent* even after deleting and
rebuilding a local ``.venv`` on Fedora; the difference tracks the host OS, not venv
staleness. (2) Two *independent* fresh Ubuntu containers, same lock file, produced
byte-identical SBOMs after this file's sorting fix — so the committed file only needs to
match *one specific, reproducible-within-itself* environment, and that environment is
whatever CI actually runs, not whatever happens to be closest at hand locally. Reproduce
with a throwaway container rather than guessing from a local run:
``docker run --rm -v $(pwd):/repo:ro,z ubuntu:latest`` — install ``uv``, ``uv sync``,
run this script with ``--write``, copy the result out.

**Off that platform, this script does not pretend to have checked anything.** It used to:
a Windows run reported ``OUT OF DATE. Re-run with --write and commit the update``, and both
halves were false — the file was current, and following the instruction committed a
137-component Windows SBOM (``colorama``, ``pywin32``) over the 136-component Linux one
(``uvloop``), which CI's security-scan job then rejects. So the check now reports
``NOT CHECKED`` and exits 0 (CI is the authority, the same way ``make security`` already
says the security-scan job is for trivy and gitleaks), and ``--write`` refuses outright
unless ``--force`` says the overwrite is deliberate.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SBOM_PATH = REPO_ROOT / "docs" / "evidence" / "sbom.json"

#: The one environment the committed SBOM is a function of — CI's `ubuntu-latest`. The
#: component set is resolved from the *installed* environment (see the module docstring),
#: so it legitimately differs by host: measured, 136 components with `uvloop` on Linux
#: against 137 with `colorama` and `pywin32` on Windows.
_REFERENCE_OS = "Linux"
_REFERENCE_MACHINES = frozenset({"x86_64", "AMD64", "amd64"})


def _on_reference_platform() -> bool:
    """Whether this host is the one the committed SBOM was generated on."""
    return platform.system() == _REFERENCE_OS and platform.machine() in _REFERENCE_MACHINES


def _platform_note() -> str:
    return (
        f"this host is {platform.system()}/{platform.machine()}; the committed SBOM is "
        f"generated on {_REFERENCE_OS}/x86_64 (CI's ubuntu-latest)"
    )


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

    # The five local workspace packages (editable installs) each carry a
    # `PackageSource: Local` externalReference whose `url` is `file://<absolute checkout
    # path>/packages/...` — read straight from the editable install's `direct_url.json`.
    # That path is wherever *this* checkout happens to live, so it can never match
    # between two different clones — confirmed by diffing a fresh install in a throwaway
    # container against this machine's long-lived venv: identical packages, identical
    # versions, and the only difference was this absolute path. Not reproducible
    # information and not useful evidence in a submitted SBOM; drop any `file://`
    # externalReference, and drop the now-possibly-empty key entirely rather than leave
    # `"externalReferences": []`, matching how the tool omits the key when there was
    # never one to report.
    for component in parsed.get("components", []):
        refs = [
            ref
            for ref in component.get("externalReferences", [])
            if not ref.get("url", "").startswith("file://")
        ]
        if refs:
            component["externalReferences"] = refs
        else:
            component.pop("externalReferences", None)

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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --write off the reference platform. Almost certainly not what you want.",
    )
    args = parser.parse_args()

    rendered = _generate_sbom()
    parsed = json.loads(rendered)
    print(
        f"SBOM: CycloneDX {parsed.get('specVersion', '?')}, "
        f"{len(parsed.get('components', []))} components"
    )

    SBOM_PATH.parent.mkdir(parents=True, exist_ok=True)

    if args.write and not args.force and not _on_reference_platform() and SBOM_PATH.exists():
        # The dangerous path, and the one the old failure message actively invited: a
        # developer on Windows saw "Re-run with --write and commit the update", did
        # exactly that, and committed an SBOM that CI's security-scan job rejects.
        print(
            f"refusing to overwrite {SBOM_PATH.relative_to(REPO_ROOT)}: {_platform_note()}.\n"
            "Regenerate in a throwaway container (see this module's docstring), or pass "
            "--force if you genuinely mean to commit this host's environment.",
            file=sys.stderr,
        )
        return 1

    if args.write or not SBOM_PATH.exists():
        SBOM_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {SBOM_PATH.relative_to(REPO_ROOT)}")
        return 0

    committed = SBOM_PATH.read_text(encoding="utf-8")
    if committed == rendered:
        print(f"{SBOM_PATH.relative_to(REPO_ROOT)}: up to date")
        return 0

    if not _on_reference_platform():
        # Neither a pass nor a failure: off the reference platform this comparison is not
        # evidence either way, so reporting "OUT OF DATE" stated a finding it had not
        # made. `make security` already positions itself as the local subset with CI
        # authoritative — this is the same posture, said out loud.
        print(
            f"{SBOM_PATH.relative_to(REPO_ROOT)}: NOT CHECKED — {_platform_note()}, so a "
            "difference here is expected and proves nothing. CI verifies this."
        )
        return 0

    print(
        f"{SBOM_PATH.relative_to(REPO_ROOT)}: OUT OF DATE.\n"
        "Re-run with --write and commit the update.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
