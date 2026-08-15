"""The revocation set the PEP consults at step 3 — T-023, `PLAN.md` §6.7.

**This is not a stub, and the distinction matters.** An empty revocation set is not a
pretend answer: nothing in this system can revoke anything until T-038 builds the revocation
service, so *no token is revoked* is the true state of the world, and reporting it is honest.

Compare with what T-023 deliberately did **not** ship: an allow-all `PolicyEngine` would have
reported that policy was evaluated when no policy existed (ADR-027). The difference is whether
the component is telling the truth about work it did, and this one is.

What T-038 replaces is the *source*: Redis pub/sub for the fast path plus a periodic full-set
pull as a correctness backstop, and a Bloom filter in front of the exact set (T-039). The
`RevocationOracle` shape does not change — `is_revoked(revocation_id) -> bool`, in memory, on
the hot path, no network. That is why `decide()` was written against a protocol.

`OracleUnavailable` exists here for the same reason: once T-038 lands, a PEP whose revocation
data is too old must say so rather than answer from a stale set, and `decide()` already turns
that into `CONTROL_PLANE_UNAVAILABLE_FAIL_CLOSED`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentiam_core.decision import OracleUnavailable

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["InMemoryRevocationSet"]


class InMemoryRevocationSet:
    """A `RevocationOracle` over a set held in this process.

    Consulted on every decision, so membership is a set lookup and nothing else. INV-10 (no
    resurrection) is enforced by `decide()`, which checks every id in the chain and treats a
    revoked *ancestor* as fatal — this class only answers about one id at a time.
    """

    def __init__(self, revoked: Iterable[str] = (), *, unavailable: str | None = None) -> None:
        """Build the set. Empty is the normal state until T-038.

        Args:
            revoked: Block revocation ids already known to be revoked.
            unavailable: If set, every lookup raises `OracleUnavailable` with this reason —
                the shape T-038 needs when its data is too stale to answer from.
        """
        self._revoked = set(revoked)
        self._unavailable = unavailable

    def is_revoked(self, revocation_id: str) -> bool:
        """Whether this block id has been revoked.

        Raises:
            OracleUnavailable: The set cannot be trusted. `decide()` fails closed on this.
        """
        if self._unavailable is not None:
            raise OracleUnavailable(self._unavailable)
        return revocation_id in self._revoked

    def revoke(self, revocation_id: str) -> None:
        """Mark a block id revoked.

        Present so T-023's slice and the demo's Beat 7 can exercise the deny path before the
        revocation service exists. T-038 replaces the *source* of these ids, not this shape.
        """
        self._revoked.add(revocation_id)

    def __len__(self) -> int:
        """How many ids are known revoked — what `/readyz` reports."""
        return len(self._revoked)
