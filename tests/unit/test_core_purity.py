"""Guard: ``agentiam-core`` performs no I/O and reads no clock.

Every correctness claim AgentIAM makes — the ten attenuation invariants, the budget
arithmetic, the decision pipeline — is a claim about ``agentiam_core``. Those claims only
hold if the package is deterministic and testable in isolation: no network, no database,
no cache, no filesystem, no subprocesses, and no wall-clock reads. The clock is injected.

This is ``PLAN.md`` §5's hard rule and ``ENGINEERING-RULES.md`` rule 3.

The check is **static**, walking the AST of every source file rather than importing the
package. A runtime import check would miss a lazily-imported violation sitting inside a
function body, which is exactly where such a violation would realistically appear.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_SRC = Path(__file__).resolve().parents[2] / "packages" / "agentiam-core" / "src"

#: Top-level modules ``agentiam_core`` may not import, by category.
#:
#: Add to this set rather than weakening it. If a genuine need arises, that is an ADR in
#: ``docs/DECISIONS.md``, not a quiet edit here.
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        # network
        "aiohttp",
        "http",
        "httpx",
        "requests",
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "websockets",
        # database
        "alembic",
        "asyncpg",
        "psycopg",
        "psycopg2",
        "sqlalchemy",
        "sqlite3",
        # cache / messaging
        "aioredis",
        "kafka",
        "redis",
        # process / filesystem / environment
        "os",
        "pathlib",
        "shutil",
        "subprocess",
        "tempfile",
        # web frameworks — core is not a service
        "fastapi",
        "starlette",
        "uvicorn",
        # clock
        "time",
    }
)

#: Attribute calls that read the wall clock. ``datetime.now()``, ``time.monotonic()``,
#: and friends. The clock is an injected dependency, never ambient.
FORBIDDEN_CLOCK_ATTRS: frozenset[str] = frozenset(
    {
        "monotonic",
        "monotonic_ns",
        "now",
        "perf_counter",
        "perf_counter_ns",
        "time_ns",
        "today",
        "utcnow",
    }
)


def _core_source_files() -> list[Path]:
    """Return every Python source file in ``agentiam-core``."""
    return sorted(CORE_SRC.rglob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    """Return the top-level module name of every import in ``tree``.

    Covers both ``import x.y`` and ``from x.y import z``. Relative imports are skipped:
    they cannot reach outside the package.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module is not None:
                roots.add(node.module.split(".")[0])
    return roots


def _clock_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Return ``(expression, lineno)`` for every wall-clock read in ``tree``."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_CLOCK_ATTRS:
            found.append((f"{ast.unparse(func)}()", node.lineno))
    return found


def test_core_source_tree_exists() -> None:
    """The purity checks below are vacuous if they scan nothing."""
    files = _core_source_files()
    assert CORE_SRC.is_dir(), f"agentiam-core source tree not found at {CORE_SRC}"
    assert files, "no Python files found in agentiam-core — the purity check would pass vacuously"


@pytest.mark.parametrize("path", _core_source_files(), ids=lambda p: p.name)
def test_core_imports_no_io_library(path: Path) -> None:
    """No file in ``agentiam-core`` imports a network, database, or filesystem library."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = sorted(_imported_roots(tree) & FORBIDDEN_MODULES)
    assert not violations, (
        f"{path.relative_to(CORE_SRC)} imports {violations}. "
        f"agentiam-core must stay I/O-free (PLAN.md §5, ENGINEERING-RULES.md rule 3). "
        f"If this is genuinely necessary, write the ADR first."
    )


@pytest.mark.parametrize("path", _core_source_files(), ids=lambda p: p.name)
def test_core_reads_no_clock(path: Path) -> None:
    """No file in ``agentiam-core`` reads the wall clock. The clock is injected."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _clock_calls(tree)
    rendered = ", ".join(f"{expr} at line {line}" for expr, line in violations)
    assert not violations, (
        f"{path.relative_to(CORE_SRC)} reads the clock: {rendered}. "
        f"Inject the clock instead (PLAN.md §5, ENGINEERING-RULES.md rule 3)."
    )


# ---------------------------------------------------------------------------
# Self-tests for the detectors above.
#
# A guard that has never been shown to fire is not a guard. These feed known-bad source
# to the detectors and assert they catch it, so the checks above cannot silently rot into
# passing on everything.
# ---------------------------------------------------------------------------

_VIOLATIONS: list[tuple[str, str]] = [
    ("top-level import", "import redis"),
    ("dotted import", "import sqlalchemy.orm"),
    ("from-import", "from httpx import AsyncClient"),
    ("aliased import", "import httpx as _client"),
    ("environment access", "import os"),
    ("filesystem access", "from pathlib import Path"),
    (
        "lazy import inside a function",
        "def fetch() -> None:\n    import httpx\n    del httpx\n",
    ),
    (
        "import inside a class body",
        "class Store:\n    import redis\n",
    ),
    (
        "conditional import",
        "if True:\n    import psycopg\n",
    ),
]


@pytest.mark.parametrize(("label", "source"), _VIOLATIONS, ids=[v[0] for v in _VIOLATIONS])
def test_import_detector_catches_violation(label: str, source: str) -> None:
    """Each known-bad import form is detected, including lazy and conditional ones."""
    roots = _imported_roots(ast.parse(source))
    assert roots & FORBIDDEN_MODULES, f"{label} slipped past the import detector: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\nx = datetime.now()",
        "from datetime import datetime\nx = datetime.utcnow()",
        "import datetime\nx = datetime.date.today()",
        "def f():\n    import time\n    return time.monotonic()",
    ],
)
def test_clock_detector_catches_violation(source: str) -> None:
    """Wall-clock reads are detected wherever they appear."""
    assert _clock_calls(ast.parse(source)), f"clock read slipped past the detector: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        "from decimal import Decimal\nx = Decimal('1.0')",
        "from datetime import datetime\ndef f(clock: datetime) -> datetime:\n    return clock",
        "from . import models",
        "import dataclasses\nimport enum\nimport uuid",
    ],
)
def test_detectors_accept_pure_code(source: str) -> None:
    """Legitimate pure code is not flagged.

    Notably: importing ``datetime`` for type annotations is fine — it is *calling*
    ``.now()`` that is forbidden. A guard that fires on correct code gets disabled.
    """
    tree = ast.parse(source)
    assert not (_imported_roots(tree) & FORBIDDEN_MODULES)
    assert not _clock_calls(tree)
