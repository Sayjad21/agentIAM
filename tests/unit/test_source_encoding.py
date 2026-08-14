"""Every text file in the repo is clean UTF-8, with no mojibake.

This project carries Bengali company names, taka amounts, and box-drawing diagrams, and
EC-T16 requires that non-ASCII survives the whole pipeline. A tool that reads a file as
ANSI and writes it back as UTF-8 replaces each multi-byte character with one visually
confusable character per byte. The result still parses, still passes every other test,
and has quietly destroyed the text.

It happened once in this repo, to a spec and a test module, and needed the encoding layer
peeled back off byte by byte. This test is why it will not happen twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

TEXT_SUFFIXES = frozenset({".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".txt"})

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        "htmlcov",
        "node_modules",
        "dist",
        "build",
    }
)

#: Sequences that a mis-decoded UTF-8 lead byte produces. Written as escapes, not literal
#: characters, for two reasons: the literals are by their nature visually confusable, and
#: spelling them out would make this file fail its own check.
#:
#: None of these appear legitimately in this codebase, which is English prose plus Bengali
#: plus box-drawing characters.
MOJIBAKE_MARKERS: tuple[str, ...] = (
    "\u00c3",  # 0xC3 read as Latin-1: an accented letter becomes two characters
    "\u00c2",  # 0xC2: the section sign gains a stray prefix
    "\u00e2\u20ac",  # start of a mangled em dash or curly quote
    "\u00e0\u00a6",  # start of mangled Bengali (U+0980 block)
    "\u00e0\u00a7",  # start of mangled Bengali, e.g. the taka sign
)


def _text_files() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in TEXT_SUFFIXES
        and not any(part in SKIP_DIRS for part in path.parts)
    )


def test_repo_has_text_files_to_scan() -> None:
    """Guard against the scan silently matching nothing."""
    assert len(_text_files()) > 10


@pytest.mark.parametrize("path", _text_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_file_is_valid_utf8(path: Path) -> None:
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - only on a real regression
        pytest.fail(f"{path.relative_to(REPO_ROOT)} is not valid UTF-8: {exc}")


@pytest.mark.parametrize("path", _text_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_file_has_no_mojibake(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = sorted({m for m in MOJIBAKE_MARKERS if m in text})
    assert not found, (
        f"{path.relative_to(REPO_ROOT)} contains mojibake {found}. "
        f"Something read it as ANSI and wrote it back as UTF-8. Repair it by peeling the "
        f"encoding layer rather than hand-editing the visible characters."
    )
