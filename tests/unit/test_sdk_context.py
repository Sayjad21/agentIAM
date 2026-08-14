"""Identity propagation across asyncio tasks and thread pools (`agentiam_sdk.context`).

T-011's acceptance criteria are all about *boundaries*: does the identity follow the
agent into a gathered task, into a nested task, into a worker thread — and, far more
importantly, does it fail to follow it *sideways* into a sibling task holding a different
token. A leak in that direction is a privilege escalation with no attacker in it.

Several tests here pin CPython's own behaviour rather than the SDK's. That is deliberate.
The SDK's design choices were made against measured runtime behaviour (ADR-012), so if a
future interpreter changes one of those facts, the test that fails should be the one
naming the assumption, not a mysterious leak three modules away.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from agentiam_sdk.context import (
    bind_identity,
    current_identity,
    current_identity_or_none,
    run_in_executor,
    use_identity,
)
from agentiam_sdk.errors import IdentityContextError, NoIdentityError
from tests.fixtures.tokens import a_root_client

if TYPE_CHECKING:
    from agentiam_sdk.identity import AgentIdentity


def an_identity(role: str) -> AgentIdentity:
    """A distinct, genuinely-signed identity. Distinct tokens, not distinct labels."""
    return a_root_client(role=role).identity


class TestCurrentIdentity:
    def test_none_when_nothing_is_active(self) -> None:
        assert current_identity_or_none() is None

    def test_current_identity_raises_when_nothing_is_active(self) -> None:
        with pytest.raises(NoIdentityError, match="no agent identity"):
            current_identity()

    def test_scope_sets_and_restores(self) -> None:
        identity = an_identity("reader")
        with use_identity(identity) as active:
            assert active is identity
            assert current_identity() is identity
        assert current_identity_or_none() is None

    def test_nested_scopes_restore_the_outer_identity(self) -> None:
        outer, inner = an_identity("outer"), an_identity("inner")
        with use_identity(outer):
            with use_identity(inner):
                assert current_identity() is inner
            assert current_identity() is outer

    def test_scope_restores_even_when_the_body_raises(self) -> None:
        identity = an_identity("reader")
        with pytest.raises(RuntimeError), use_identity(identity):
            raise RuntimeError("boom")
        assert current_identity_or_none() is None

    def test_a_scope_object_cannot_be_entered_twice(self) -> None:
        """Re-entering would overwrite the reset token and lose the outer identity."""
        scope = use_identity(an_identity("reader"))
        with scope, pytest.raises(IdentityContextError, match="already active"), scope:
            pass


class TestAsyncioBoundaries:
    async def test_identity_propagates_into_a_created_task(self) -> None:
        identity = an_identity("reader")

        async def child() -> AgentIdentity:
            await asyncio.sleep(0)
            return current_identity()

        with use_identity(identity):
            assert await asyncio.create_task(child()) is identity

    async def test_identity_propagates_through_gather_and_nesting(self) -> None:
        identity = an_identity("reader")

        async def grandchild() -> str:
            await asyncio.sleep(0)
            return current_identity().agent_id

        async def child() -> list[str]:
            return list(await asyncio.gather(grandchild(), grandchild()))

        with use_identity(identity):
            results = list(await asyncio.gather(child(), child()))

        assert results == [[identity.agent_id] * 2] * 2

    async def test_a_task_does_not_leak_its_identity_to_the_parent(self) -> None:
        parent = an_identity("parent")

        async def child() -> None:
            with use_identity(an_identity("child")):
                await asyncio.sleep(0)

        with use_identity(parent):
            await asyncio.create_task(child())
            assert current_identity() is parent

    async def test_a_bare_coroutine_does_not_leak_either(self) -> None:
        """The sharp edge: an awaited coroutine runs in the *caller's* context.

        Measured — a bare `VAR.set()` inside `await coro()` mutates the caller. Only the
        scope's `reset()` on exit stops that becoming a cross-agent leak, which is why
        the SDK exposes no unpaired setter.
        """
        parent = an_identity("parent")

        async def child() -> None:
            with use_identity(an_identity("child")):
                await asyncio.sleep(0)

        with use_identity(parent):
            await child()  # deliberately not wrapped in a task
            assert current_identity() is parent

    async def test_a_running_task_is_unaffected_by_a_later_parent_change(self) -> None:
        """A task copies the context at creation, not at first await."""
        first, second = an_identity("first"), an_identity("second")
        started = asyncio.Event()

        async def child() -> AgentIdentity:
            started.set()
            await asyncio.sleep(0.01)
            return current_identity()

        with use_identity(first):
            task = asyncio.create_task(child())
            await started.wait()

        with use_identity(second):
            assert await task is first

    async def test_one_hundred_concurrent_tasks_never_see_another_token(self) -> None:
        """The acceptance criterion. 100 distinct tokens, zero cross-task visibility.

        Each task interleaves repeatedly, so the scheduler genuinely switches between
        them mid-scope rather than running each to completion.
        """
        identities = [an_identity(f"agent-{i}") for i in range(100)]
        tokens = {identity.token for identity in identities}
        assert len(tokens) == 100, "the fixture must produce genuinely distinct tokens"

        async def act(identity: AgentIdentity) -> set[str]:
            seen: set[str] = set()
            with use_identity(identity):
                for _ in range(20):
                    await asyncio.sleep(0)
                    seen.add(current_identity().token)
            return seen

        observed = await asyncio.gather(*(act(identity) for identity in identities))

        for identity, seen in zip(identities, observed, strict=True):
            assert seen == {identity.token}
        assert current_identity_or_none() is None


class TestThreadBoundaries:
    async def test_raw_run_in_executor_does_not_propagate(self) -> None:
        """Pins the platform fact the SDK helper exists to work around.

        `loop.run_in_executor` does not copy the context. If this ever starts passing,
        `run_in_executor` below becomes redundant — but until then, an agent that reaches
        for the stdlib call directly silently loses its identity.
        """
        with use_identity(an_identity("reader")), ThreadPoolExecutor(1) as pool:
            loop = asyncio.get_running_loop()
            assert await loop.run_in_executor(pool, current_identity_or_none) is None

    async def test_asyncio_to_thread_does_propagate(self) -> None:
        """`asyncio.to_thread` copies the context; `run_in_executor` does not.

        The boundary is not "threads" — it is which thread API. Worth pinning so the
        documentation does not overstate the problem.
        """
        identity = an_identity("reader")
        with use_identity(identity):
            assert await asyncio.to_thread(current_identity) is identity

    async def test_the_sdk_helper_carries_the_identity_across(self) -> None:
        identity = an_identity("reader")
        with use_identity(identity), ThreadPoolExecutor(1) as pool:
            assert await run_in_executor(current_identity, executor=pool) is identity

    async def test_the_helper_passes_arguments_through(self) -> None:
        def scoped(prefix: str, suffix: str) -> str:
            return f"{prefix}{current_identity().role}{suffix}"

        with use_identity(an_identity("reader")):
            assert await run_in_executor(scoped, "<", ">") == "<reader>"

    async def test_the_helper_works_with_no_identity_active(self) -> None:
        assert await run_in_executor(current_identity_or_none) is None

    def test_bind_identity_carries_into_a_plain_thread_pool(self) -> None:
        identity = an_identity("reader")
        with use_identity(identity), ThreadPoolExecutor(1) as pool:
            assert pool.submit(bind_identity(current_identity)).result() is identity

    def test_bind_identity_does_not_leak_into_the_worker_thread_afterwards(self) -> None:
        identity = an_identity("reader")
        with ThreadPoolExecutor(1) as pool:
            with use_identity(identity):
                pool.submit(bind_identity(current_identity)).result()
            leftover = pool.submit(current_identity_or_none).result()
        assert leftover is None

    def test_bind_identity_is_safe_when_reused_concurrently(self) -> None:
        """The regression test for ADR-012.

        The obvious implementation binds `contextvars.copy_context()` to the callable.
        Measured: entering one `Context` object from two threads at once raises
        `RuntimeError: cannot enter context ... is already entered`, so the obvious
        implementation crashes under exactly the concurrency it exists to serve. The
        barrier forces the overlap rather than hoping for it.
        """
        identity = an_identity("reader")
        barrier = threading.Barrier(4, timeout=10)

        with use_identity(identity):
            bound = bind_identity(current_identity)

        def call() -> AgentIdentity:
            barrier.wait()
            return bound()

        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(call) for _ in range(4)]
            assert [f.result() for f in futures] == [identity] * 4

    def test_the_copy_context_alternative_really_does_fail(self) -> None:
        """Proof the guard above is load-bearing, not folklore.

        A guard whose removal changes nothing is not protecting anything, so the
        rejected design is exercised here directly.
        """
        var: contextvars.ContextVar[int] = contextvars.ContextVar("probe", default=0)
        var.set(1)
        snapshot = contextvars.copy_context()
        barrier = threading.Barrier(2, timeout=10)
        errors: list[str] = []

        def call() -> None:
            def inner() -> None:
                barrier.wait()

            try:
                snapshot.run(inner)
            except RuntimeError as exc:
                errors.append(str(exc))
            except threading.BrokenBarrierError:  # the thread that lost the race
                pass

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert any("already entered" in message for message in errors)

    def test_bind_identity_with_no_identity_active_is_a_no_op(self) -> None:
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(bind_identity(current_identity_or_none)).result() is None

    def test_bind_identity_preserves_the_wrapped_functions_identity(self) -> None:
        def tool(x: int) -> int:
            """Docstring worth keeping."""
            return x

        with use_identity(an_identity("reader")):
            bound = bind_identity(tool)

        assert bound.__name__ == "tool"
        assert bound.__doc__ == "Docstring worth keeping."
        assert bound(3) == 3


class TestCrossContextExit:
    async def test_exiting_a_scope_from_another_task_is_a_legible_error(self) -> None:
        """`ContextVar.reset()` refuses a token minted in a different context.

        Measured: CPython raises `ValueError: <Token ...> was created in a different
        Context`, which names neither the SDK nor the mistake. It is contrived to do on
        purpose and entirely possible to do by accident with an `AsyncExitStack` held
        across a task boundary.
        """
        scope = use_identity(an_identity("reader"))
        scope.__enter__()

        async def leave() -> None:
            scope.__exit__(None, None, None)

        with pytest.raises(IdentityContextError, match="different context"):
            await asyncio.create_task(leave())
