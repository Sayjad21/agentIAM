"""The SDK's HTTP calls and its escalation wait (`AgentIAM.call`, `call_with_escalation_wait`).

APICTA-03. The PEP and the control plane are faked with one `httpx.MockTransport` that
routes on host, so what is pinned is the SDK's half of the conversation: which token goes
to which service, when it polls, what it retries with, and what it raises. The control
plane's half — that the status route hands the grant only to the agent that asked — is
`tests/integration/test_escalation_delivery.py`.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root
from agentiam_sdk import client as sdk_client
from agentiam_sdk.client import AgentIAM
from agentiam_sdk.errors import (
    EscalationDenied,
    EscalationError,
    EscalationExpired,
    EscalationWaitTimeout,
)
from tests.fixtures.tokens import a_mandate, frozen_clock

KEYS = generate_keypair()
KEY_SET = RootKeySet([KEYS.public_key])
ESCALATION_ID = uuid.UUID("e5ca1a7e-0000-4000-8000-000000000003")
PEP = "http://pep"
CP = "http://cp"


class FakeServices:
    """A PEP that wants approval for the original token, and a control plane that grants it.

    `states` is what successive status polls report; the last one repeats.
    """

    def __init__(
        self,
        *,
        states: list[str],
        grant_amount: str = "50000.0000",
        refusal_code: str = "APPROVAL_REQUIRED",
        status_code: int = 200,
        pep_accepts_elevated: bool = True,
        lease_refusals: int = 0,
    ) -> None:
        """Mint the original and the elevated token, and script the control plane's answers."""
        self.original = mint_root(a_mandate(), KEYS.private_key)
        self.elevated = mint_root(a_mandate(), KEYS.private_key)
        self.states = list(states)
        self.grant_amount = grant_amount
        self.refusal_code = refusal_code
        self.status_code = status_code
        self.pep_accepts_elevated = pep_accepts_elevated
        self.lease_refusals = lease_refusals
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.url.host == "pep":
            return self._pep(request, bearer)
        assert request.url.path == f"/v1/escalations/{ESCALATION_ID}/status"
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"detail": "nope"})
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        status: dict[str, object] = {"id": str(ESCALATION_ID), "state": state}
        if state == "approved":
            status |= {
                "elevated_token": self.elevated,
                "amount": self.grant_amount,
                "scopes": ["payment:initiate"],
                "expires_at": "2026-08-14T12:40:00+00:00",
            }
        if state == "denied":
            status["reason"] = "supplier not vetted"
        return httpx.Response(200, json=status)

    def _pep(self, request: httpx.Request, bearer: str) -> httpx.Response:
        if bearer == self.elevated and self.lease_refusals > 0:
            # The PEP's lease is being topped up to cover the approved amount (spec 04 §4.1a).
            self.lease_refusals -= 1
            return httpx.Response(429, json={"reason_code": "LEASE_UNAVAILABLE", "detail": "x"})
        if bearer == self.elevated and self.pep_accepts_elevated:
            return httpx.Response(200, json={"paid": json.loads(request.content)["amount"]})
        body = {"reason_code": self.refusal_code, "detail": "needs approval", "trace_id": "t"}
        if self.refusal_code in {"APPROVAL_REQUIRED", "DRIFT_ESCALATION"}:
            body["escalation_id"] = str(ESCALATION_ID)
        return httpx.Response(403, json=body)

    def client(self, *, control_plane_url: str | None = CP) -> AgentIAM:
        return AgentIAM(
            token=self.original,
            key_set=KEY_SET,
            task_intent="Pay the bulk supplier",
            clock=frozen_clock(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            control_plane_url=control_plane_url,
        )

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


async def wait(services: FakeServices, **kw: object) -> httpx.Response:
    return await services.client().call_with_escalation_wait(
        "POST",
        f"{PEP}/payments",
        json={"amount": "75000.0000", "recipient": {"account_id": "acct_supplier"}},
        intent="Pay the bulk supplier 75,000",
        **({"poll_s": 0.001} | kw),  # type: ignore[arg-type]
    )


class TestCall:
    async def test_sends_the_token_the_intents_and_the_body(self) -> None:
        services = FakeServices(states=["pending"])
        response = await services.client().call(
            "POST", f"{PEP}/payments", json={"amount": "10"}, intent="pay ten"
        )
        assert response.status_code == 403
        (sent,) = services.requests
        assert sent.headers["authorization"] == f"Bearer {services.original}"
        assert sent.headers["agentiam-task-intent"] == "Pay the bulk supplier"
        assert sent.headers["agentiam-action-intent"] == "pay ten"
        assert json.loads(sent.content) == {"amount": "10"}

    async def test_a_child_client_keeps_the_transport_and_the_control_plane(self) -> None:
        services = FakeServices(states=["approved"])
        child = services.client().attenuate(role="payer", scopes=["payment:initiate"])
        assert child.control_plane_url == CP
        await child.call("GET", f"{PEP}/invoices/1")
        assert services.requests[0].headers["authorization"] == f"Bearer {child.token}"


class TestEscalationWait:
    async def test_refusal_poll_approval_retry_succeeds(self) -> None:
        services = FakeServices(states=["pending", "pending", "approved"])
        response = await wait(services, fit_to_grant=True)

        assert response.status_code == 200
        polls = services.to("cp")
        assert len(polls) == 3
        # The agent authenticates the poll with the token it was refused under.
        assert {p.headers["authorization"] for p in polls} == {f"Bearer {services.original}"}
        first, retry = services.to("pep")
        assert first.headers["authorization"] == f"Bearer {services.original}"
        assert retry.headers["authorization"] == f"Bearer {services.elevated}"
        assert retry.headers["agentiam-action-intent"] == "Pay the bulk supplier 75,000"

    async def test_fit_to_grant_reduces_the_amount_to_the_approved_ceiling(self) -> None:
        services = FakeServices(states=["approved"])
        response = await wait(services, fit_to_grant=True)
        assert response.json() == {"paid": "50000.0000"}
        retry_body = json.loads(services.to("pep")[1].content)
        assert retry_body["recipient"] == {"account_id": "acct_supplier"}

    async def test_without_fit_to_grant_the_original_amount_is_retried(self) -> None:
        # The PEP is what refuses an over-grant amount; the SDK does not silently shrink it.
        services = FakeServices(states=["approved"])
        response = await wait(services)
        assert response.json() == {"paid": "75000.0000"}

    async def test_an_amount_already_within_the_grant_is_not_raised(self) -> None:
        services = FakeServices(states=["approved"], grant_amount="90000.0000")
        response = await wait(services, fit_to_grant=True)
        assert response.json() == {"paid": "75000.0000"}

    async def test_it_retries_once_and_returns_the_second_answer(self) -> None:
        services = FakeServices(states=["approved"], pep_accepts_elevated=False)
        response = await wait(services)
        assert response.status_code == 403
        assert len(services.to("pep")) == 2
        assert len(services.to("cp")) == 1

    async def test_the_retry_rides_out_a_lease_being_topped_up(self) -> None:
        """`LEASE_UNAVAILABLE` on the approved retry is waited through.

        An approved amount larger than the PEP's lease is refused once while the PEP acquires
        enough to cover it (spec 04 §4.1a), then served.
        """
        services = FakeServices(states=["approved"], lease_refusals=2)
        response = await wait(services)
        assert response.status_code == 200
        assert len(services.to("pep")) == 4
        assert len(services.to("cp")) == 1, "a lease refusal is not a reason to poll again"

    async def test_a_lease_that_never_arrives_is_returned_not_retried_forever(self) -> None:
        services = FakeServices(states=["approved"], lease_refusals=1000)
        response = await wait(services)
        assert response.status_code == 429
        assert len(services.to("pep")) < 10

    async def test_a_denial_raises_with_the_reason(self) -> None:
        services = FakeServices(states=["pending", "denied"])
        with pytest.raises(EscalationDenied, match="supplier not vetted") as caught:
            await wait(services)
        assert caught.value.escalation_id == ESCALATION_ID
        assert len(services.to("pep")) == 1

    async def test_an_expired_escalation_raises(self) -> None:
        services = FakeServices(states=["pending", "expired"])
        with pytest.raises(EscalationExpired):
            await wait(services)

    async def test_giving_up_waiting_is_not_the_escalation_expiring(self) -> None:
        services = FakeServices(states=["pending"])
        with pytest.raises(EscalationWaitTimeout) as caught:
            await wait(services, timeout_s=0.05, poll_s=0.01)
        assert caught.value.escalation_id == ESCALATION_ID
        assert not isinstance(caught.value, EscalationExpired)

    async def test_backoff_spaces_the_polls_out(self) -> None:
        services = FakeServices(states=["pending"])
        with pytest.raises(EscalationWaitTimeout):
            await wait(services, timeout_s=0.3, poll_s=0.02)
        # Fixed-rate polling at 20 ms would make ~15 calls in 300 ms.
        assert len(services.to("cp")) < 10

    async def test_a_control_plane_that_will_not_say_raises_rather_than_spinning(self) -> None:
        services = FakeServices(states=["pending"], status_code=404)
        with pytest.raises(EscalationError, match="404"):
            await wait(services)
        assert len(services.to("cp")) == 1

    async def test_a_control_plane_outage_is_waited_through(self) -> None:
        services = FakeServices(states=["pending", "approved"])
        outages = iter([True, False, False])

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cp" and next(outages):
                raise httpx.ConnectError("control plane down", request=request)
            return services.handle(request)

        client = AgentIAM(
            token=services.original,
            key_set=KEY_SET,
            clock=frozen_clock(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(flaky)),
            control_plane_url=CP,
        )
        response = await client.call_with_escalation_wait(
            "POST", f"{PEP}/payments", json={"amount": "1"}, poll_s=0.001
        )
        assert response.status_code == 200

    async def test_a_refusal_that_is_not_an_escalation_is_returned_as_is(self) -> None:
        services = FakeServices(states=["approved"], refusal_code="BUDGET_EXHAUSTED")
        response = await wait(services)
        assert response.status_code == 403
        assert services.to("cp") == []

    async def test_a_drift_escalation_is_waited_on_too(self) -> None:
        services = FakeServices(states=["approved"], refusal_code="DRIFT_ESCALATION")
        response = await wait(services)
        assert response.status_code == 200

    async def test_waiting_needs_a_control_plane(self) -> None:
        services = FakeServices(states=["approved"])
        with pytest.raises(ValueError, match="control_plane_url"):
            await services.client(control_plane_url=None).call_with_escalation_wait(
                "POST", f"{PEP}/payments", json={"amount": "1"}
            )


class TestEdges:
    """The branches a well-behaved PEP and control plane never take, pinned anyway."""

    async def test_a_5xx_from_the_control_plane_is_polled_through(self) -> None:
        services = FakeServices(states=["pending", "approved"])
        failures = iter([True, False, False])

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cp" and next(failures):
                services.requests.append(request)
                return httpx.Response(503, json={"detail": "starting"})
            return services.handle(request)

        client = AgentIAM(
            token=services.original,
            key_set=KEY_SET,
            clock=frozen_clock(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(flaky)),
            control_plane_url=CP,
        )
        response = await client.call_with_escalation_wait(
            "POST", f"{PEP}/payments", json={"amount": "1"}, poll_s=0.001
        )
        assert response.status_code == 200

    async def test_an_approval_with_no_token_to_collect_raises(self) -> None:
        services = FakeServices(states=["approved"])

        def tokenless(request: httpx.Request) -> httpx.Response:
            response = services.handle(request)
            if request.url.host == "cp":
                body = response.json()
                body.pop("elevated_token")
                return httpx.Response(200, json=body)
            return response

        client = AgentIAM(
            token=services.original,
            key_set=KEY_SET,
            clock=frozen_clock(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(tokenless)),
            control_plane_url=CP,
        )
        with pytest.raises(EscalationError, match="without a token"):
            await client.call_with_escalation_wait(
                "POST", f"{PEP}/payments", json={"amount": "1"}, poll_s=0.001
            )

    async def test_without_an_injected_client_each_call_opens_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services = FakeServices(states=["approved"])
        real = httpx.AsyncClient

        def with_fake_transport(**kw: object) -> httpx.AsyncClient:
            return real(transport=httpx.MockTransport(services.handle), **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "AsyncClient", with_fake_transport)
        client = AgentIAM(token=services.original, key_set=KEY_SET, clock=frozen_clock())
        response = await client.call("GET", f"{PEP}/invoices/1")
        assert response.status_code == 403
        assert len(services.requests) == 1

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, json={}),
            httpx.Response(403, content=b"not json"),
            httpx.Response(403, json=["a", "list"]),
            httpx.Response(403, json={"reason_code": "APPROVAL_REQUIRED"}),
            httpx.Response(403, json={"reason_code": "APPROVAL_REQUIRED", "escalation_id": "x"}),
        ],
    )
    def test_a_refusal_without_a_usable_escalation_id_is_not_waited_on(
        self, response: httpx.Response
    ) -> None:
        assert sdk_client._escalation_id(response) is None

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            (httpx.Response(429, json={"reason_code": "LEASE_UNAVAILABLE"}), True),
            (httpx.Response(429, json={"reason_code": "RATE_LIMITED"}), False),
            (httpx.Response(429, content=b"slow down"), False),
            (httpx.Response(403, json={"reason_code": "LEASE_UNAVAILABLE"}), False),
        ],
    )
    def test_only_a_lease_refusal_is_ridden_out(
        self, response: httpx.Response, expected: bool
    ) -> None:
        assert sdk_client._lease_unavailable(response) is expected

    @pytest.mark.parametrize(
        "body",
        [None, ["amount"], {"recipient": "x"}, {"amount": "not a number"}],
    )
    def test_fit_to_grant_leaves_what_it_cannot_read_alone(self, body: object) -> None:
        assert sdk_client._fit_to_grant(body, "50000") == body
