"""The live decision stream's pure half — T-046.

`explain()` is where this ticket's third acceptance criterion lives: *a denial shows the
exact failing caveat inline, not a generic message.* "Denied by policy" is what every other
IAM product says, and is the thing this project exists not to do — so the tests that matter
are the ones asserting a refusal names its cause.

The SQL half (`read_since`) is exercised in `tests/integration/test_decision_stream.py`,
because filtering happens in Postgres' JSONB operators and a fake would prove nothing about
them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from agentiam_controlplane.db.decisions import DecisionFilter, explain, project_record
from agentiam_controlplane.db.models import AuditRecordRow


def a_record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "outcome": "deny",
        "reason_code": "BUDGET_EXHAUSTED_CAVEAT",
        "reason_detail": "spend_bdt 60000 exceeds the caveat ceiling 50000",
    }
    return base | over


class TestExplainNamesTheCause:
    def test_a_caveat_refusal_names_the_kind_and_the_block(self) -> None:
        # The criterion, stated as a test: an operator reading the stream can see *which*
        # caveat in *which* block of the chain refused the call.
        sentence = explain(
            a_record(
                failing_caveat={
                    "kind": "budget_ceiling",
                    "block_index": 2,
                    "detail": "spend_bdt 60000 exceeds 50000",
                }
            )
        )
        assert "budget_ceiling" in sentence
        assert "block 2" in sentence
        assert "60000" in sentence

    def test_the_caveats_own_detail_wins_over_the_records(self) -> None:
        # The caveat knows more about itself than the pipeline's summary does.
        sentence = explain(
            a_record(
                reason_detail="generic summary",
                failing_caveat={"kind": "tool_deny", "block_index": 1, "detail": "tool_x denied"},
            )
        )
        assert "tool_x denied" in sentence
        assert "generic summary" not in sentence

    def test_a_caveat_without_detail_borrows_the_records(self) -> None:
        sentence = explain(
            a_record(
                reason_detail="spend_bdt 60000 exceeds the caveat ceiling 50000",
                failing_caveat={"kind": "budget_ceiling", "block_index": 0},
            )
        )
        assert "budget_ceiling" in sentence
        assert "60000" in sentence

    def test_a_missing_block_index_still_explains(self) -> None:
        # Degrade to naming the chain rather than printing "block None".
        sentence = explain(a_record(failing_caveat={"kind": "time_window"}))
        assert "time_window" in sentence
        assert "None" not in sentence

    def test_a_policy_refusal_falls_back_to_the_detail(self) -> None:
        # Policy denials have no caveat — the cause is a Cedar statement, and the pipeline
        # already names it.
        sentence = explain(
            a_record(
                reason_code="POLICY_DENIED",
                reason_detail="denied by policy statement no_external_email",
            )
        )
        assert "no_external_email" in sentence

    def test_a_refusal_is_never_explained_as_nothing(self) -> None:
        # The failure mode this function exists to prevent. Even a record with no detail
        # and no caveat yields the reason code rather than an empty cell.
        assert explain({"outcome": "deny", "reason_code": "TOKEN_EXPIRED"}) == "TOKEN_EXPIRED"
        assert explain({"outcome": "deny"}).strip() != ""

    def test_an_allow_reads_as_allowed(self) -> None:
        assert explain({"outcome": "allow", "reason_code": "OK"}) == "allowed"

    def test_a_non_dict_caveat_is_ignored_rather_than_crashing(self) -> None:
        # The stream renders whatever the chain holds; one malformed record must not break
        # the feed for every well-formed one after it.
        sentence = explain(a_record(failing_caveat="budget_ceiling"))
        assert "exceeds" in sentence


class TestFilterSemantics:
    def test_no_filter_is_not_the_same_as_an_empty_filter(self) -> None:
        # `?agent_id=` in a URL arrives as "", and treating that as "no filter" would show
        # an operator everything at the moment they asked for one agent.
        assert not DecisionFilter().matches_nothing()
        assert DecisionFilter(agent_id="").matches_nothing()
        assert DecisionFilter(outcome="").matches_nothing()
        assert DecisionFilter(scope="").matches_nothing()

    def test_a_populated_filter_matches_something(self) -> None:
        assert not DecisionFilter(outcome="deny", agent_id="agt-1").matches_nothing()


class TestProjectRecordCarriesTaskId:
    """T-048 needs `task_id` on `DecisionEvent` to link a search hit to its custody view."""

    def _row(self, **record_over: Any) -> AuditRecordRow:
        record: dict[str, Any] = a_record(**record_over)
        record.setdefault("agent_id", "agt-1")
        return AuditRecordRow(
            seq=1,
            decision_id=uuid.uuid4(),
            record=record,
            prev_hash=None,
            record_hash="0" * 64,
            created_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        )

    def test_a_valid_task_id_round_trips(self) -> None:
        task = uuid.uuid4()
        event = project_record(self._row(task_id=str(task)))
        assert event.task_id == task

    def test_a_missing_task_id_is_none_rather_than_a_crash(self) -> None:
        event = project_record(self._row())
        assert event.task_id is None

    def test_a_malformed_task_id_is_none_rather_than_a_crash(self) -> None:
        # Deliberately forgiving, same as every other field `project_record` projects: a
        # malformed record must render as best it can, not break the page for every
        # well-formed row after it.
        event = project_record(self._row(task_id="not-a-uuid"))
        assert event.task_id is None
