"""CH-8 — Ollama down (T-052, `PLAN.md` §13.2).

*Expected: template fallback; no hot-path impact.*

Both halves hold as of APICTA-01. **The template fallback (T-031) exists since Part D**: when
the compiler's model is unreachable, a sentence in one of the demo's policy shapes is compiled
by the offline template compiler, and anything else gets the list of shapes it understands. The
model's own failure is still checked to be typed and bounded rather than hanging, and — the part
that matters for a running system — it never touches the request path.

Ollama sits behind two unrelated callers, and only one of them is on the hot path:

* the **NL→Cedar compiler** (`nl_compiler/ollama_client.py`), an operator authoring flow
  where a 300 s budget is deliberate (ADR-038);
* the **drift oracle** (`agentiam_pep/drift.py`), which `decide()` step 6 consults on every
  request that carries both intent headers.

So "no hot-path impact" is a claim about the second, and spec 09 §5 already fixes what it
should be: drift is advisory and an outage of an advisory heuristic must not deny. `decide()`
catches `OracleUnavailable` and carries on.

**The hot-path claim holds as of APICTA-07, and it did not before.** `EmbeddingClient` used
to hold a *synchronous* `httpx.Client` that `decide()` step 6 called from the event loop, so an
unreachable model cost every uncached request its full timeout — and every other in-flight
request waited behind it. Measured then: **p99 6,066 ms** against a 2 s embedding timeout
(STATUS gap 22). Now `decide()` never touches the network: the pipeline awaits the model for at
most `assess_timeout_s` (120 ms by default, measured against the real model), and a fetch that
outlives the bound finishes in the background. Both drift scenarios below assert the absence of
the stall against a baseline measured in the same run, where they used to pin its presence.

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

from agentiam_controlplane.nl_compiler.compiler import compile_nl_to_policy
from agentiam_controlplane.nl_compiler.ollama_client import OllamaClient, OllamaError
from agentiam_core.corpus import CORPUS_SOURCE
from agentiam_pep.drift import DEFAULT_ASSESS_TIMEOUT_S, RuleBasedDriftOracle
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

#: `EmbeddingClient`'s own default: how long a *background* fetch may run. Before APICTA-07 a
#: request paid this in full; now it pays at most `ASSESS_TIMEOUT_S`.
EMBED_TIMEOUT_S = 2.0

#: `drift.DEFAULT_ASSESS_TIMEOUT_S` — how long one request may wait for the model.
ASSESS_TIMEOUT_S = DEFAULT_ASSESS_TIMEOUT_S

#: Headroom over the baseline p99 for one bounded wait plus scheduling noise on a loaded
#: runner. Generous on purpose: the regression this guards against cost *seconds*.
STALL_MARGIN_MS = ASSESS_TIMEOUT_S * 1000 + 400

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
    oracle = RuleBasedDriftOracle(
        base_url=f"http://127.0.0.1:{_dead_port()}", timeout=EMBED_TIMEOUT_S
    )
    built = await build_stack(
        ledger_url=postgres_url,
        mandate=mandate,
        pep_id="pep-ch08",
        lease_size=LEASE_SIZE,
        drift_oracle=oracle,
    )
    yield built
    await built.aclose()
    await oracle.embeddings.aclose()


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

            # The hot-path half, which held only from APICTA-07 on. Every one of these requests
            # misses the drift cache and reaches step 6, so each one either waited at most the
            # bound or the stall is back.
            assert report.percentile(99) < STALL_MARGIN_MS + 300, (
                f"p99 was {report.percentile(99)} ms with Ollama unreachable, against a "
                f"{ASSESS_TIMEOUT_S * 1000:.0f} ms bound on how long a request waits for "
                f"the model — the embedding call is blocking the request path again"
            )

            run.note(
                "Both halves of the expectation hold: the template fallback (T-031) answers "
                "the compiler when the model is down (CH-08-compiler), and the hot path is "
                "unaffected."
            )
            run.note(
                f"An unreachable Ollama costs the hot path p50 {report.percentile(50)} ms / "
                f"p99 {report.percentile(99)} ms: each request waits at most "
                f"{ASSESS_TIMEOUT_S * 1000:.0f} ms for the model and then goes on with drift "
                f"unassessed (spec 09 §5 fails it open). Before APICTA-07 the same scenario "
                f"measured p99 6,066 ms, because the embedding client was synchronous and "
                f"called from the event loop (STATUS gap 22, closed). All {report.ok} requests "
                f"were allowed."
            )
            run.note(
                "Measured on the Windows development host, and it invalidated this scenario's "
                "first premise: a connect to a *closed* 127.0.0.1 port does not refuse, it raises "
                "ConnectTimeout after ~3 s — port 9 included. There is no cheap-failure mode "
                "for an unreachable local service on Windows."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"

    async def test_a_black_holed_ollama_does_not_stall_the_event_loop(
        self, migrated_engine: AsyncEngine, postgres_url: str
    ) -> None:
        """The measurement the fast-failure case cannot make, now asserting the fix.

        A black hole accepts the connection and never answers, so a client waiting on it
        waits for its whole timeout. Before APICTA-07 that wait happened inside `decide()`,
        on the event loop, and every concurrent request queued behind it — this test pinned
        that (p99 >= half a 2 s timeout). Now a request waits at most the assess bound and
        the fetch carries on in the background, so concurrent requests stay within one bound
        of the baseline.
        """
        parsed_port = _dead_port()
        proxy = await FaultProxy("127.0.0.1", parsed_port).start()
        proxy.cut(FaultMode.BLACKHOLE)

        mandate = a_mandate(uuid.uuid4(), total=POOL_TOTAL, task_intent_text=TASK_INTENT)
        await make_pool_budget(migrated_engine, mandate_id=mandate.mandate_id, total=POOL_TOTAL)
        oracle = RuleBasedDriftOracle(
            base_url=f"http://127.0.0.1:{proxy.port}", timeout=EMBED_TIMEOUT_S
        )
        stack = await build_stack(
            ledger_url=postgres_url,
            mandate=mandate,
            pep_id="pep-ch08-blackhole",
            lease_size=LEASE_SIZE,
            drift_oracle=oracle,
        )

        try:
            async with chaos_run(
                "CH-08-blackhole",
                title="Ollama black-holed (accepts, never answers)",
                expected="requests still allowed; each waits at most the assess bound",
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
                    total=9,
                    concurrency=3,
                )
                wall_s = time.perf_counter() - started
                run.load(stalled)

                run.measure("embedding_timeout_s", EMBED_TIMEOUT_S)
                run.measure("assess_timeout_ms", ASSESS_TIMEOUT_S * 1000)
                run.measure("baseline_p99_ms", baseline.percentile(99))
                run.measure("blackholed_p99_ms", stalled.percentile(99))
                run.measure("nine_requests_three_at_a_time_wall_s", round(wall_s, 2))
                run.measure("blackholed_connections", proxy.connections_blackholed)
                run.measure("assessments_timed_out", oracle.outcomes["timeout"])

                assert proxy.connections_blackholed > 0, (
                    "the oracle never reached the black hole, so nothing was measured"
                )
                assert oracle.outcomes["timeout"] > 0, (
                    "no assessment hit the bound, so the black hole was never waited on"
                )
                assert stalled.ok == 9, (
                    f"a hung advisory oracle denied requests: {stalled.by_reason}"
                )
                assert stalled.dropped == 0, f"requests were dropped: {stalled.errors}"

                # The fix, pinned. A request waits at most one bound for the model; if p99
                # climbs back towards the embedding timeout, the wait is unbounded again or
                # has moved back onto the loop.
                assert stalled.percentile(99) < baseline.percentile(99) + STALL_MARGIN_MS, (
                    f"p99 was {stalled.percentile(99)} ms against a baseline p99 of "
                    f"{baseline.percentile(99)} ms and a {ASSESS_TIMEOUT_S * 1000:.0f} ms "
                    f"bound — a hung model is stalling requests again (STATUS gap 22)"
                )
                run.note(
                    f"A black-holed Ollama now costs a request at most the "
                    f"{ASSESS_TIMEOUT_S * 1000:.0f} ms assess bound: p99 "
                    f"{stalled.percentile(99)} ms against a baseline p99 of "
                    f"{baseline.percentile(99)} ms, nine requests three at a time in "
                    f"{wall_s:.1f} s. Before APICTA-07 each uncached request paid the full "
                    f"{EMBED_TIMEOUT_S} s embedding timeout on the event loop, and three "
                    f"concurrent requests took over 6 s. Outcomes are unchanged — spec 09 §5 "
                    f"fails drift open."
                )

            assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
        finally:
            await stack.aclose()
            await oracle.embeddings.aclose()
            proxy.heal()
            await proxy.aclose()

    async def test_the_compiler_fails_over_to_templates_inside_the_bound(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """The operator path: a dead model must not hang the authoring flow, and beat 5 survives.

        The client fails typed and bounded, and — since APICTA-01 Part D — the compiler then
        answers a sentence in one of the demo's shapes with the offline template compiler.

        `OllamaClient` hardcodes `http://127.0.0.1:11434` — deliberately, as T-028's
        no-egress guarantee — so there is no injection point and the base URL is overridden
        here. That is the one thing this test fakes; everything else is the shipped client.
        """
        async with chaos_run(
            "CH-08-compiler",
            title="Ollama down — the NL compiler",
            expected="typed error inside the timeout; the template compiler answers",
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

            # And the whole compile, through the same dead client, still produces a policy.
            started = time.perf_counter()
            output = await compile_nl_to_policy(
                "Quarantine agent agt-payer",
                client=client,
                current_policy=CORPUS_SOURCE,
                timeout_s=COMPILER_TIMEOUT_S * 3,
            )
            fallback_s = time.perf_counter() - started
            run.measure("compiled_by", output.compiled_by)
            run.measure("fallback_compile_s", round(fallback_s, 3))
            assert output.compiled_by == "template", output.fallback_reason
            assert output.cedar_source is not None
            assert 'Agent::"agt-payer"' in output.cedar_source
            assert fallback_s < COMPILER_TIMEOUT_S * 3

            run.note(
                f"With Ollama unreachable the compiler fails over to the offline template "
                f"compiler (APICTA-01 Part D, T-031): a quarantine sentence compiled in "
                f"{fallback_s:.2f} s, appended to the current policy. DEMO.md drill F-2 now has "
                f"a recovery for the demo's policy shapes; other sentences get the list of "
                f"shapes the offline compiler understands."
            )

        assert run.sidecar.clean, f"invariant violated: {run.sidecar.violations}"
