"""Declarative scope checks (`agentiam_sdk.decorators`).

`@requires_scope` is a *local, advisory* check. It exists so an agent fails at its own
call site instead of building a request the PEP will reject, and so the reason is legible
in the agent's own traceback. It is not the security boundary — an agent that deletes the
decorator gains nothing, because the PEP re-derives the same answer from the token.

The distinction the tests care about is `SCOPE_NOT_GRANTED` versus
`SCOPE_ATTENUATED_AWAY`. Both mean "denied", and they mean very different things to
whoever is debugging: the first says the mandate never had it, the second says a
delegation step gave it up. The console filters on that difference (PLAN.md §6.9).
"""

from __future__ import annotations

import asyncio

import pytest

from agentiam_core.errors import ReasonCode
from agentiam_sdk.decorators import requires_scope
from agentiam_sdk.errors import NoIdentityError, ScopeDeniedError
from tests.fixtures.tokens import a_root_client


@requires_scope("invoice:read")
def read_invoice(invoice_id: str) -> str:
    """Read an invoice."""
    return f"invoice:{invoice_id}"


@requires_scope("invoice:read")
async def read_invoice_async(invoice_id: str) -> str:
    """Read an invoice, asynchronously."""
    await asyncio.sleep(0)
    return f"invoice:{invoice_id}"


@requires_scope("invoice:read", "payment:initiate")
def pay_invoice(invoice_id: str) -> str:
    return f"paid:{invoice_id}"


class TestAllow:
    def test_a_held_scope_permits_the_call(self) -> None:
        with a_root_client().activate():
            assert read_invoice("INV-1") == "invoice:INV-1"

    async def test_the_async_form_works_the_same(self) -> None:
        with a_root_client().activate():
            assert await read_invoice_async("INV-1") == "invoice:INV-1"

    def test_every_required_scope_must_be_held(self) -> None:
        with a_root_client().activate():
            assert pay_invoice("INV-1") == "paid:INV-1"

    def test_an_attenuated_child_keeps_what_it_kept(self) -> None:
        child = a_root_client().attenuate(role="reader", scopes=["invoice:read"])
        with child.activate():
            assert read_invoice("INV-1") == "invoice:INV-1"


class TestDeny:
    def test_a_scope_the_mandate_never_had_is_not_granted(self) -> None:
        @requires_scope("admin:write")
        def escalate() -> None: ...

        with a_root_client().activate(), pytest.raises(ScopeDeniedError) as caught:
            escalate()

        assert caught.value.reason_code is ReasonCode.SCOPE_NOT_GRANTED
        assert caught.value.missing_scopes == frozenset({"admin:write"})

    def test_a_scope_given_up_by_a_parent_is_attenuated_away(self) -> None:
        """The difference that makes the two reason codes worth having."""
        child = a_root_client().attenuate(role="reader", scopes=["invoice:read"])

        with child.activate(), pytest.raises(ScopeDeniedError) as caught:
            pay_invoice("INV-1")

        assert caught.value.reason_code is ReasonCode.SCOPE_ATTENUATED_AWAY
        assert caught.value.missing_scopes == frozenset({"payment:initiate"})

    def test_never_granted_wins_when_both_kinds_are_missing(self) -> None:
        """The harder failure is reported: a scope no delegation could have supplied."""

        @requires_scope("payment:initiate", "admin:write")
        def both() -> None: ...

        child = a_root_client().attenuate(role="reader", scopes=["invoice:read"])
        with child.activate(), pytest.raises(ScopeDeniedError) as caught:
            both()

        assert caught.value.reason_code is ReasonCode.SCOPE_NOT_GRANTED
        assert caught.value.missing_scopes == frozenset({"payment:initiate", "admin:write"})

    def test_the_message_names_the_missing_scope(self) -> None:
        with a_root_client().activate(), pytest.raises(ScopeDeniedError, match="admin:write"):

            @requires_scope("admin:write")
            def escalate() -> None: ...

            escalate()

    async def test_the_async_form_denies_too(self) -> None:
        @requires_scope("admin:write")
        async def escalate() -> None: ...

        with a_root_client().activate(), pytest.raises(ScopeDeniedError):
            await escalate()

    def test_the_wrapped_function_never_runs_when_denied(self) -> None:
        ran: list[bool] = []

        @requires_scope("admin:write")
        def escalate() -> None:
            ran.append(True)

        with a_root_client().activate(), pytest.raises(ScopeDeniedError):
            escalate()
        assert ran == []


class TestNoIdentity:
    def test_calling_without_an_active_identity_is_an_error(self) -> None:
        with pytest.raises(NoIdentityError):
            read_invoice("INV-1")

    async def test_the_async_form_too(self) -> None:
        with pytest.raises(NoIdentityError):
            await read_invoice_async("INV-1")

    def test_it_fails_closed_rather_than_permitting(self) -> None:
        """A missing identity must never read as "unrestricted"."""
        ran: list[bool] = []

        @requires_scope("invoice:read")
        def tool() -> None:
            ran.append(True)

        with pytest.raises(NoIdentityError):
            tool()
        assert ran == []


class TestDecoratorHygiene:
    def test_metadata_is_preserved(self) -> None:
        assert read_invoice.__name__ == "read_invoice"
        assert read_invoice.__doc__ == "Read an invoice."

    def test_async_metadata_is_preserved(self) -> None:
        assert read_invoice_async.__name__ == "read_invoice_async"
        assert asyncio.iscoroutinefunction(read_invoice_async)

    def test_decorating_with_no_scopes_is_refused_at_import_time(self) -> None:
        """A decorator that checks nothing looks like protection and is not."""
        with pytest.raises(ValueError, match="at least one scope"):

            @requires_scope()
            def tool() -> None: ...

    def test_an_empty_scope_string_is_refused(self) -> None:
        with pytest.raises(ValueError, match="scope"):

            @requires_scope("")
            def tool() -> None: ...
