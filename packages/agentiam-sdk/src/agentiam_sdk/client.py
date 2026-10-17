"""The client an agent developer holds: verify a token, attenuate it, act under it.

Minting a narrower child is pure local cryptography — no issuer round-trip, no shared
state (INV-3) — and it is the claim the demo is built on, so it shipped first.

`call()` and `call_with_escalation_wait()` (APICTA-03) are the network half: a request
through the PEP under this client's token, and, when the PEP answers `APPROVAL_REQUIRED`,
waiting for a human's decision and retrying once under the elevated token the approval
minted. The agent collects that token from the control plane with the token it was refused
under — the only credential that proves it is the agent that asked. Nothing here enforces
anything; the PEP does, and an over-grant retry is refused there, not here.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import warnings
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

import httpx

from agentiam_core.attenuation import attenuate as core_attenuate
from agentiam_core.errors import DepthExceededError, ReasonCode
from agentiam_core.models import (
    BudgetCeiling,
    BudgetDimension,
    Caveat,
    ScopeSubset,
    TimeWindow,
    ToolAllow,
    ToolDeny,
)
from agentiam_core.tokens import WARN_SIZE_LIMIT_B64, RootKeySet, verify
from agentiam_sdk.context import use_identity
from agentiam_sdk.errors import (
    EscalationDenied,
    EscalationError,
    EscalationExpired,
    EscalationWaitTimeout,
    TokenSizeWarning,
)
from agentiam_sdk.identity import AgentIdentity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from agentiam_sdk.context import _IdentityScope


#: Refusal codes whose body carries an escalation id worth waiting on (spec 09 §11).
_ESCALATING: Final = frozenset(
    {ReasonCode.APPROVAL_REQUIRED.value, ReasonCode.DRIFT_ESCALATION.value}
)
#: Each status poll waits this much longer than the last, up to this multiple of `poll_s`.
#: An approver takes tens of seconds; the cap keeps the worst added latency a few polls.
_BACKOFF: Final = 1.5
_BACKOFF_CAP: Final = 5.0
#: How often the approved retry is re-sent while the PEP answers `LEASE_UNAVAILABLE`, and the
#: longest wait between. An approved amount can exceed the PEP's standing lease; the refusal
#: makes the PEP acquire enough to cover it (spec 04 §4.1a), which takes one ledger round trip.
_LEASE_RETRIES: Final = 5
_LEASE_RETRY_CAP_S: Final = 1.0
#: Per-request bound for a client the SDK builds itself. Generous: it covers the upstream.
_HTTP_TIMEOUT_S: Final = 30.0


def _utc_now() -> datetime:
    """The default clock.

    The SDK may read one; `agentiam-core` may not, which is why it is injected here and
    passed down as an argument there (ENGINEERING-RULES rule 3).
    """
    return datetime.now(UTC)


class AgentIAM:
    """An agent acting under one token.

    Immutable in the way that matters: `attenuate()` returns a *new* client wrapping a
    new token and never touches this one. A sub-agent receiving a child client cannot
    reach back through it to the parent's authority.
    """

    __slots__ = ("_clock", "_control_plane_url", "_http", "_identity", "_key_set", "_task_intent")

    def __init__(
        self,
        *,
        token: str,
        key_set: RootKeySet,
        role: str = "root",
        agent_id: str | None = None,
        known_caveats: Sequence[Caveat] = (),
        task_intent: str | None = None,
        clock: Callable[[], datetime] | None = None,
        http: httpx.AsyncClient | None = None,
        control_plane_url: str | None = None,
    ) -> None:
        """Verify `token` and hold it as this agent's identity.

        Verifying at construction rather than at first use means a bad token fails where
        it entered the process, with the reason code attached, instead of surfacing later
        as a confusing denial from the PEP.

        Args:
            token: Base64-encoded biscuit chain.
            key_set: Root public keys to accept. More than one during rotation (EC-T05).
            role: Human-readable role, for the console and the audit trail.
            agent_id: Identity of this agent. Defaults to one derived from the mandate.
            known_caveats: Caveats already on the chain, if the holder knows them. Supply
                these when a token is handed over along with the restrictions that were
                placed on it; omitting them makes the folded authority an upper bound
                rather than an exact one, never the reverse.
            task_intent: The original English description of the task this token authorizes.
                Used by the PEP for drift scoring.
            clock: Source of the current instant. Injected so tests are reproducible.
            http: The HTTP client `call()` sends through. When omitted, each call opens and
                closes its own; pass one to reuse connections or to inject a transport.
            control_plane_url: Base URL of the control plane, where
                `call_with_escalation_wait()` collects an approved escalation.

        Raises:
            TokenTooLargeError, MalformedTokenError, InvalidSignatureError,
            TokenNotYetValidError, TokenExpiredError, DepthExceededError: As `verify()`.
        """
        self._key_set = key_set
        self._clock = clock or _utc_now
        self._task_intent = task_intent
        self._http = http
        self._control_plane_url = control_plane_url
        verified = verify(token, key_set, now=self._clock())
        self._identity = AgentIdentity(
            token=token,
            verified=verified,
            agent_id=agent_id or f"agent:{verified.mandate_id}",
            role=role,
            known_caveats=tuple(known_caveats),
        )

    def headers(self, action_intent: str | None = None) -> dict[str, str]:
        """HTTP headers required to authenticate a request using this identity.

        Includes the token itself and the stateless intent strings for drift detection.

        Args:
            action_intent: Optional plain English description of the specific action being
                attempted right now. Required for accurate semantic drift scoring.
        """
        h = {"Authorization": f"Bearer {self.token}"}
        if self._task_intent:
            h["AgentIAM-Task-Intent"] = self._task_intent
        if action_intent:
            h["AgentIAM-Action-Intent"] = action_intent
        return h

    @property
    def identity(self) -> AgentIdentity:
        """The identity this client acts under."""
        return self._identity

    @property
    def token(self) -> str:
        """The chain as it goes on the wire."""
        return self._identity.token

    @property
    def control_plane_url(self) -> str | None:
        """Where an approved escalation is collected from, if configured."""
        return self._control_plane_url

    @property
    def key_set(self) -> RootKeySet:
        """The root keys this client verifies against, for handing a token onward."""
        return self._key_set

    def activate(self) -> _IdentityScope:
        """Make this client's identity current for the duration of a `with` block."""
        return use_identity(self._identity)

    def attenuate(
        self,
        *,
        role: str,
        agent_id: str | None = None,
        scopes: Iterable[str] | None = None,
        budget: Mapping[str | BudgetDimension, Decimal | int | str] | None = None,
        ttl_s: int | None = None,
        tools_allow: Iterable[str] | None = None,
        tools_deny: Iterable[str] | None = None,
        caveats: Sequence[Caveat] = (),
    ) -> AgentIAM:
        """Mint a strictly narrower child token, offline, and wrap it in a new client.

        The four common restrictions have keyword forms because they are what an agent
        reaches for; the other five caveat types go through `caveats`. Everything given
        is checked against the grant *and* against the caveats this client already knows
        about, which is the check core cannot make on its own — given only a
        `VerifiedToken`, a scope a parent gave up is still listed in the authority block.

        Args:
            role: The child's role.
            agent_id: The child's identity. Defaults to one derived from the mandate.
            scopes: Scopes the child keeps. Must be a subset of what this token holds.
            budget: Per-dimension ceilings, keyed by `BudgetDimension` or its value.
                Never floats — money is `Decimal`, `int` or `str` (rule 4).
            ttl_s: Seconds from now until the child expires. Read from the injected
                clock, so a child never outlives its parent by clock skew.
            tools_allow: The only tools the child may call.
            tools_deny: Tools the child may not call, in addition to any already denied.
            caveats: Any other caveats, including the four types with no keyword form.

        Returns:
            A new client holding the child token.

        Raises:
            ValueError: If nothing was restricted, or `ttl_s` is not positive.
            AttenuationError: If any restriction would widen authority. No token is
                produced.
            DepthExceededError: If the child would exceed the mandate's `max_depth`.

        Warns:
            TokenSizeWarning: If the child crosses the 4 KB advisory limit (EC-T11).
        """
        proposed = [
            *self._keyword_caveats(
                scopes=scopes,
                budget=budget,
                ttl_s=ttl_s,
                tools_allow=tools_allow,
                tools_deny=tools_deny,
            ),
            *caveats,
        ]
        if not proposed:
            raise ValueError(
                "attenuate() was given no caveats; a block that narrows nothing adds "
                "~410 bytes to every request and grants no additional safety"
            )

        parent = self._identity
        if parent.depth + 1 > parent.verified.max_depth:
            # Checked before minting. `verify()` would catch it a moment later, but only
            # after producing a token that must then be thrown away.
            raise DepthExceededError(
                f"delegating from depth {parent.depth} would exceed the mandate's "
                f"max_depth of {parent.verified.max_depth}",
                reason_code=ReasonCode.DEPTH_EXCEEDED,
            )

        child_agent_id = agent_id or f"agent:{parent.verified.mandate_id}:{parent.depth + 1}"
        child_token = core_attenuate(
            parent.verified,
            proposed,
            agent_id=child_agent_id,
            role=role,
            ancestor_caveats=parent.known_caveats,
        )
        if len(child_token) > WARN_SIZE_LIMIT_B64:
            warnings.warn(
                f"child token is {len(child_token)} base64 characters, past the "
                f"{WARN_SIZE_LIMIT_B64} advisory limit; some HTTP servers cap the "
                f"request line well below that",
                TokenSizeWarning,
                stacklevel=2,
            )

        return AgentIAM(
            token=child_token,
            key_set=self._key_set,
            role=role,
            agent_id=child_agent_id,
            known_caveats=(*parent.known_caveats, *proposed),
            task_intent=self._task_intent,
            clock=self._clock,
            http=self._http,
            control_plane_url=self._control_plane_url,
        )

    async def call(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,  # noqa: ANN401 — any JSON-serializable body, as httpx takes it
        intent: str | None = None,
    ) -> httpx.Response:
        """Send one request through the PEP under this client's token.

        Args:
            method: HTTP method.
            url: Full URL, the PEP's address included.
            json: Request body, serialized as JSON. Money in it should be a string or an
                integer, never a float (rule 4).
            intent: What this specific action is for, in English — the action intent the
                PEP's drift detection compares against the task (spec 06).

        Returns:
            The PEP's response, whatever its status. A refusal is an answer, not an error.
        """
        return await self._send(method, url, json=json, headers=self.headers(intent))

    async def call_with_escalation_wait(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,  # noqa: ANN401 — as `call()`
        intent: str | None = None,
        timeout_s: float = 120.0,
        poll_s: float = 1.0,
        fit_to_grant: bool = False,
    ) -> httpx.Response:
        """`call()`, and on a refusal that opened an escalation, wait for it and retry once.

        On `403` with `APPROVAL_REQUIRED` or `DRIFT_ESCALATION` and an `escalation_id`, polls
        the control plane's status route with backoff, authenticated by this client's own
        token. Once approved, the request is sent again exactly once under the elevated
        token, and that response is returned whatever it is — except that a
        `429 LEASE_UNAVAILABLE` is re-sent a few times, briefly, because an approved amount
        larger than the PEP's standing lease is refused once while the PEP acquires enough to
        cover it. Any other response is returned as it came.

        Args:
            method: HTTP method.
            url: Full URL, the PEP's address included.
            json: Request body, as `call()`.
            intent: The action intent, as `call()`; sent again with the retry.
            timeout_s: How long to wait for a decision before giving up.
            poll_s: The first interval between status polls; later ones back off.
            fit_to_grant: If the approver granted less than a top-level `amount` in `json`
                asked for, retry with the granted amount instead. Off by default: paying
                less than was asked is a decision for the caller, not a side effect.

        Returns:
            The PEP's response to the original request, or to the one retry.

        Raises:
            ValueError: This client has no `control_plane_url`.
            EscalationDenied: An approver refused; the message carries their reason.
            EscalationExpired: Nobody decided within the escalation's own window.
            EscalationWaitTimeout: `timeout_s` passed with the escalation still pending.
            EscalationError: The control plane will not tell this token about the
                escalation (401/404), or approved it without a token to collect.
        """
        if self._control_plane_url is None:
            raise ValueError(
                "call_with_escalation_wait() needs a control_plane_url to collect the grant from"
            )
        response = await self.call(method, url, json=json, intent=intent)
        escalation_id = _escalation_id(response)
        if escalation_id is None:
            return response

        grant = await self._await_grant(escalation_id, timeout_s=timeout_s, poll_s=poll_s)
        elevated = AgentIAM(
            token=grant["elevated_token"],
            key_set=self._key_set,
            role=self._identity.role,
            agent_id=self._identity.agent_id,
            task_intent=self._task_intent,
            clock=self._clock,
            http=self._http,
            control_plane_url=self._control_plane_url,
        )
        if fit_to_grant:
            json = _fit_to_grant(json, grant["amount"])
        delay = min(poll_s, 0.25)
        for _ in range(_LEASE_RETRIES):
            response = await elevated.call(method, url, json=json, intent=intent)
            if not _lease_unavailable(response):
                return response
            await asyncio.sleep(delay)
            delay = min(delay * 2, _LEASE_RETRY_CAP_S)
        return await elevated.call(method, url, json=json, intent=intent)

    async def _await_grant(
        self, escalation_id: uuid.UUID, *, timeout_s: float, poll_s: float
    ) -> dict[str, Any]:
        """Poll the status route until the escalation is decided or `timeout_s` passes."""
        assert self._control_plane_url is not None  # noqa: S101 — checked by the caller
        status_url = f"{self._control_plane_url.rstrip('/')}/v1/escalations/{escalation_id}/status"
        deadline = time.monotonic() + timeout_s
        interval = poll_s
        while True:
            status = await self._poll(status_url, escalation_id)
            state = status.get("state") if status is not None else None
            if state == "approved":
                if not isinstance(status, dict) or "elevated_token" not in status:
                    raise EscalationError(
                        f"escalation {escalation_id} was approved without a token to collect",
                        escalation_id=escalation_id,
                    )
                return status
            if state == "denied":
                reason = (status or {}).get("reason") or "no reason given"
                raise EscalationDenied(
                    f"escalation {escalation_id} was denied: {reason}",
                    escalation_id=escalation_id,
                )
            if state == "expired":
                raise EscalationExpired(
                    f"escalation {escalation_id} expired before anyone decided it",
                    escalation_id=escalation_id,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EscalationWaitTimeout(
                    f"stopped waiting on escalation {escalation_id} after {timeout_s}s; "
                    f"it may still be approved",
                    escalation_id=escalation_id,
                )
            await asyncio.sleep(min(interval, remaining))
            interval = min(interval * _BACKOFF, poll_s * _BACKOFF_CAP)

    async def _poll(self, status_url: str, escalation_id: uuid.UUID) -> dict[str, Any] | None:
        """One status read. `None` for a transient failure worth polling through."""
        try:
            response = await self._send(
                "GET", status_url, json=None, headers={"Authorization": f"Bearer {self.token}"}
            )
        except httpx.TransportError:
            return None
        if response.status_code >= 500:
            return None
        if response.status_code != 200:
            # A 401 or 404 is this token's answer for this id, and will not change.
            raise EscalationError(
                f"the control plane answered {response.status_code} for escalation {escalation_id}",
                escalation_id=escalation_id,
            )
        body = response.json()
        return body if isinstance(body, dict) else None

    async def _send(
        self,
        method: str,
        url: str,
        *,
        json: Any,  # noqa: ANN401 — as `call()`
        headers: dict[str, str],
    ) -> httpx.Response:
        if self._http is not None:
            return await self._http.request(method, url, json=json, headers=headers)
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as http:
            return await http.request(method, url, json=json, headers=headers)

    def _keyword_caveats(
        self,
        *,
        scopes: Iterable[str] | None,
        budget: Mapping[str | BudgetDimension, Decimal | int | str] | None,
        ttl_s: int | None,
        tools_allow: Iterable[str] | None,
        tools_deny: Iterable[str] | None,
    ) -> list[Caveat]:
        """Translate the keyword shorthands into caveats, in a stable order."""
        built: list[Caveat] = []
        if scopes is not None:
            built.append(ScopeSubset(scopes=frozenset(scopes)))
        if budget is not None:
            # `model_validate` rather than the constructor so the ceiling's own
            # `_reject_float` validator sees the raw value. Converting to `Decimal` here
            # first would silently accept `12000.5` — `Decimal(float)` is perfectly happy
            # to absorb a binary fraction, which is exactly what rule 4 forbids.
            built += [
                BudgetCeiling.model_validate(
                    {"dimension": BudgetDimension(dimension), "value": value}
                )
                for dimension, value in budget.items()
            ]
        if ttl_s is not None:
            if ttl_s <= 0:
                raise ValueError(f"ttl_s must be positive, got {ttl_s}")
            built.append(TimeWindow(not_after=self._clock() + timedelta(seconds=ttl_s)))
        if tools_allow is not None:
            built.append(ToolAllow(tools=frozenset(tools_allow)))
        if tools_deny is not None:
            built.append(ToolDeny(tools=frozenset(tools_deny)))
        return built


def _escalation_id(response: httpx.Response) -> uuid.UUID | None:
    """The escalation a refusal opened, if it opened one an agent can wait on."""
    if response.status_code != 403:
        return None
    try:
        body = response.json()
        if not isinstance(body, dict) or body.get("reason_code") not in _ESCALATING:
            return None
        return uuid.UUID(str(body["escalation_id"]))
    except (ValueError, KeyError):
        return None


def _lease_unavailable(response: httpx.Response) -> bool:
    """Whether the PEP refused only because its lease is being topped up (spec 09 §4)."""
    if response.status_code != 429:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("reason_code") == "LEASE_UNAVAILABLE"


def _fit_to_grant(body: Any, granted: object) -> Any:  # noqa: ANN401 — a JSON body
    """`body` with a top-level `amount` above the grant lowered to it; otherwise unchanged.

    Compared as `Decimal` and written back as the grant's own string, so no float touches
    the money on the way (rule 4).
    """
    if not isinstance(body, dict) or "amount" not in body:
        return body
    try:
        requested, ceiling = Decimal(str(body["amount"])), Decimal(str(granted))
    except InvalidOperation:
        return body
    if requested <= ceiling:
        return body
    return {**body, "amount": str(granted)}


__all__ = ["AgentIAM"]
