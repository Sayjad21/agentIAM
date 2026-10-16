"""The console's revoke button and its money display — APICTA-04.

Revocation has worked through `POST /v1/revocations` since T-038, but only for someone holding
an operator token and a terminal. The identity tree showed revoked agents and could not make
one. These tests pin the console half: who is offered the button, what it sends, and how an
amount reads to someone who does not think in taka.

The page itself does no I/O, so the app here is built on an engine that is never connected.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path

import httpx
import itsdangerous
import pytest

from agentiam_controlplane.app import create_app
from agentiam_controlplane.console.money import DEFAULT_BDT_PER_USD, format_bdt
from agentiam_controlplane.db.base import make_engine, make_session_factory
from agentiam_controlplane.settings import ControlPlaneSettings
from agentiam_core.tokens import generate_keypair

_SECRET = "test-session-secret"  # noqa: S105 — throwaway test signing key
_SETTINGS = ControlPlaneSettings(
    root_private_key=generate_keypair().private_key,
    approvers=frozenset({"kc:cfo"}),
    session_secret_key=_SECRET,
)
_TASK = "d0d0d0d0-0000-4000-8000-000000000002"
_TEMPLATES = (
    Path(__file__).resolve().parents[2]
    / "packages/agentiam-controlplane/src/agentiam_controlplane/console"
)


def _session_cookie(principal_id: str) -> str:
    """A cookie `SessionMiddleware` accepts, built the way the escalation tests build one."""
    payload = base64.b64encode(json.dumps({"principal_id": principal_id}).encode())
    return itsdangerous.TimestampSigner(_SECRET).sign(payload).decode()


def _client(*, bdt_per_usd: Decimal | None = None) -> httpx.AsyncClient:
    engine = make_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/unused")
    app = create_app(
        session_factory=make_session_factory(engine),
        escalation_settings=_SETTINGS,
        bdt_per_usd=bdt_per_usd,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")


async def _tree_page(principal_id: str | None) -> str:
    async with _client() as client:
        if principal_id is not None:
            client.cookies.set("session", _session_cookie(principal_id))
        response = await client.get("/identity-tree", params={"task_id": _TASK})
    assert response.status_code == 200
    return response.text


class TestWhoIsOfferedTheButton:
    async def test_an_approver_session_is_offered_the_revoke_control(self) -> None:
        page = await _tree_page("kc:cfo")
        assert 'data-revoker="kc:cfo"' in page

    async def test_no_session_means_no_revoke_control(self) -> None:
        page = await _tree_page(None)
        assert "data-revoker=" not in page

    async def test_a_signed_in_non_approver_is_not_offered_it(self) -> None:
        # The API would answer 403; a button that can only fail is worse than none.
        page = await _tree_page("kc:analyst")
        assert "data-revoker=" not in page


class TestWhatTheButtonSends:
    """The request is built in the browser, so its shape is pinned against the template."""

    @pytest.fixture
    def tree_js(self) -> str:
        return (_TEMPLATES / "templates/identity_tree.html").read_text(encoding="utf-8")

    def test_it_revokes_the_agents_own_block(self, tree_js: str) -> None:
        # `block_ids[depth]` is this agent's block; `block_ids[depth - 1]` is its parent's.
        # Naming the parent would revoke the agent's siblings too.
        assert "block_id: node.block_ids[node.depth]" in tree_js

    def test_it_posts_to_the_revocation_api_with_the_session(self, tree_js: str) -> None:
        assert "fetch('/v1/revocations'" in tree_js
        assert "credentials: 'same-origin'" in tree_js

    def test_an_unknown_expiry_errs_late_never_early(self, tree_js: str) -> None:
        # Spec 07 §8: a row is prunable once `expires_at` passes. An expiry earlier than the
        # token's real one would let pruning un-revoke a live token; a later one only keeps
        # the row longer.
        assert "REVOCATION_FALLBACK_EXPIRY_DAYS" in tree_js


class TestMoney:
    def test_a_round_amount(self) -> None:
        assert format_bdt(Decimal("500000")) == "৳500,000 ($4,167)"

    def test_zero(self) -> None:
        assert format_bdt(Decimal("0")) == "৳0 ($0.00)"

    def test_a_small_amount_keeps_its_cents(self) -> None:
        # Rounding ৳100 to "$1" would overstate it by a fifth.
        assert format_bdt(Decimal("100")) == "৳100 ($0.83)"

    def test_ledger_precision_is_not_shown_as_noise(self) -> None:
        assert format_bdt(Decimal("25000.0000")) == "৳25,000 ($208.33)"

    def test_a_fractional_taka_amount_shows_its_fraction(self) -> None:
        assert format_bdt(Decimal("1234.5")) == "৳1,234.50 ($10.29)"

    def test_the_rate_is_configurable(self) -> None:
        assert format_bdt(Decimal("1000"), bdt_per_usd=Decimal("100")) == "৳1,000 ($10.00)"

    def test_the_default_rate(self) -> None:
        assert DEFAULT_BDT_PER_USD == Decimal("120")

    async def test_every_page_states_the_rate_once(self) -> None:
        async with _client(bdt_per_usd=Decimal("110")) as client:
            page = (await client.get("/identity-tree")).text
        assert 'data-bdt-per-usd="110"' in page
        assert "fixed display rate" in page

    def test_the_browser_helper_matches_the_server_one(self) -> None:
        ui = (_TEMPLATES / "static/ui.js").read_text(encoding="utf-8")
        assert "bdt: bdt" in ui
        for page in ("budgets.html", "identity_tree.html", "escalations.html"):
            source = (_TEMPLATES / "templates" / page).read_text(encoding="utf-8")
            assert "AgentIAM.bdt(" in source, f"{page} formats money without the shared helper"
