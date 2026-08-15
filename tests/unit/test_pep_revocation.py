"""The revocation set — T-023, step 3.

Small, and worth its own module for one reason: an empty revocation set is **not** a stub.
Nothing in this system can revoke anything until T-038, so *no token is revoked* is the true
state of the world. Compare with the allow-all policy engine T-023 deliberately did not ship
(ADR-027) — that one would have reported work it did not do.
"""

from __future__ import annotations

import pytest

from agentiam_core.decision import OracleUnavailable
from agentiam_pep.revocation import InMemoryRevocationSet


class TestLookups:
    def test_an_empty_set_revokes_nothing(self) -> None:
        assert not InMemoryRevocationSet().is_revoked("blk_1")

    def test_a_known_id_is_revoked(self) -> None:
        assert InMemoryRevocationSet(["blk_1"]).is_revoked("blk_1")

    def test_an_unknown_id_is_not(self) -> None:
        assert not InMemoryRevocationSet(["blk_1"]).is_revoked("blk_2")

    def test_revoking_takes_effect_immediately(self) -> None:
        """Demo Beat 7 flips this live, on stage."""
        oracle = InMemoryRevocationSet()
        assert not oracle.is_revoked("blk_1")
        oracle.revoke("blk_1")
        assert oracle.is_revoked("blk_1")

    def test_revoking_twice_is_idempotent(self) -> None:
        oracle = InMemoryRevocationSet()
        oracle.revoke("blk_1")
        oracle.revoke("blk_1")
        assert len(oracle) == 1


class TestUnavailable:
    """The shape T-038 needs when its data is too stale to answer from."""

    def test_an_unavailable_set_raises_rather_than_answering(self) -> None:
        """`decide()` turns this into CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED.

        Answering "not revoked" from a set known to be stale is the one wrong reply: it is
        indistinguishable from a correct answer and it fails open.
        """
        oracle = InMemoryRevocationSet(unavailable="gossip stream is 40 s behind")
        with pytest.raises(OracleUnavailable, match="40 s behind"):
            oracle.is_revoked("blk_1")

    def test_an_available_set_does_not_raise(self) -> None:
        assert InMemoryRevocationSet().is_revoked("blk_1") is False


class TestSize:
    def test_length_reports_what_is_held(self) -> None:
        """`/readyz` reports this, so an operator can see the set is populated at all."""
        assert len(InMemoryRevocationSet(["a", "b"])) == 2
