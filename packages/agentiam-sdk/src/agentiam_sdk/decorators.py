"""Declarative scope checks at the agent's own call sites.

`@requires_scope` is local, advisory, and **not** the security boundary. An agent that
removes it gains nothing: the PEP re-derives the same answer from the token, and the
token itself cannot exercise a scope that was attenuated away regardless of what the
client believes. What the decorator buys is a failure at the call site — in the agent's
own traceback, before a request is built — rather than a denial arriving from a gateway
several hops later.

Stating that plainly matters for the threat model. A client-side check presented as
enforcement is exactly the kind of claim `docs/threat-model.md` §2 exists to prevent.
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, TypeVar, cast

from agentiam_core.errors import ReasonCode
from agentiam_sdk.context import current_identity
from agentiam_sdk.errors import ScopeDeniedError

if TYPE_CHECKING:
    from collections.abc import Callable

F = TypeVar("F", bound="Callable[..., object]")


def _check(required: frozenset[str]) -> None:
    """Raise if the current identity cannot exercise every scope in `required`.

    Raises:
        NoIdentityError: If no identity is active. Fails closed — an absent identity is
            never read as an unrestricted one.
        ScopeDeniedError: Tagged `SCOPE_ATTENUATED_AWAY` when the mandate granted the
            scope and a delegation step gave it up, and `SCOPE_NOT_GRANTED` when the
            mandate never carried it. When both kinds are missing at once the second
            wins: no delegation could have supplied it, so it is the harder failure and
            the more useful thing to put in front of whoever is debugging.
    """
    identity = current_identity()
    missing = required - identity.scopes
    if not missing:
        return

    never_granted = missing - identity.granted_scopes
    reason = ReasonCode.SCOPE_NOT_GRANTED if never_granted else ReasonCode.SCOPE_ATTENUATED_AWAY
    detail = (
        "never granted by the mandate"
        if never_granted
        else "granted by the mandate but attenuated away before this token"
    )
    raise ScopeDeniedError(
        f"{identity.role} lacks {sorted(missing)} — {detail}",
        reason_code=reason,
        missing_scopes=missing,
    )


def requires_scope(*scopes: str) -> Callable[[F], F]:
    """Refuse to call the wrapped function unless the current identity holds `scopes`.

    Works on both sync and async functions; the async form stays a coroutine function so
    `inspect.iscoroutinefunction` and any framework relying on it keep working.

    Args:
        *scopes: Every scope the call needs. All of them are required.

    Returns:
        A decorator.

    Raises:
        ValueError: At decoration time, if no scopes were named or one is empty. A
            decorator that checks nothing looks like protection and is not, and finding
            that at import is much better than finding it in production.
    """
    if not scopes:
        raise ValueError("requires_scope() needs at least one scope; it checks nothing otherwise")
    if any(not scope.strip() for scope in scopes):
        raise ValueError(f"requires_scope() was given an empty scope: {scopes!r}")

    required = frozenset(scopes)

    def decorate(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: object, **kwargs: object) -> object:
                _check(required)
                return await func(*args, **kwargs)

            return cast("F", async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            _check(required)
            return func(*args, **kwargs)

        return cast("F", wrapper)

    return decorate


__all__ = ["requires_scope"]
