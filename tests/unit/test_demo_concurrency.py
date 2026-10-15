"""`scripts/demo_concurrency.py` — the sibling race, shown live.

`make demo-seed` sends one call at a time to one PEP, and its payments never approach the
pool, so it cannot show INV-5 at all. This scenario exists to show exactly that: three
siblings, each holding a token that on its own allows the whole pool, spending at once
through three separate PEPs that share one ledger.

What these tests protect is that the scenario still *can* show it. Each constraint below is
one a plausible edit would break without anything failing loudly — a pool the PEPs drain at
boot races nothing during the drive; an amount that does not divide the lease strands budget
and the total can never reach the pool; a sibling without a senior role is refused by the
critical-resource forbid, which looks like enforcement and demonstrates nothing.

Nothing here talks to Postgres or a PEP. The live half is `make demo-conc` against the demo
stack, which prints the ledger's own figures next to the driver's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import demo_concurrency as conc

REPO_ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (REPO_ROOT / "docker-compose.demo.yml").read_text(encoding="utf-8")
    )
    return loaded


class TestTheScenarioShape:
    def test_there_are_three_siblings_each_behind_its_own_pep(self) -> None:
        agents = [agent for agent, _ in conc.SIBLINGS]
        peps = [pep for _, pep in conc.SIBLINGS]
        assert len(conc.SIBLINGS) == 3
        assert len(set(agents)) == 3
        # One PEP per sibling: a single PEP would serialize the race in its own process and
        # prove nothing about the ledger — the point `test_sibling_budgets.py` makes too.
        assert len(set(peps)) == 3

    def test_each_sibling_alone_is_within_its_token(self) -> None:
        # INV-5's shape: nothing about any single token is wrong. Each sibling's own demand
        # fits its own ceiling, and each single payment does too.
        assert conc.SIBLING_CEILING == conc.POOL_TOTAL
        assert conc.PAYMENT <= conc.SIBLING_CEILING
        assert conc.PAYMENTS_PER_SIBLING * conc.PAYMENT <= conc.SIBLING_CEILING

    def test_together_they_ask_for_more_than_the_pool(self) -> None:
        # Otherwise nothing is refused and the demo shows an allow-list.
        assert conc.requested_total() > conc.POOL_TOTAL
        assert conc.requested_total() == (
            len(conc.SIBLINGS) * conc.PAYMENTS_PER_SIBLING * conc.PAYMENT
        )

    def test_the_race_happens_during_the_drive_not_at_boot(self) -> None:
        # Each PEP primes one lease at start-up. If those three leases already cover the
        # pool, the ledger serialized everything before the first payment was sent.
        assert conc.POOL_TOTAL > len(conc.SIBLINGS) * conc.LEASE_SIZE

    def test_amounts_divide_evenly_so_the_pool_can_be_spent_exactly(self) -> None:
        # A lease left holding less than one payment can never be spent, and the total would
        # stop short of the pool for a reason that has nothing to do with concurrency.
        assert conc.PAYMENT <= conc.LEASE_SIZE
        assert conc.LEASE_SIZE % conc.PAYMENT == 0
        assert conc.POOL_TOTAL % conc.PAYMENT == 0


class TestClassify:
    def test_a_success_is_allowed(self) -> None:
        assert conc.classify(200, "OK") == "allowed"

    def test_an_empty_lease_is_retried(self) -> None:
        # The PEP refuses while its top-up is in flight, then succeeds. An agent retries.
        assert conc.classify(429, "LEASE_UNAVAILABLE") == "retry"

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (429, "BUDGET_EXHAUSTED_MANDATE"),
            (429, "BUDGET_EXHAUSTED_CAVEAT"),
            (403, "POLICY_DENY"),
            (401, "TOKEN_INVALID"),
        ],
    )
    def test_anything_else_is_final(self, status: int, reason: str) -> None:
        assert conc.classify(status, reason) == "final"


class TestSummary:
    def _summary(self, allowed: dict[str, int]) -> conc.Summary:
        return conc.Summary(
            allowed_payments=allowed,
            final_refusals={"BUDGET_EXHAUSTED_MANDATE": 3},
            retries=7,
            gave_up=0,
            elapsed_s=0.4,
        )

    def test_the_total_is_payments_times_the_amount(self) -> None:
        summary = self._summary({"a": 10, "b": 12, "c": 8})
        assert summary.total_allowed == 30 * conc.PAYMENT

    def test_a_total_inside_the_pool_holds(self) -> None:
        summary = self._summary({"a": 10, "b": 12, "c": 8})
        assert summary.total_allowed == conc.POOL_TOTAL
        assert summary.held

    def test_a_total_over_the_pool_is_reported_as_a_violation(self) -> None:
        # The renderer must be able to say the bad thing, or a green line proves nothing.
        summary = self._summary({"a": 20, "b": 12, "c": 8})
        assert not summary.held
        assert "VIOLATED" in conc.render(summary, conc.SIBLINGS, gauge=None)

    def test_the_rendering_names_the_pool_and_each_sibling(self) -> None:
        summary = self._summary({agent: 10 for agent, _ in conc.SIBLINGS})
        text = conc.render(summary, conc.SIBLINGS, gauge=None)
        assert "15,000" in text
        for agent, pep in conc.SIBLINGS:
            assert agent in text
            assert pep in text

    def test_the_ledger_view_is_shown_when_there_is_one(self) -> None:
        summary = self._summary({agent: 10 for agent, _ in conc.SIBLINGS})
        gauge = {
            "total": "15000.0000",
            "committed": "15000.0000",
            "leased": "0.0000",
            "available": "0.0000",
        }
        text = conc.render(summary, conc.SIBLINGS, gauge=gauge, invariants_ok=True)
        assert "committed" in text
        assert "invariants OK" in text


class TestRunState:
    def test_a_fresh_run_gets_new_ids(self, tmp_path: Path) -> None:
        first = conc.resolve_ids(tmp_path, new=True)
        conc.write_state(tmp_path, first)
        second = conc.resolve_ids(tmp_path, new=True)
        assert first.mandate_id != second.mandate_id
        assert first.task_id != second.task_id

    def test_without_new_the_existing_run_is_reused(self, tmp_path: Path) -> None:
        # `docker compose --profile conc up` re-runs the one-shot seed. Minting a new mandate
        # there would leave the running PEPs bound to the old one.
        first = conc.resolve_ids(tmp_path, new=True)
        conc.write_state(tmp_path, first)
        assert conc.resolve_ids(tmp_path, new=False) == first

    def test_without_new_and_without_state_a_run_is_created(self, tmp_path: Path) -> None:
        ids = conc.resolve_ids(tmp_path, new=False)
        assert isinstance(ids.mandate_id, uuid.UUID)

    def test_the_pep_reads_the_mandate_id_from_a_plain_file(self, tmp_path: Path) -> None:
        ids = conc.resolve_ids(tmp_path, new=True)
        conc.write_state(tmp_path, ids)
        mandate_file = tmp_path / conc.STATE_DIR / conc.MANDATE_ID_FILE
        assert mandate_file.read_text(encoding="utf-8") == str(ids.mandate_id)


class TestTokens:
    def test_every_sibling_token_is_real_and_names_its_agent(self, tmp_path: Path) -> None:
        """Read back the way the PEP reads it."""
        from biscuit_auth import Algorithm, PublicKey

        from agentiam_core.datalog import token_caveats, token_identity
        from agentiam_core.models import BudgetCeiling
        from agentiam_core.tokens import RootKeySet, verify
        from scripts import bootstrap_demo_secrets

        bootstrap_demo_secrets.main(["--out", str(tmp_path)])
        now = datetime.now(UTC)
        ids = conc.resolve_ids(tmp_path, new=True)
        tokens = conc.mint_tokens(tmp_path, ids, now)
        public = PublicKey.from_bytes(  # type: ignore[call-arg]
            bytes.fromhex((tmp_path / "root_public_key.hex").read_text(encoding="utf-8")),
            Algorithm.Ed25519,  # type: ignore[attr-defined]
        )
        key_set = RootKeySet([public])

        assert set(tokens) == {agent for agent, _ in conc.SIBLINGS}
        for agent, token in tokens.items():
            verified = verify(token, key_set, now=now)
            assert verified.mandate_id == ids.mandate_id
            assert token_identity(verified).agent_id == agent
            ceilings = [c for c in token_caveats(verified) if isinstance(c, BudgetCeiling)]
            assert [c.value for c in ceilings] == [conc.SIBLING_CEILING]

    def test_every_sibling_is_senior(self) -> None:
        # `payment_api` is critical and the corpus forbids critical resources to non-seniors.
        # Without this every payment is refused by policy — enforcement, but not this one.
        from scripts.bootstrap_demo_secrets import ROLE_ASSIGNMENTS

        for agent, _ in conc.SIBLINGS:
            assert ROLE_ASSIGNMENTS.get(agent) == "senior", agent


class TestCompose:
    def test_the_concurrency_services_are_opt_in(self) -> None:
        services = _compose()["services"]
        for name in [conc.SEED_SERVICE, *(pep for _, pep in conc.SIBLINGS)]:
            assert services[name].get("profiles") == ["conc"], name

    def test_each_pep_has_its_own_id(self) -> None:
        services = _compose()["services"]
        ids = {services[pep]["environment"]["AGENTIAM_PEP_ID"] for _, pep in conc.SIBLINGS}
        assert len(ids) == len(conc.SIBLINGS)
        assert "pep-1" not in ids

    def test_each_pep_leases_the_size_the_scenario_is_built_on(self) -> None:
        services = _compose()["services"]
        for _, pep in conc.SIBLINGS:
            size = services[pep]["environment"]["AGENTIAM_PEP_LEASE_SIZE"]
            assert Decimal(size) == conc.LEASE_SIZE, pep

    def test_each_pep_binds_the_mandate_the_seed_wrote(self) -> None:
        services = _compose()["services"]
        path = f"/secrets/{conc.STATE_DIR}/{conc.MANDATE_ID_FILE}"
        for _, pep in conc.SIBLINGS:
            assert "AGENTIAM_PEP_MANDATE_ID" not in services[pep]["environment"], pep
            assert f"AGENTIAM_PEP_MANDATE_ID=$$(cat {path})" in services[pep]["command"], pep

    def test_each_pep_waits_for_the_seed(self) -> None:
        services = _compose()["services"]
        for _, pep in conc.SIBLINGS:
            gate = services[pep]["depends_on"][conc.SEED_SERVICE]["condition"]
            assert gate == "service_completed_successfully", pep

    def test_the_main_pep_is_untouched(self) -> None:
        pep = _compose()["services"]["pep"]
        assert "profiles" not in pep
        assert pep["environment"]["AGENTIAM_PEP_ID"] == "pep-1"


class TestMakeTargets:
    def test_both_make_files_have_the_target(self) -> None:
        # ADR-003: a target added to one and not the other is a real drift risk.
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        ps1 = (REPO_ROOT / "make.ps1").read_text(encoding="utf-8")
        assert "\ndemo-conc:" in makefile
        assert "'demo-conc'" in ps1
