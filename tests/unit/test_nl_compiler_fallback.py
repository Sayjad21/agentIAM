"""When the model cannot answer, the compiler still does — APICTA-01 Part D, T-031.

Three ways the hosted model fails on stage, each measured or seen before: unreachable (venue
Wi-Fi, DEMO.md F-1/F-2), too slow (a rate-limited free tier backs off for up to 30 s per retry),
and an answer that is not the shape asked for. In all three a sentence in one of the template
shapes still compiles, and the output says which compiler produced it and why.

And the change that makes a compiled sentence activatable at all: the compiler is given the
current policy and returns the whole updated policy. Measured before: every single-rule policy
scored 27/51 on the corpus gate standalone, so no English sentence could ever be activated.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from agentiam_controlplane.nl_compiler.compiler import (
    _SYSTEM_PROMPT,
    CompilerOutput,
    compile_nl_to_policy,
)
from agentiam_controlplane.nl_compiler.llm import LLMError
from agentiam_core.corpus import CORPUS_SOURCE

QUARANTINE = "Quarantine agent agt-payer"
_GOOD = {
    "cedar_source": CORPUS_SOURCE + '\nforbid(principal == Agent::"agt-payer", action, resource);',
    "tests": [],
}


class Model:
    """A stand-in model whose failure mode a test chooses."""

    def __init__(
        self,
        *,
        answer: dict[str, Any] | None = None,
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        """Answer `answer`, raise `error`, or hang for `delay_s` first."""
        self.answer = answer if answer is not None else _GOOD
        self.error = error
        self.delay_s = delay_s
        self.prompts: list[str] = []

    async def generate_structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return self.answer

    async def warm(self) -> bool:
        return True


class TestFallback:
    async def test_an_unreachable_model_falls_back_to_templates(self) -> None:
        output = await compile_nl_to_policy(
            QUARANTINE,
            client=Model(error=LLMError("Gemini network error: connection refused")),
            current_policy=CORPUS_SOURCE,
        )
        assert output.compiled_by == "template"
        assert output.fallback_reason is not None and "network error" in output.fallback_reason
        assert output.cedar_source is not None
        assert output.cedar_source.startswith(CORPUS_SOURCE.rstrip())
        assert 'Agent::"agt-payer"' in output.cedar_source
        assert output.tests

    async def test_a_slow_model_is_abandoned_at_the_bound(self) -> None:
        started = time.perf_counter()
        output = await compile_nl_to_policy(
            QUARANTINE,
            client=Model(delay_s=5.0),
            current_policy=CORPUS_SOURCE,
            timeout_s=0.1,
        )
        assert time.perf_counter() - started < 1.0
        assert output.compiled_by == "template"
        assert output.fallback_reason is not None and "0.1" in output.fallback_reason

    async def test_a_malformed_answer_falls_back(self) -> None:
        output = await compile_nl_to_policy(
            QUARANTINE,
            client=Model(answer={"tests": "not a list"}),
            current_policy=CORPUS_SOURCE,
        )
        assert output.compiled_by == "template"

    async def test_offline_mode_never_asks_the_model(self) -> None:
        model = Model()
        output = await compile_nl_to_policy(
            QUARANTINE, client=model, current_policy=CORPUS_SOURCE, offline=True
        )
        assert model.prompts == []
        assert output.compiled_by == "template"
        assert output.fallback_reason == "offline mode"

    async def test_an_unrecognised_sentence_gets_the_shapes_not_a_guess(self) -> None:
        output = await compile_nl_to_policy(
            "Make everything safer",
            client=Model(error=LLMError("down")),
            current_policy=CORPUS_SOURCE,
        )
        assert output.cedar_source is None
        assert output.clarifying_question is not None
        assert "No payments over 100000" in output.clarifying_question

    async def test_the_fallback_does_not_log_the_statement(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        statement = "Quarantine agent agt-payer because alice@example.com said so"
        await compile_nl_to_policy(
            statement, client=Model(error=LLMError("down")), current_policy=CORPUS_SOURCE
        )
        assert "alice@example.com" not in caplog.text


class TestTheModelEditsTheWholePolicy:
    async def test_the_current_policy_is_in_the_prompt(self) -> None:
        model = Model()
        output = await compile_nl_to_policy(QUARANTINE, client=model, current_policy=CORPUS_SOURCE)
        assert output.compiled_by == "model"
        assert output.fallback_reason is None
        (prompt,) = model.prompts
        assert "CURRENT POLICY" in prompt
        assert CORPUS_SOURCE.strip() in prompt

    async def test_without_a_current_policy_the_prompt_is_unchanged(self) -> None:
        # The 30-case dataset was measured (27/30) with exactly this prompt; it must not
        # move, or the measurement stops describing the compiler.
        model = Model(answer={"cedar_source": "permit(principal, action, resource);"})
        await compile_nl_to_policy(QUARANTINE, client=model)
        assert model.prompts == [f"{_SYSTEM_PROMPT}\n\nREQUIREMENT:\n{QUARANTINE}\n"]

    async def test_a_successful_compile_is_labelled_as_the_models(self) -> None:
        output = await compile_nl_to_policy(QUARANTINE, client=Model())
        assert isinstance(output, CompilerOutput)
        assert output.compiled_by == "model"

    async def test_the_model_cannot_claim_to_be_the_template_compiler(self) -> None:
        forged = dict(_GOOD) | {"compiled_by": "template", "fallback_reason": "offline mode"}
        output = await compile_nl_to_policy(QUARANTINE, client=Model(answer=forged))
        assert output.compiled_by == "model"
        assert output.fallback_reason is None

    def test_the_labels_are_not_in_the_schema_the_model_sees(self) -> None:
        properties = CompilerOutput.model_json_schema()["properties"]
        assert "compiled_by" not in properties
        assert "fallback_reason" not in properties
