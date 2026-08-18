"""CH-8 — Ollama down (T-052, `PLAN.md` §13.2).

*Expected: template fallback; no hot-path impact.*

Half of that expectation cannot be met and the results table says so rather than quietly
scoring the scenario green. **T-031, the template fallback, is deferred** (`PLAN.md` §21,
`STATUS.md`), so when the compiler's backend is unreachable there is nothing to fall back
*to*. What the scenario checks instead is the honest remainder: the failure is typed, it is
bounded by the client's own timeout rather than hanging, and — the part that actually
matters for a running system — it never touches the request path.

Ollama sits behind two unrelated callers, and only one of them is on the hot path:

* the **NL→Cedar compiler** (`nl_compiler/ollama_client.py`), an operator authoring flow
  where a 300 s budget is deliberate (ADR-038);
* the **drift oracle** (`agentiam_pep/drift.py`), which `decide()` step 6 consults on every
  request that carries both intent headers.

So "no hot-path impact" is a claim about the second, and spec 09 §5 already fixes what it
should be: drift is advisory and an outage of an advisory heuristic must not deny. `decide()`
catches `OracleUnavailable` and carries on.

**The hot-path claim does not hold, and the number is the point of this scenario.**
`EmbeddingClient` holds a *synchronous* `httpx.Client`, and `decide()` step 6 calls it from
the event loop. Scoring one uncached request costs **three** embed calls — the task text,
the action template, and the rendered action — so an unreachable model costs roughly three
timeouts per request, and it costs them on the loop, where every other in-flight request
waits behind them. Measured here: **p99 6,066 ms** against a 2 s embedding timeout, with
every request still correctly allowed.

This scenario was first written around a premise that turned out to be false: that a
*refused* connection surfaces in milliseconds and only a *black hole* costs the timeout. A
probe says otherwise on the development host — a connect to a closed `127.0.0.1` port does
not refuse, it raises `ConnectTimeout` after ~3 s, and so does port 9, which nothing has
ever bound. So there is no fast-failure mode to contrast against here, and both tests below
measure the same cost by two different routes rather than a fast path and a slow one. The
platform note is worth keeping: *"Ollama is down"* is not a cheap error on Windows.

`PLAN.md` §13.2's CH-9 (embedding service down) expects *strict scopes escalate*. The
shipped `decide()` fails **open** on an unavailable oracle in every mode, per spec 09 §5.
CH-9 is deferred, so this is recorded rather than resolved — but the two documents disagree
and someone should pick one.
"""

from __future__ import annotations

import socket
import time
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient, OllamaError
from agentiam_pep.drift import RuleBasedDriftOracle
from tests.chaos.faultproxy import FaultMode, FaultProxy
from tests.chaos.harness import chaos_run, drive_load
from tests.chaos.pepstack import PepStack, a_mandate, build_stack, make_pool_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.chaos

POOL_TOTAL = Decimal("5000.0000")
LEASE_SIZE = Decimal("1000.0000")
PAYMENT = Decimal("5.0000")

#: `EmbeddingClient`'s own default. Short enough that a stalled call is visible in a test.
EMBED_TIMEOUT_S = 2.0

#: Scoring one uncached request embeds three texts — the task, the action template and the
#: rendered action — so an unreachable model costs about three timeouts. Measured at 6,066 ms
#: against a 2 s timeout. The bound is generous around that rather than tight, because the
#: claim being pinned is *"this costs timeouts, not microseconds"*, not a precise figure.
HOT_PATH_FLOOR_MS = EMBED_TIMEOUT_S * 1000
HOT_PATH_CEILING_MS = EMBED_TIMEOUT_S * 1000 * 6

#: The compiler's timeout for this scenario. Its real default is 300 s (ADR-038), which is
#: correct for an authoring flow and useless in a chaos run — what is under test is that
#: the failure is bounded by *whatever* the budget is, not that the budget is small.
COMPILER_TIMEOUT_S = 2.0


