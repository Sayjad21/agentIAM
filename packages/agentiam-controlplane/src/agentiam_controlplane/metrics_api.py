"""`GET /metrics` — Prometheus exposition for Grafana's Decisions and Budgets dashboards.

T-049, `PLAN.md` §1197. Reuses `count_by_outcome_reason` (the audit chain) and
`build_dashboard` (T-047's own budget snapshot) rather than a third parallel query path that
could drift from what an operator already sees on `/decisions` and `/budgets` — the same
reasoning T-048 used for custody.

A fresh `CollectorRegistry` per scrape, matching `agentiam_pep.app`'s `/metrics` (T-018):
Prometheus polls on an interval, and a process-lifetime registry would accumulate every
`(outcome, reason_code)` or `mandate_id` label combination the process has ever rendered,
including ones no longer current — a closed-out mandate would keep reporting its last value
forever instead of disappearing from the dashboard.

Money crosses from `Decimal` to `float` here, and only here (ADR-047). Rule 4 — "money is
`Decimal`, never `float`" — governs the ledger and everything that computes with it; this
endpoint computes nothing, it renders `db/budget_dashboard.py`'s already-computed `Decimal`
amounts for a Prometheus client that has no arbitrary-precision numeric type. A Grafana gauge
reading `61234.5678` instead of `61234.5678000...` is a display rounding a human never
notices; the ledger and the invariant checker never read this endpoint back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge
from prometheus_client import generate_latest as render_metrics
from starlette.responses import PlainTextResponse

from agentiam_controlplane.db.budget_dashboard import build_dashboard
from agentiam_controlplane.db.decision_metrics import count_by_outcome_reason

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["build_router"]


def build_router(
    *,
    session_factory: Callable[[], AsyncSession] | None,
    now: Callable[[], datetime] | None = None,
) -> APIRouter:
    """Wire `GET /metrics`.

    A `None` `session_factory` yields 503, matching every other console route T-045 onward:
    the console boots without a database and visibly cannot report, rather than a scrape
    target that 500s or a boot failure.
    """
    router = APIRouter(tags=["Metrics"])

    def _now() -> datetime:
        return now() if now else datetime.now(UTC)

    @router.get("/metrics")
    async def metrics() -> PlainTextResponse:
        if session_factory is None:
            raise HTTPException(status_code=503, detail="no database configured")

        registry = CollectorRegistry()
        decisions_total = Gauge(
            "agentiam_controlplane_decisions_total",
            "Decisions recorded in the audit chain, by outcome and reason code.",
            labelnames=("outcome", "reason_code"),
            registry=registry,
        )
        committed_gauge = Gauge(
            "agentiam_controlplane_budget_committed_bdt",
            "Committed spend per mandate pool.",
            labelnames=("mandate_id", "dimension"),
            registry=registry,
        )
        available_gauge = Gauge(
            "agentiam_controlplane_budget_available_bdt",
            "Remaining headroom per mandate pool.",
            labelnames=("mandate_id", "dimension"),
            registry=registry,
        )
        lease_utilization_gauge = Gauge(
            "agentiam_controlplane_lease_utilization_ratio",
            "Settled / granted across a pool's active leases.",
            labelnames=("mandate_id", "dimension"),
            registry=registry,
        )
        invariant_gauge = Gauge(
            "agentiam_controlplane_invariant_ok",
            "1 when T-016's live invariant sweep is clean, 0 on any violation.",
            registry=registry,
        )

        async with session_factory() as session:
            for row in await count_by_outcome_reason(session):
                decisions_total.labels(row.outcome, row.reason_code).set(row.count)

            dashboard = await build_dashboard(session, now=_now())
            for gauge in dashboard.gauges:
                labels = (str(gauge.mandate_id), gauge.dimension)
                committed_gauge.labels(*labels).set(float(gauge.committed))
                available_gauge.labels(*labels).set(float(gauge.available))
                lease_utilization_gauge.labels(*labels).set(
                    float(gauge.lease_settled / gauge.lease_granted) if gauge.lease_granted else 0.0
                )
            invariant_gauge.set(1.0 if dashboard.invariants_ok else 0.0)

        return PlainTextResponse(render_metrics(registry).decode(), media_type=CONTENT_TYPE_LATEST)

    return router
