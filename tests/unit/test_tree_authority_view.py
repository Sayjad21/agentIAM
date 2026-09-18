"""`_authority_view` — what the identity tree's node panel renders.

The panel is opened by clicking an agent, and it answered "what may this agent do" while
never answering "whose authority is it doing it under". `principal_id` was on every decision
record and on every tree node, and this projection dropped it — so the console could name the
human in the audit drawer and not on the screen an operator actually clicks.

These tests are unit, not integration: the function folds a sequence of rows and touches no
database, so a stub row carrying `.record` exercises it exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agentiam_controlplane.tree_api import _authority_view


@dataclass
class StubRow:
    """Just the one attribute `_authority_view` reads off an `AuditRecordRow`."""

    record: dict[str, Any] = field(default_factory=dict)


def a_row(**over: Any) -> StubRow:
    base: dict[str, Any] = {
        "principal_id": "kc:alice",
        "role": "payer",
        "depth": 1,
        "budget_before": {"spend_bdt": "20000"},
        "budget_after": {"spend_bdt": "19000"},
        "authority": {
            "scopes": ["payment:initiate"],
            "ceilings": {"spend_bdt": "20000"},
            "not_after": "2026-09-19T01:06:53+00:00",
            "max_depth": 4,
        },
    }
    return StubRow(record=base | over)


class TestItNamesTheHuman:
    def test_the_panel_view_carries_the_principal(self) -> None:
        view = _authority_view([a_row()])
        assert view is not None
        assert view.principal_id == "kc:alice"

    def test_the_principal_comes_from_the_newest_record(self) -> None:
        """Rows arrive newest-first, and the panel describes the current state."""
        view = _authority_view([a_row(principal_id="kc:bob"), a_row(principal_id="kc:alice")])
        assert view is not None
        assert view.principal_id == "kc:bob"

    def test_a_record_without_a_principal_is_none_not_empty_string(self) -> None:
        """An absent principal must read as unknown, not as a blank name."""
        row = a_row()
        del row.record["principal_id"]
        view = _authority_view([row])
        assert view is not None
        assert view.principal_id is None

    def test_naming_the_human_did_not_disturb_the_rest_of_the_fold(self) -> None:
        view = _authority_view([a_row()])
        assert view is not None
        assert view.role == "payer"
        assert view.depth == 1
        assert view.scopes == ["payment:initiate"]
        assert view.ceilings == {"spend_bdt": Decimal("20000")}
        assert view.spent == Decimal("1000")
        assert view.decisions == 1
