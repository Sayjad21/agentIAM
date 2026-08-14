"""Ambient propagation of the current agent identity.

An agent framework calls tools from wherever it likes — a gathered coroutine, a nested
task, a worker thread. Threading a token through every call signature is the kind of
discipline that survives the first refactor and not the second, so the identity travels
in a `ContextVar` instead and the call sites read it.

Three measured facts shaped this module. Each is pinned by a test in
`tests/unit/test_sdk_context.py`, because they are properties of the interpreter rather
than of this code, and a future interpreter could change them.

1. **A task copies the context when it is created.** So a child task cannot see a change
   its parent makes afterwards, and — the part that matters — a change the *child* makes
   is invisible to its siblings. That is where isolation between 100 concurrently
   delegated sub-agents actually comes from.

2. **An awaited coroutine does not.** `await coro()` runs in the caller's context, so a
   bare `ContextVar.set()` inside it silently rewrites the caller's identity. This module
   therefore exposes no unpaired setter: `use_identity` is a scope that always resets.

3. **`loop.run_in_executor` does not copy the context; `asyncio.to_thread` does.** The
   thread-pool boundary is real but narrower than "threads" — see `run_in_executor` and
   `bind_identity` below.
"""

from __future__ import annotations

import asyncio
import functools
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final

from agentiam_sdk.errors import IdentityContextError, NoIdentityError

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Executor
    from contextvars import Token
    from types import TracebackType

    from agentiam_sdk.identity import AgentIdentity

#: The one piece of ambient state in the SDK. Module-private on purpose: everything that
#: reads or writes it goes through the functions below, so there is exactly one place
#: where an identity can be installed and exactly one where it is torn down.
_CURRENT: Final[ContextVar[AgentIdentity | None]] = ContextVar(
    "agentiam_current_identity", default=None
)


def current_identity_or_none() -> AgentIdentity | None:
    """The identity active in this context, or None if the agent has not activated one.

    For code that legitimately runs both inside and outside an agent — logging, metrics,
    a transport that adds an `Authorization` header when it can. Anything making an
    authorization decision wants `current_identity()` instead, which fails closed.
    """
    return _CURRENT.get()


def current_identity() -> AgentIdentity:
    """The identity active in this context.

    Returns:
        The current identity.

    Raises:
        NoIdentityError: If no identity is active. Failing is the point: an absent
            identity must never read as an unrestricted one (ENGINEERING-RULES rule 6,
            fail closed).
    """
    identity = _CURRENT.get()
    if identity is None:
        raise NoIdentityError(
            "no agent identity is active in this context; wrap the call in "
            "`client.activate()` or `use_identity(...)`"
        )
    return identity


class _IdentityScope:
    """The block during which one identity is current.

    A context manager rather than a setter because `ContextVar.set` has no natural end.
    Inside a coroutine awaited without a task wrapper, an unpaired `set` leaks into the
    caller — which, between two agents holding different tokens, is a privilege
    escalation with no attacker in it.
    """

    __slots__ = ("_identity", "_reset")

    def __init__(self, identity: AgentIdentity) -> None:
        self._identity = identity
        self._reset: Token[AgentIdentity | None] | None = None

    def __enter__(self) -> AgentIdentity:
        """Install the identity and return it."""
        if self._reset is not None:
            raise IdentityContextError(
                "this identity scope is already active; call `use_identity()` again "
                "rather than re-entering the same scope object"
            )
        self._reset = _CURRENT.set(self._identity)
        return self._identity

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Restore whatever identity was current before, including on the error path."""
        reset, self._reset = self._reset, None
        if reset is None:  # pragma: no cover - only reachable by calling __exit__ twice
            return
        try:
            _CURRENT.reset(reset)
        except ValueError as cause:
            raise IdentityContextError(
                "an identity scope was exited in a different context than it was "
                "entered in — most often an exit stack held across an asyncio task "
                "boundary; enter and exit the scope in the same task"
            ) from cause


def use_identity(identity: AgentIdentity) -> _IdentityScope:
    """Make `identity` current for the duration of a `with` block.

    Args:
        identity: The identity to act under.

    Returns:
        A scope that restores the previous identity on exit.
    """
    return _IdentityScope(identity)


def bind_identity[R](func: Callable[..., R]) -> Callable[..., R]:
    """Capture the current identity now, and re-establish it wherever `func` later runs.

    For the thread-pool boundary, where the context does not follow automatically.

    The obvious implementation binds `contextvars.copy_context()` to the callable and
    calls `Context.run`. Measured: entering one `Context` object from two threads at once
    raises ``RuntimeError: cannot enter context ... is already entered``, so that version
    fails under exactly the concurrency it exists to serve. Capturing the identity and
    re-installing it per invocation has no shared mutable state and is safe to reuse
    (ADR-012). The trade-off is that it carries the AgentIAM identity only, not unrelated
    context variables — which is the SDK's job and not more.

    Args:
        func: The callable to run later, elsewhere.

    Returns:
        A wrapper that restores the captured identity around each call. With no identity
        active at bind time, it is a transparent pass-through.
    """
    identity = _CURRENT.get()

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> R:
        if identity is None:
            return func(*args, **kwargs)
        with _IdentityScope(identity):
            return func(*args, **kwargs)

    return wrapper


async def run_in_executor[R](
    func: Callable[..., R],
    *args: object,
    executor: Executor | None = None,
) -> R:
    """Run `func` in a worker thread with the current identity carried across.

    `loop.run_in_executor` does not propagate context variables — measured, and pinned by
    a test so the day it changes is a visible one. `asyncio.to_thread` does, so an agent
    already using that needs nothing from here; this is for code holding its own pool.

    Args:
        func: Blocking callable to run off the event loop.
        *args: Positional arguments for `func`.
        executor: Pool to run in, or None for the loop's default.

    Returns:
        Whatever `func` returns.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, bind_identity(functools.partial(func, *args)))


__all__ = [
    "bind_identity",
    "current_identity",
    "current_identity_or_none",
    "run_in_executor",
    "use_identity",
]
