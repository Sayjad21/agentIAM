"""Shared builders for a mandate, a root token, and a root SDK client.

The SDK tests need a live token in three separate modules. Duplicating the mandate
literal a fifth time is what `STATUS.md` §4.3 warns about, so it lands here instead.
Existing test modules keep their local copies for now; this is the destination they
should migrate to, not a refactor done in passing.

The clock is fixed. Nothing in these builders reads the wall clock, so a test that
mints, attenuates and verifies is reproducible on a slow machine and at midnight.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from biscuit_auth import KeyPair

from agentiam_core.models import Budget, Mandate
from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root
from agentiam_sdk.client import AgentIAM

#: The mandate's validity window, and an instant inside it. Fixed so that `ttl_s`
#: arithmetic in the SDK is checked against a known number rather than "roughly now".
NOT_BEFORE = datetime(2026, 8, 14, 11, 45, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 8, 14, 12, 45, tzinfo=UTC)
NOW = datetime(2026, 8, 14, 11, 50, tzinfo=UTC)

#: sha256 of the demo task description. A real digest, so the worked example in
#: `specs/01-token-format.md` stays reproducible.
INTENT = "0dc20e2931968ffffd73e255ac7f6984d9677d5ac1508af1a94eacd962537b70"

ROOT_SCOPES = frozenset({"invoice:read", "vendor:read", "payment:initiate"})


def frozen_clock(instant: datetime = NOW) -> Callable[[], datetime]:
    """A clock that always returns `instant`, for injection into `AgentIAM`."""

    def _clock() -> datetime:
        return instant

    return _clock


def a_mandate(**kw: object) -> Mandate:
    """Build a valid mandate, overriding any field by keyword."""
    base: dict[str, object] = {
        "mandate_id": uuid4(),
        "task_id": uuid4(),
        "principal_id": "kc:9f2c1e40-7a3b-4d21-9c88-1b2e5f0a4d77",
        "intent_hash": INTENT,
        "scopes": ROOT_SCOPES,
        "budget": Budget(
            spend_bdt=Decimal("500000"),
            tool_calls=2000,
            rows_read=500_000,
            external_emails=50,
            wall_clock_s=3600,
        ),
        "max_depth": 8,
        "not_before": NOT_BEFORE,
        "expires_at": EXPIRES_AT,
    }
    return Mandate(**(base | kw))  # type: ignore[arg-type]


def a_root_client(
    *,
    key_pair: KeyPair | None = None,
    now: datetime = NOW,
    role: str = "root",
    agent_id: str | None = None,
    **mandate_kw: object,
) -> AgentIAM:
    """Mint a root token and wrap it in a client whose clock is pinned to `now`."""
    key_pair = key_pair or generate_keypair()
    mandate = a_mandate(**mandate_kw)
    token = mint_root(mandate, key_pair.private_key)
    return AgentIAM(
        token=token,
        key_set=RootKeySet([key_pair.public_key]),
        role=role,
        agent_id=agent_id,
        clock=frozen_clock(now),
    )


def a_short_window_mandate_kwargs(seconds: int) -> dict[str, object]:
    """Mandate kwargs whose window closes `seconds` after `NOW`."""
    return {"not_before": NOT_BEFORE, "expires_at": NOW + timedelta(seconds=seconds)}


__all__ = [
    "EXPIRES_AT",
    "INTENT",
    "NOT_BEFORE",
    "NOW",
    "ROOT_SCOPES",
    "a_mandate",
    "a_root_client",
    "a_short_window_mandate_kwargs",
    "frozen_clock",
]
