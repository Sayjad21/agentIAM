"""The stub tool servers — M4, `PLAN.md` §5.

Four upstreams for the PEP to protect. They persist nothing and call nothing; the PEP's job is
to decide *before* a real side effect, so what sits behind it only has to be a believable HTTP
surface.

`TestDeterminism` is the class that earns its place. The first version of `_stable_id` used
Python's `hash()`, which is randomized per process — so the "deterministic" stub returned a
different payment id on every run, which would have made a demo re-run disagree with its own
screencast. Caught by running it three times; these tests are what stop it coming back.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator
from decimal import Decimal

import httpx
import pytest

from agentiam_demo.tools import INVOICES, VENDORS, create_tools_app


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_tools_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://tools") as c:
        yield c


class TestInvoices:
    async def test_list_returns_the_seed_data(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/invoices")).json()
        assert body["count"] == len(INVOICES)

    async def test_limit_is_honoured(self, client: httpx.AsyncClient) -> None:
        """The extractor maps `query.limit`, so it needs an upstream that reads it."""
        body = (await client.get("/invoices", params={"limit": 2})).json()
        assert body["count"] == 2

    async def test_one_invoice_by_id(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/invoices/inv_001")).json()
        assert body["id"] == "inv_001"

    async def test_an_unknown_invoice_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/invoices/nope")).status_code == 404


class TestVendors:
    async def test_list(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/vendors")).json()["count"] == len(VENDORS)

    async def test_one_vendor(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/vendors/ven_01")).json()["account_id"] == "acct_1001"

    async def test_an_unknown_vendor_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/vendors/nope")).status_code == 404


class TestPayments:
    async def test_a_payment_is_accepted_and_echoed(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/payments", json={"amount": "1250.5000", "recipient": {"account_id": "acct_1001"}}
        )
        body = response.json()
        assert body["status"] == "accepted"
        assert body["amount"] == "1250.5000"

    async def test_the_echoed_amount_is_what_step_9_settles(
        self, client: httpx.AsyncClient
    ) -> None:
        """Spec 04 §4.3's `actual`. Returning it is what makes the commit path exercisable."""
        body = (
            await client.post(
                "/payments",
                json={"amount": "7.2500", "recipient": {"account_id": "acct_1002"}},
            )
        ).json()
        assert Decimal(body["amount"]) == Decimal("7.2500")

    async def test_a_missing_recipient_is_400(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/payments", json={"amount": "1"})).status_code == 400

    async def test_a_missing_amount_is_400(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/payments", json={"recipient": {"account_id": "acct_1001"}})
        assert response.status_code == 400

    async def test_a_non_numeric_amount_is_400_not_a_crash(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/payments", json={"amount": "lots", "recipient": {"account_id": "acct_1001"}}
        )
        assert response.status_code == 400

    async def test_the_amount_never_becomes_a_float(self, client: httpx.AsyncClient) -> None:
        """Rule 6 does not stop applying because this is a stub.

        `0.1 + 0.2` is the canonical demonstration; here the equivalent is that a value with
        four decimal places comes back with all four, rather than as a float's nearest.
        """
        body = (
            await client.post(
                "/payments",
                json={"amount": "0.0001", "recipient": {"account_id": "acct_1001"}},
            )
        ).json()
        assert body["amount"] == "0.0001"


class TestEmail:
    async def test_an_email_is_queued(self, client: httpx.AsyncClient) -> None:
        body = (await client.post("/email/send", json={"to": "finance@example.com"})).json()
        assert body["status"] == "queued"

    async def test_a_missing_recipient_is_400(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/email/send", json={})).status_code == 400


class TestDeterminism:
    """`ROADMAP.md` M6 wants scripted, deterministic demo beats.

    A stub returning a fresh id per run makes a screencast disagree with a live run for no
    reason, and makes an e2e assertion on the id impossible.
    """

    async def test_the_same_payment_gives_the_same_id(self, client: httpx.AsyncClient) -> None:
        payload = {"amount": "500.0000", "recipient": {"account_id": "acct_1001"}}
        first = (await client.post("/payments", json=payload)).json()["payment_id"]
        second = (await client.post("/payments", json=payload)).json()["payment_id"]
        assert first == second

    async def test_different_payments_give_different_ids(self, client: httpx.AsyncClient) -> None:
        one = (
            await client.post(
                "/payments",
                json={"amount": "500.0000", "recipient": {"account_id": "acct_1001"}},
            )
        ).json()["payment_id"]
        two = (
            await client.post(
                "/payments",
                json={"amount": "501.0000", "recipient": {"account_id": "acct_1001"}},
            )
        ).json()["payment_id"]
        assert one != two

    def test_ids_are_stable_across_processes(self) -> None:
        """The bug that was actually written: `hash()` is randomized per interpreter.

        Two subprocesses, because a single process cannot observe `PYTHONHASHSEED` varying —
        which is exactly why the original bug survived being written.
        """
        script = (
            "from agentiam_demo.tools import _stable_id; "
            "print(_stable_id('pay', 'acct_1001', '500.0000'))"
        )
        runs = {
            subprocess.run(  # noqa: S603
                [sys.executable, "-c", script], capture_output=True, text=True, check=True
            ).stdout.strip()
            for _ in range(3)
        }
        assert len(runs) == 1, f"the id differs between processes: {runs}"

    def test_parts_cannot_collide(self) -> None:
        """`('a', 'bc')` and `('ab', 'c')` must not hash to the same id."""
        from agentiam_demo.tools import _stable_id

        assert _stable_id("x", "a", "bc") != _stable_id("x", "ab", "c")

    def test_the_source_carries_no_raw_control_characters(self) -> None:
        """The separator is written as an escape, not pasted in.

        A raw control character in source is invisible in an editor and survives exactly one
        careless edit — the same family of hazard as the encoding corruption
        `tests/unit/test_source_encoding.py` exists to catch.
        """
        from pathlib import Path

        import agentiam_demo.tools as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        offenders = {c for c in source if ord(c) < 32 and c not in "\n\t"}
        assert not offenders, f"raw control characters in source: {offenders!r}"
