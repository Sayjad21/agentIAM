"""`scripts/demo_tamper.py` — the red half of "Verify chain", shown rather than asserted.

The database half is exercised against the live demo stack; this pins the CLI, and that the
script can never be pointed at a table whose name was built at runtime.
"""

from __future__ import annotations

import inspect

from scripts import demo_tamper


class TestCli:
    def test_the_default_is_to_tamper(self) -> None:
        assert demo_tamper.build_parser().parse_args([]).undo is False

    def test_undo_is_a_flag(self) -> None:
        assert demo_tamper.build_parser().parse_args(["--undo"]).undo is True


class TestTheSql:
    def test_no_statement_is_an_f_string(self) -> None:
        """Every table and column name is literal, so nothing reaches SQL by interpolation."""
        source = inspect.getsource(demo_tamper)
        assert 'text(f"' not in source
        assert "text(f'" not in source

    def test_it_targets_the_refusal_the_seed_produces(self) -> None:
        from scripts import seed_demo

        labels = {label for label, *_ in seed_demo._TRAFFIC}
        assert "settlement agent exceeds its ceiling" in labels
        assert demo_tamper.TARGET_AGENT == "agt-settlement"