def _dead_port() -> int:
    """A port nothing is listening on.

    Bind, read the port, release. Racy in principle — something could claim it in the gap —
    and it is the standard way to get a port that is almost certainly refusing.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


#: The task this scenario's mandate is bound to. Constant, because the mandate's
#: `intent_hash` has to equal `hash_object` of it — otherwise step 4 refuses with
#: `INTENT_MISMATCH` and drift is never reached (see `pepstack.a_mandate`).
TASK_INTENT = "reconcile the quarterly supplier invoices for the Dhaka office"


def _intents(index: int) -> dict[str, str]:
    """Headers that force the oracle to actually embed something.

    `RuleBasedDriftOracle` short-circuits when the two texts are equal and again when the
    action's words are a subset of the task's, and it caches by `(task, action)`. A request
    whose action text is unique and lexically disjoint from the task is the only kind that
    reaches `embed()` — and reaching `embed()` is the entire point of this scenario.
    """
    return {
        "AgentIAM-Task-Intent": TASK_INTENT,
        "AgentIAM-Action-Intent": f"dispatch remittance batch {index} via correspondent bank",
    }


@pytest.fixture
async def stack_without_ollama(
    migrated_engine: AsyncEngine, postgres_url: str
) -> AsyncIterator[PepStack]:
    """A PEP whose drift oracle points at a port nothing answers on."""
    mandate = a_mandate(uuid.uuid4(), total=POOL_TOTAL, task_intent_text=TASK_INTENT)
    await make_pool_budget(migrated_engine, mandate_id=mandate.mandate_id, total=POOL_TOTAL)
    built = await build_stack(
        ledger_url=postgres_url,
        mandate=mandate,
        pep_id="pep-ch08",
        lease_size=LEASE_SIZE,
        drift_oracle=RuleBasedDriftOracle(
            base_url=f"http://127.0.0.1:{_dead_port()}", timeout=EMBED_TIMEOUT_S
        ),
    )
    yield built
    await built.aclose()


class TestOllamaDown:
    async def test_the_hot_path_is_unaffected_when_ollama_refuses(
        self, stack_without_ollama: PepStack, migrated_engine: AsyncEngine
    ) -> None:
        stack = stack_without_ollama

        async with chaos_run(
            "CH-08",
            title="Ollama down",
            expected="template fallback; no hot-path impact",
            engine=migrated_engine,
        ) as run:
            report = await drive_load(
                lambda i: stack.pay(PAYMENT, **_intents(i)),
                label="ollama refusing connections",
                total=12,
                concurrency=3,
            )
            run.load(report)

            run.measure("served_with_ollama_down", report.ok)
            run.measure("hot_path_p99_ms", report.percentile(99))
            run.measure("hot_path_p50_ms", report.percentile(50))
            run.measure("embedding_timeout_s", EMBED_TIMEOUT_S)

            # The half of the expectation that does hold, and it is the important half.
            assert report.ok == 12, (
                f"an unreachable *advisory* oracle denied requests: {report.by_reason}. "
                f"Spec 09 §5 is explicit that drift must not fail closed"
            )
            assert report.dropped == 0, f"requests were dropped: {report.errors}"

            # The half that does not. Asserted as a floor as well as a ceiling: if this
            # ever drops below one timeout, `EmbeddingClient` has stopped blocking — which
            # is the fix, and this test and STATUS.md gap 22 should both be rewritten then.
            assert HOT_PATH_FLOOR_MS <= report.percentile(99) < HOT_PATH_CEILING_MS, (
                f"p99 was {report.percentile(99)} ms against a {EMBED_TIMEOUT_S} s embedding "
                f"timeout. Below one timeout means the synchronous embedding call is no "
                f"longer on the hot path (good — rewrite this); far above six means "
                f"something is retrying more than the three embeds a scoring call makes"
            )

            run.note(
                "The template fallback PLAN.md §13.2 expects for CH-8 does not exist: T-031 "
                "is deferred (PLAN.md §21). CH-8 is therefore PARTIAL on both halves — the "
                "fallback is unimplemented, and 'no hot-path impact' is false."
            )
            run.note(
                f"An unreachable Ollama costs the hot path p50 {report.percentile(50)} ms / "
                f"p99 {report.percentile(99)} ms against a {EMBED_TIMEOUT_S} s embedding "
                f"timeout — roughly three timeouts, because scoring one request embeds the "
                f"task, the action template and the rendered action, and `EmbeddingClient` "
                f"is a *synchronous* httpx.Client called from the event loop (ADR-037 noted "
                f"the shape; this puts a number on it). Outcomes stay correct: all "
                f"{report.ok} requests were allowed, because spec 09 §5 fails drift open. "
                f"See STATUS.md gap 22."
            )
            run.note(
                "Measured on the development host, and it invalidated this scenario's first "
                "premise: a connect to a *closed* 127.0.0.1 port does not refuse, it raises "
                "ConnectTimeout after ~3 s — port 9 included. There is no cheap-failure mode "
                "for an unreachable local service on Windows."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"

    async def test_a_black_holed_ollama_stalls_the_event_loop(
        self, migrated_engine: AsyncEngine, postgres_url: str
    ) -> None:
        """The measurement the fast-failure case cannot make.

        `EmbeddingClient` holds an `httpx.Client` — synchronous — and `decide()` calls it
        from the event loop. When the far end refuses, that costs nothing worth measuring.
        When the far end accepts and never answers, the call blocks for the full timeout,
        and because it blocks the *loop* rather than a task, every other in-flight request
        waits behind it too.

        The outcome is still correct: nothing is denied, because spec 09 §5's fail-open is
        implemented where it should be. The cost is latency, and the number goes in the
        results table rather than in a footnote.
        """
        parsed_port = _dead_port()
        proxy = await FaultProxy("127.0.0.1", parsed_port).start()
        proxy.cut(FaultMode.BLACKHOLE)

        mandate = a_mandate(uuid.uuid4(), total=POOL_TOTAL, task_intent_text=TASK_INTENT)
        await make_pool_budget(migrated_engine, mandate_id=mandate.mandate_id, total=POOL_TOTAL)
        stack = await build_stack(
            ledger_url=postgres_url,
            mandate=mandate,
            pep_id="pep-ch08-blackhole",
            lease_size=LEASE_SIZE,
            drift_oracle=RuleBasedDriftOracle(
                base_url=f"http://127.0.0.1:{proxy.port}", timeout=EMBED_TIMEOUT_S
            ),
        )

        try:
            async with chaos_run(
                "CH-08-blackhole",
                title="Ollama black-holed (accepts, never answers)",
                expected="requests still allowed; the latency cost is measured, not assumed",
                engine=migrated_engine,
            ) as run:
                baseline = await drive_load(
                    lambda _i: stack.pay(PAYMENT),
                    label="no intent headers (oracle not consulted)",
                    total=6,
                    concurrency=3,
                )
                run.load(baseline)
                assert baseline.ok == 6

                started = time.perf_counter()
                stalled = await drive_load(
                    lambda i: stack.pay(PAYMENT, **_intents(i)),
                    label="ollama black-holed",
                    total=3,
                    concurrency=3,
                )
                wall_s = time.perf_counter() - started
                run.load(stalled)

                run.measure("embedding_timeout_s", EMBED_TIMEOUT_S)
                run.measure("baseline_p99_ms", baseline.percentile(99))
                run.measure("blackholed_p99_ms", stalled.percentile(99))
                run.measure("three_concurrent_requests_wall_s", round(wall_s, 2))
                run.measure("blackholed_connections", proxy.connections_blackholed)

                assert proxy.connections_blackholed > 0, (
                    "the oracle never reached the black hole, so nothing was measured"
                )
                assert stalled.ok == 3, (
                    f"a hung advisory oracle denied requests: {stalled.by_reason}"
                )
                assert stalled.dropped == 0, f"requests were dropped: {stalled.errors}"

                # The finding, pinned. `EmbeddingClient` is synchronous, so a black-holed
                # model costs each uncached scoring call its full timeout. If this ever
                # stops being true — because the client went async, or moved off the loop —
                # this assertion is where that shows up, and the note below should go.
                assert stalled.percentile(99) >= EMBED_TIMEOUT_S * 1000 * 0.5, (
                    f"p99 was {stalled.percentile(99)} ms against a {EMBED_TIMEOUT_S} s "
                    f"embedding timeout: the stall this test exists to measure did not "
                    f"happen, so either the client is no longer blocking or the oracle was "
                    f"never consulted — check which before relaxing this"
                )
                run.note(
                    f"A black-holed Ollama costs every uncached drift scoring call its full "
                    f"{EMBED_TIMEOUT_S} s timeout, on the event loop, because "
                    f"`EmbeddingClient` holds a synchronous httpx.Client (ADR-037 already "
                    f"noted the shape). Three concurrent requests took {wall_s:.1f} s wall "
                    f"against a baseline p99 of {baseline.percentile(99)} ms. Outcomes stay "
                    f"correct — spec 09 §5 fails open — but 'no hot-path impact' is only "
                    f"true for a *refused* Ollama, not a hung one. See STATUS.md gap 22."
                )

            assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
        finally:
            await stack.aclose()
            proxy.heal()
            await proxy.aclose()

    async def test_the_compiler_fails_typed_and_bounded_with_no_template_fallback(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The operator path: a dead model must not hang the authoring flow.

        `OllamaClient` hardcodes `http://127.0.0.1:11434` — deliberately, as T-028's
        no-egress guarantee — so there is no injection point and the base URL is overridden
        here. That is the one thing this test fakes; everything else is the shipped client.
        """
        async with chaos_run(
            "CH-08-compiler",
            title="Ollama down — the NL compiler",
            expected="typed error inside the timeout; template fallback is unimplemented",
            engine=migrated_engine,
        ) as run:
            client = OllamaClient(timeout=COMPILER_TIMEOUT_S)
            # Deliberate: see the docstring. The attribute is private because the address
            # is a security property, not a setting.
            client._base_url = f"http://127.0.0.1:{_dead_port()}"

            started = time.perf_counter()
            warmed = await client.warm()
            warm_s = time.perf_counter() - started
            run.measure("warm_returned", warmed)
            run.measure("warm_s", round(warm_s, 3))
            assert warmed is False, "warm() must report failure rather than raise at startup"

            started = time.perf_counter()
            with pytest.raises(OllamaError) as caught:
                await client.generate_structured("warm-up", schema={"type": "object"})
            failed_after = time.perf_counter() - started

            run.measure("generate_failed_after_s", round(failed_after, 3))
            run.measure("generate_error", type(caught.value).__name__)
            assert failed_after < COMPILER_TIMEOUT_S * 3, (
                f"the compiler took {failed_after:.1f} s to give up against a "
                f"{COMPILER_TIMEOUT_S} s budget — a dead model must not hang the console"
            )

            run.note(
                "No template fallback exists to exercise: T-031 is deferred (PLAN.md §21), "
                "so demo beat 5 has no F-2 recovery while that stays true. This is the "
                "resumption trigger STATUS.md records for T-031."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
