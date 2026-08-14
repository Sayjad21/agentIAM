"""Smoke test: every workspace package is installed and importable.

Cheap, but it catches a broken ``uv sync``, a missing ``py.typed``, or a package that was
added to the workspace but never wired into ``[tool.uv.sources]`` — all of which
otherwise surface much later as a confusing import error mid-ticket.
"""

from __future__ import annotations

import importlib

import pytest

WORKSPACE_PACKAGES = [
    "agentiam_controlplane",
    "agentiam_core",
    "agentiam_demo",
    "agentiam_pep",
    "agentiam_sdk",
]


@pytest.mark.parametrize("name", WORKSPACE_PACKAGES)
def test_package_is_importable(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__version__


@pytest.mark.parametrize("name", WORKSPACE_PACKAGES)
def test_package_has_a_docstring(name: str) -> None:
    """Each package explains what it is for. ENGINEERING-RULES Definition of Done."""
    module = importlib.import_module(name)
    assert module.__doc__ and module.__doc__.strip()
