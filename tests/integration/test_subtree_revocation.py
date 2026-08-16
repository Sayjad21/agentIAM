"""Subtree revocation e2e — T-040, `PLAN.md` line 1160.

`PLAN.md`: *revoke root -> a depth-4 tree of 12 agents all fail within 2 s; a sibling
subtree is unaffected (this negative test matters -- over-revocation is also a bug);
measured propagation time recorded for the evidence pack.*

T-039 proved the cache mechanism (Bloom filter + exact set) and a basic 3-oracle
propagation number against synthetic block ids revoked one at a time. This module is the
first to build a real chain with `attenuate()` (T-011) and prove two things that were
still unverified: that a real chain's `revocation_ids` tuple actually carries ancestor ids
in the order `decide()` depends on (spec 07 §2), and that revoking one subtree's own block
id leaves a sibling subtree — sharing no common ancestor id below the root — untouched.

The tree: three independent depth-4 chains under one root mandate (`agt-a1..a4`,
`agt-b1..b4`, `agt-c1..c4`), 12 agents total, minted offline with real biscuit blocks (no
caveats added -- only the chain structure matters here, not the narrowing algebra, which
`tests/property/test_attenuation.py` already covers). Revocation runs through the real
control plane (`REVOKE`, Postgres) and a real `RedisRevocationSet` oracle (push + pull,
same as T-039's NFR-4 test), and each leaf's fate is decided by the real `decide()`
pipeline, not by inspecting the oracle's set directly -- policy and budget are stubbed to
always allow, so revocation is the only thing that can produce a deny.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import redis.asyncio as redis_asyncio

from agentiam_controlplane.app import create_app
from agentiam_controlplane.db.base import make_session_factory
from agentiam_controlplane.db.revocation_publisher import RedisRevocationPublisher
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.attenuation import attenuate
from agentiam_core.decision import BudgetVerdict, Decision, PolicyVerdict, decide
from agentiam_core.errors import ReasonCode
from agentiam_core.models import BudgetDimension, Outcome, RequestContext
from agentiam_core.tokens import RootKeySet, VerifiedToken, generate_keypair, mint_root, verify
from agentiam_pep.revocation import RedisRevocationSet
from tests.fixtures.tokens import EXPIRES_AT, NOW, a_mandate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_KEY = generate_keypair()
_KEY_SET = RootKeySet([_KEY.public_key])
_OPERATION = "invoice:read"

#: Same budget T-039's NFR-4 test measures against; T-040 is the same claim at tree scale.
_NFR4_LIMIT_S = 2.0


class _AllowPolicy:
    """A `PolicyEngine` that always permits -- revocation is what this module tests."""

    def evaluate(self, context: RequestContext) -> PolicyVerdict:
        return PolicyVerdict(allowed=True, statement="t040-allow-all", version="t040")


class _AllowBudget:
    """A `BudgetOracle` that always has room -- revocation is what this module tests."""

    def check(self, requested: Mapping[BudgetDimension, Decimal]) -> BudgetVerdict:
        return BudgetVerdict(ok=True)


def _ctx(token: VerifiedToken) -> RequestContext:
    return RequestContext(
        operation=_OPERATION,
        requested=dict.fromkeys(BudgetDimension, Decimal(0)),
        current_depth=token.depth,
        request_intent=token.intent_hash,
        now=NOW,
    )


def _mint_chain(root: VerifiedToken, label: str, depth: int) -> list[VerifiedToken]:
    """`depth` linear attenuations under `root`: `agt-{label}1` .. `agt-{label}{depth}`.

    No caveats -- only the chain's `revocation_ids` structure matters here. Narrowing
    correctness is `test_attenuation.py`'s job, not this module's.
    """
    tokens: list[VerifiedToken] = []
    parent = root
    for level in range(1, depth + 1):
        child_b64 = attenuate(parent, [], agent_id=f"agt-{label}{level}", role="worker")
        child = verify(child_b64, _KEY_SET, now=NOW)
        tokens.append(child)
        parent = child
    return tokens


@dataclass
class Tree:
    """A root mandate and three independent depth-4 subtrees -- 12 agents total."""

    root: VerifiedToken
    subtree_a: list[VerifiedToken]
    subtree_b: list[VerifiedToken]
    subtree_c: list[VerifiedToken]

    @property
    def all_leaves(self) -> list[VerifiedToken]:
        return [*self.subtree_a, *self.subtree_b, *self.subtree_c]


def _build_tree() -> Tree:
    root = verify(mint_root(a_mandate(), _KEY.private_key), _KEY_SET, now=NOW)
    return Tree(
        root=root,
        subtree_a=_mint_chain(root, "a", 4),
        subtree_b=_mint_chain(root, "b", 4),
        subtree_c=_mint_chain(root, "c", 4),
    )


async def _revoke(client: httpx.AsyncClient, block_id: str) -> None:
    response = await client.post(
        "/v1/revocations",
        json={
            "block_id": block_id,
            "scope": "subtree",
            "reason": "T-040 subtree revocation e2e",
            "revoked_by": "kc:manager",
            "expires_at": EXPIRES_AT.isoformat(),
        },
    )
    assert response.status_code == 201


async def _time_to_denied(
    oracle: RedisRevocationSet, token: VerifiedToken, deadline_s: float
) -> tuple[float, Decision]:
    """Seconds until `decide()` first denies `token`, plus the denying decision.

    Polls rather than blocks on one event, matching `test_revocation_nfr4.py`'s
    `_time_to_deny`: the id may arrive via push or the next pull tick, and which one wins
    is not this test's concern.
    """
    start = time.perf_counter()
    while True:
        decision = decide(
            token,
            _ctx(token),
            revocation=oracle,
            policy=_AllowPolicy(),
            budget=_AllowBudget(),
        )
        if decision.outcome is Outcome.DENY:
            return time.perf_counter() - start, decision
        elapsed = time.perf_counter() - start
        if elapsed >= deadline_s:
            raise AssertionError(f"{token.revocation_ids[-1]} not denied within {deadline_s}s")
        await asyncio.sleep(0.005)


class _Harness:
    """The control plane, one `RedisRevocationSet` oracle, and their clients.

    One oracle, not T-039's three: this module's job is proving the tree structure and the
    negative (sibling) case, which T-039's NFR-4 test already established holds across
    multiple PEP instances. A second oracle here would repeat that proof, not extend it.
    """

    def __init__(self, migrated_engine: AsyncEngine, redis_url: str) -> None:
        factory = make_session_factory(migrated_engine)
        self._publish_redis = redis_asyncio.Redis.from_url(redis_url)
        settings = ControlPlaneSettings(
            root_private_key=_KEY.private_key, approvers=frozenset({"kc:manager"})
        )
        app = create_app(
            session_factory=factory,
            escalation_settings=settings,
            revocation_publisher=RedisRevocationPublisher(self._publish_redis),
            now=lambda: NOW,
        )
        self.revoke_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cp"
        )
        self._subscribe_redis = redis_asyncio.Redis.from_url(redis_url)
        self._oracle_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cp"
        )
        self.oracle = RedisRevocationSet(
            redis_client=self._subscribe_redis,
            control_plane_client=self._oracle_http,
            pull_interval_s=1.0,
            staleness_limit_s=30.0,
            now=lambda: datetime.now(UTC),
        )

    async def start(self) -> None:
        await self.oracle.start()
        # Let SUBSCRIBE land before the first revoke -- a message published before anyone
        # is listening is EC-R06, proven separately in `test_revocation_consumer.py`.
        await asyncio.sleep(0.3)

    async def aclose(self) -> None:
        await self.oracle.aclose()
        await self._subscribe_redis.aclose()
        await self._oracle_http.aclose()
        await self.revoke_client.aclose()
        await self._publish_redis.aclose()


async def test_revoke_root_denies_all_twelve_agents_within_2s(
    migrated_engine: AsyncEngine, redis_url: str
) -> None:
    """`PLAN.md` T-040, verbatim: revoke root -> 12 agents across 3 subtrees all fail."""
    tree = _build_tree()
    harness = _Harness(migrated_engine, redis_url)
    try:
        await harness.start()

        root_block_id = tree.root.revocation_ids[-1]
        await _revoke(harness.revoke_client, root_block_id)

        samples: list[float] = []
        for leaf in tree.all_leaves:
            elapsed, decision = await _time_to_denied(
                harness.oracle, leaf, deadline_s=_NFR4_LIMIT_S * 2
            )
            samples.append(elapsed)
            # The root id is never any leaf's own block (every leaf is at least depth 1),
            # so every one of the 12 is denied for its *ancestor*, never itself.
            assert decision.reason_code is ReasonCode.ANCESTOR_REVOKED, (
                f"{leaf.revocation_ids[-1]} denied for {decision.reason_code}, "
                "expected ANCESTOR_REVOKED"
            )

        samples.sort()
        print(
            f"\nT-040 (revoke root, 12 agents): min={samples[0] * 1e6:.0f}us "
            f"max={samples[-1] * 1e6:.0f}us (limit {_NFR4_LIMIT_S * 1000:.0f}ms)"
        )
        assert samples[-1] < _NFR4_LIMIT_S, f"slowest {samples[-1]:.3f}s exceeds {_NFR4_LIMIT_S}s"
    finally:
        await harness.aclose()


async def test_revoke_mid_tree_leaves_sibling_subtree_unaffected(
    migrated_engine: AsyncEngine, redis_url: str
) -> None:
    """EC-R02 (spec 07 S11): a mid-tree revoke denies only its own subtree.

    `PLAN.md` calls this out explicitly: "over-revocation is also a bug." Subtree B and C
    share no ancestor id with subtree A's root block below the mandate itself, so revoking
    it must never touch them.
    """
    tree = _build_tree()
    harness = _Harness(migrated_engine, redis_url)
    try:
        await harness.start()

        subtree_a_root_id = tree.subtree_a[0].revocation_ids[-1]  # agt-a1's own block
        await _revoke(harness.revoke_client, subtree_a_root_id)

        samples: list[float] = []
        for index, leaf in enumerate(tree.subtree_a):
            elapsed, decision = await _time_to_denied(
                harness.oracle, leaf, deadline_s=_NFR4_LIMIT_S * 2
            )
            samples.append(elapsed)
            expected = ReasonCode.TOKEN_REVOKED if index == 0 else ReasonCode.ANCESTOR_REVOKED
            assert decision.reason_code is expected, (
                f"agt-a{index + 1} denied for {decision.reason_code}, expected {expected}"
            )

        samples.sort()
        print(
            f"\nT-040 (revoke mid-tree, subtree A): min={samples[0] * 1e6:.0f}us "
            f"max={samples[-1] * 1e6:.0f}us (limit {_NFR4_LIMIT_S * 1000:.0f}ms)"
        )
        assert samples[-1] < _NFR4_LIMIT_S, f"slowest {samples[-1]:.3f}s exceeds {_NFR4_LIMIT_S}s"

        # The negative test PLAN.md calls out: siblings B and C, checked only after A's
        # revocation has fully propagated (above), must still be allowed.
        for leaf in (*tree.subtree_b, *tree.subtree_c):
            decision = decide(
                leaf,
                _ctx(leaf),
                revocation=harness.oracle,
                policy=_AllowPolicy(),
                budget=_AllowBudget(),
            )
            assert decision.outcome is Outcome.ALLOW, (
                f"{leaf.revocation_ids[-1]} was denied ({decision.reason_code}) by an "
                "unrelated subtree's revocation -- over-revocation"
            )
    finally:
        await harness.aclose()
