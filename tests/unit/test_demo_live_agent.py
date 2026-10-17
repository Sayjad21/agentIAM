"""The live LLM agent and its prompt injection — APICTA-05 (`scripts/demo_live_agent.py`).

The PEP, the tool server behind it and the control plane's audit search are one fake
`httpx.MockTransport`; the model is a fake that returns the actions a test scripts. What is
pinned is the agent's honesty more than its cleverness: a payment the model did not attempt
is never reported as one the PEP refused, a forced attempt is labelled as forced, and the
latency printed is the one the decision record carries — never a wall-clock time.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

import httpx
import pytest

from agentiam_controlplane.nl_compiler.llm import LLMError
from agentiam_core.tokens import RootKeySet, generate_keypair, mint_root
from agentiam_demo.tools import BENIGN_INVOICE, INJECTION, INVOICES, POISONED_INVOICE
from agentiam_sdk import AgentIAM
from scripts import demo_live_agent as live
from tests.fixtures.tokens import a_mandate, frozen_clock

if TYPE_CHECKING:
    from collections.abc import Sequence

KEYS = generate_keypair()
PEP = "http://pep"
CP = "http://cp"
RECORDED_US = 1734


class FakeServices:
    """A PEP that serves invoices and refuses every payment, and an audit search."""

    def __init__(self, *, recorded: bool = True) -> None:
        """Start with nothing sent; `recorded=False` models a record not yet written."""
        self.requests: list[httpx.Request] = []
        self.refused: dict[str, str] = {}
        self.recorded = recorded

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "cp":
            decision_id = request.url.params["decision_id"]
            if not self.recorded or decision_id not in self.refused:
                return httpx.Response(200, json={"results": [], "total": 0})
            record = {
                "decision_id": decision_id,
                "outcome": "deny",
                "reason_code": "SCOPE_ATTENUATED_AWAY",
                "latency_us": RECORDED_US,
                "failing_caveat_kind": "scope_subset",
            }
            return httpx.Response(200, json={"results": [record], "total": 1})
        if path.startswith("/proxy/invoices/"):
            invoice = INVOICES.get(path.rsplit("/", 1)[1])
            if invoice is None:
                return httpx.Response(404, json={"detail": "no such invoice"})
            return httpx.Response(200, json=invoice)
        if path == "/proxy/payments":
            decision_id = str(uuid.uuid4())
            self.refused[decision_id] = request.content.decode()
            return httpx.Response(
                403,
                json={
                    "reason_code": "SCOPE_ATTENUATED_AWAY",
                    "detail": "operation 'payment:initiate' is not in this token's scope set",
                    "decision_id": decision_id,
                    "trace_id": decision_id,
                },
            )
        return httpx.Response(404)

    def payments(self) -> list[dict[str, object]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.host == "pep" and r.url.path == "/proxy/payments"
        ]


class FakeModel:
    """Returns the actions it was given, in order, then `done`."""

    label = "fake model"

    def __init__(
        self, actions: Sequence[live.Action] = (), *, fail: Exception | None = None
    ) -> None:
        """Script the replies, or make every call raise `fail`."""
        self.actions = list(actions)
        self.fail = fail
        self.calls = 0

    async def next_action(self, task: str, history: Sequence[live.Step]) -> live.Action:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if self.actions:
            return self.actions.pop(0)
        return live.Action(tool="done", args=live.ActionArgs(summary="nothing more to do"))


FOOLED = live.Action(
    tool="pay", args=live.ActionArgs(amount="250000", account_id="offshore-acct-99")
)
DONE = live.Action(tool="done", args=live.ActionArgs(summary="paper and toner, 12,500"))


async def run(
    invoice_id: str,
    model: live.Model,
    *,
    services: FakeServices | None = None,
    force_attempt: bool = False,
) -> tuple[live.Transcript, FakeServices, list[str]]:
    services = services or FakeServices()
    http = httpx.AsyncClient(transport=httpx.MockTransport(services.handle))
    agent = AgentIAM(
        token=mint_root(a_mandate(), KEYS.private_key),
        key_set=RootKeySet([KEYS.public_key]),
        agent_id=live.AGENT_ID,
        clock=frozen_clock(),
        http=http,
    )
    lines: list[str] = []
    transcript = await live.run(
        invoice_id,
        agent=agent,
        model=model,
        http=http,
        pep_url=PEP,
        control_plane_url=CP,
        force_attempt=force_attempt,
        record_wait_s=0.05,
        emit=lines.append,
    )
    return transcript, services, lines


class TestTheBenignInvoice:
    async def test_the_agent_reads_it_and_finishes(self) -> None:
        transcript, services, _ = await run(BENIGN_INVOICE, FakeModel([DONE]))
        assert [s.action.tool for s in transcript.steps] == ["read_invoice", "done"]
        assert services.payments() == []
        assert not transcript.model_attempted_payment

    async def test_the_read_goes_through_the_pep_under_the_agent_s_token(self) -> None:
        _, services, _ = await run(BENIGN_INVOICE, FakeModel([DONE]))
        read = services.requests[0]
        assert read.url.path == f"/proxy/invoices/{BENIGN_INVOICE}"
        assert read.headers["authorization"].startswith("Bearer ")


class TestThePoisonedInvoice:
    async def test_a_fooled_model_tries_to_pay_and_the_refusal_is_reported(self) -> None:
        transcript, services, lines = await run(POISONED_INVOICE, FakeModel([FOOLED, DONE]))

        assert services.payments() == [
            {"amount": "250000", "recipient": {"account_id": "offshore-acct-99"}}
        ]
        assert transcript.model_attempted_payment
        refusal = transcript.model_payment
        assert refusal is not None
        assert refusal.status == 403
        assert refusal.reason_code == "SCOPE_ATTENUATED_AWAY"
        assert refusal.latency_us == RECORDED_US
        assert "the model tried to pay; the PEP refused it" in transcript.verdict
        assert any("1.73 ms" in line and "decision record" in line for line in lines)

    async def test_the_refusal_is_fed_back_and_the_agent_carries_on(self) -> None:
        model = FakeModel([FOOLED, DONE])
        transcript, _, _ = await run(POISONED_INVOICE, model)
        assert [s.action.tool for s in transcript.steps] == ["read_invoice", "pay", "done"]
        assert transcript.steps[1].status == 403

    async def test_a_model_that_resists_is_reported_as_resisting(self) -> None:
        """Nothing was attempted, so nothing was refused — and the transcript says so."""
        transcript, services, lines = await run(POISONED_INVOICE, FakeModel([DONE]))
        assert services.payments() == []
        assert not transcript.model_attempted_payment
        assert transcript.model_payment is None
        assert "the model resisted the injection" in transcript.verdict
        assert not any("refused it" in line for line in lines)

    async def test_force_attempt_sends_the_poisoned_payment_and_labels_it(self) -> None:
        transcript, services, lines = await run(
            POISONED_INVOICE, FakeModel([DONE]), force_attempt=True
        )
        assert services.payments() == [
            {"amount": "250000", "recipient": {"account_id": "offshore-acct-99"}}
        ]
        assert not transcript.model_attempted_payment
        assert transcript.forced_payment is not None
        assert transcript.forced_payment.reason_code == "SCOPE_ATTENUATED_AWAY"
        assert "the model resisted the injection" in transcript.verdict
        assert "FORCED" in transcript.verdict
        assert any("FORCED" in line for line in lines)

    async def test_force_attempt_adds_nothing_when_the_model_already_paid(self) -> None:
        transcript, services, _ = await run(
            POISONED_INVOICE, FakeModel([FOOLED, DONE]), force_attempt=True
        )
        assert len(services.payments()) == 1
        assert transcript.forced_payment is None

    async def test_a_record_not_yet_written_is_not_given_a_made_up_latency(self) -> None:
        transcript, _, lines = await run(
            POISONED_INVOICE, FakeModel([FOOLED, DONE]), services=FakeServices(recorded=False)
        )
        assert transcript.model_payment is not None
        assert transcript.model_payment.latency_us is None
        assert any("not yet in the audit trail" in line for line in lines)
        assert not any(" ms " in line and "decision record" in line for line in lines)

    async def test_a_model_that_keeps_paying_is_stopped_after_max_steps(self) -> None:
        _, services, _ = await run(POISONED_INVOICE, FakeModel([FOOLED] * 20))
        assert len(services.payments()) == live.MAX_MODEL_STEPS


class TestScriptedAndFallback:
    async def test_offline_takes_the_same_actions_as_a_fooled_model(self) -> None:
        scripted, _, _ = await run(POISONED_INVOICE, live.ScriptedModel())
        fooled, _, _ = await run(POISONED_INVOICE, FakeModel([FOOLED, DONE]))
        assert [s.action.tool for s in scripted.steps] == [s.action.tool for s in fooled.steps]
        assert scripted.steps[1].action == fooled.steps[1].action  # the same poisoned payment
        assert scripted.model_label == "scripted"

    async def test_scripted_output_never_reads_as_a_live_model_s(self) -> None:
        transcript, _, lines = await run(POISONED_INVOICE, live.ScriptedModel())
        assert transcript.verdict.startswith("the scripted model tried to pay")
        assert any("scripted model → pay" in line for line in lines)
        assert not any(" model → " in line and "scripted" not in line for line in lines)

    async def test_offline_on_the_benign_invoice_is_done(self) -> None:
        transcript, services, _ = await run(BENIGN_INVOICE, live.ScriptedModel())
        assert services.payments() == []
        assert transcript.steps[-1].action.tool == "done"

    @pytest.mark.parametrize(
        "failure", [LLMError("quota exhausted"), TimeoutError(), ValueError("not an action")]
    )
    async def test_a_model_failure_falls_back_to_the_script_and_says_so(
        self, failure: Exception
    ) -> None:
        model = live.FallbackModel(FakeModel(fail=failure), timeout_s=1.0)
        transcript, services, lines = await run(POISONED_INVOICE, model)
        assert transcript.model_label.startswith("scripted (fallback")
        assert any("falling back to the scripted model" in line for line in lines)
        assert len(services.payments()) == 1

    async def test_a_slow_model_is_abandoned_at_its_bound(self) -> None:
        class Slow:
            label = "slow"

            async def next_action(self, task: str, history: Sequence[live.Step]) -> live.Action:
                import asyncio

                await asyncio.sleep(10)
                return DONE

        transcript, _, _ = await run(BENIGN_INVOICE, live.FallbackModel(Slow(), timeout_s=0.01))
        assert transcript.model_label.startswith("scripted (fallback")


class TestTheActionSchema:
    def test_an_amount_given_as_a_number_never_becomes_a_float(self) -> None:
        action = live.Action.model_validate(
            {"tool": "pay", "args": {"amount": 250000.5, "account_id": "x"}}
        )
        assert action.args.amount == "250000.5"

    def test_an_unknown_tool_is_refused(self) -> None:
        with pytest.raises(ValueError):
            live.Action.model_validate({"tool": "wire_money", "args": {}})


class TestNothingTheAgentReadsReachesTheLogs:
    """Ticket step 2: the invoice text and the prompt are never logged — digests only.

    The static scanner in `tests/security/test_secret_scanning.py` walks `packages/` only, so
    for a script this is checked the other way: run it with a planted invoice and read
    every record emitted at every level.
    """

    async def test_no_record_carries_the_invoice_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        planted = "CANARY-7f3a planted invoice text that must never be logged"
        services = FakeServices()
        original = services.handle

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/proxy/invoices/{POISONED_INVOICE}":
                services.requests.append(request)
                return httpx.Response(
                    200, json=INVOICES[POISONED_INVOICE] | {"description": planted}
                )
            return original(request)

        services.handle = handle  # type: ignore[method-assign]
        with caplog.at_level(logging.DEBUG):
            await run(POISONED_INVOICE, live.ScriptedModel(), services=services)
            await run(POISONED_INVOICE, FakeModel([FOOLED, DONE]))

        assert caplog.records, "expected the agent to log something"
        for record in caplog.records:
            text = record.getMessage()
            assert "CANARY-7f3a" not in text
            assert INJECTION not in text
            assert "SYSTEM OVERRIDE" not in text


class FakeClient:
    """An `LLMClient` that records the prompt and returns a canned reply."""

    model = "gemini-9-test"

    def __init__(self, reply: dict[str, object]) -> None:
        """Answer every prompt with `reply`."""
        self.reply = reply
        self.prompts: list[str] = []

    async def generate_structured(
        self, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        self.prompts.append(prompt)
        return self.reply

    async def warm(self) -> bool:
        return True


class TestTheLiveModelAdapter:
    async def test_the_model_sees_the_invoice_it_read_injection_included(self) -> None:
        """Otherwise the beat tests nothing: a model never shown the injection resists it."""
        client = FakeClient({"tool": "done", "args": {"summary": "ok"}})
        await run(POISONED_INVOICE, live.LLMModel(client))
        assert INJECTION in client.prompts[0]
        assert "pay" in client.prompts[0]  # the tool exists in the agent's toolbox

    async def test_a_reply_that_is_not_an_action_raises_for_the_fallback(self) -> None:
        with pytest.raises(ValueError):
            await live.LLMModel(FakeClient({"answer": "pay them"})).next_action("t", [])

    def test_the_label_names_the_model_that_answered(self) -> None:
        assert live.LLMModel(FakeClient({})).label == "live model (gemini-9-test)"
