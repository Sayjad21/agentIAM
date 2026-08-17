"""Structural checks on T-049's committed infra config.

`docker-compose.observability.yml`, `deploy/otel/`, `deploy/prometheus/`, `deploy/tempo/`,
`deploy/grafana/`. None of this runs a container — that is a manual/compose-level check, per
ADR-047: no Testcontainers image exists for "the whole observability stack" the way Postgres
or Keycloak have one. What is checked here is what a unit test *can* prove without Docker:
every file parses, and it says what T-049's accept criterion (`PLAN.md` §1197) requires — two
dashboards with the named panels, wired to a datasource that exists, scraping the two apps'
`/metrics`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"


def _yaml(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _json(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


class TestComposeFile:
    def test_it_parses_and_has_the_four_services(self) -> None:
        compose = _yaml(REPO_ROOT / "docker-compose.observability.yml")
        assert set(compose["services"]) == {"otel-collector", "tempo", "prometheus", "grafana"}

    def test_the_collector_publishes_its_otlp_http_port(self) -> None:
        compose = _yaml(REPO_ROOT / "docker-compose.observability.yml")
        ports = compose["services"]["otel-collector"]["ports"]
        assert any(str(p).startswith("4318:") for p in ports)

    def test_grafana_mounts_the_committed_dashboards_and_provisioning(self) -> None:
        compose = _yaml(REPO_ROOT / "docker-compose.observability.yml")
        volumes = " ".join(compose["services"]["grafana"]["volumes"])
        assert "deploy/grafana/provisioning" in volumes
        assert "deploy/grafana/dashboards" in volumes


class TestOtelCollectorConfig:
    def test_it_receives_otlp_http_and_forwards_to_tempo(self) -> None:
        config = _yaml(DEPLOY / "otel" / "otel-collector-config.yaml")
        assert config["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "0.0.0.0:4318"
        assert config["exporters"]["otlp/tempo"]["endpoint"] == "tempo:4317"
        traces = config["service"]["pipelines"]["traces"]
        assert traces["receivers"] == ["otlp"]
        assert traces["exporters"] == ["otlp/tempo"]


class TestTempoConfig:
    def test_it_receives_otlp_grpc_on_the_port_the_collector_targets(self) -> None:
        config = _yaml(DEPLOY / "tempo" / "tempo.yaml")
        grpc = config["distributor"]["receivers"]["otlp"]["protocols"]["grpc"]
        assert grpc["endpoint"] == "0.0.0.0:4317"


class TestPrometheusConfig:
    def test_it_scrapes_both_apps_metrics_endpoints(self) -> None:
        config = _yaml(DEPLOY / "prometheus" / "prometheus.yml")
        jobs = {job["job_name"]: job for job in config["scrape_configs"]}
        assert set(jobs) == {"agentiam-controlplane", "agentiam-pep"}
        for job in jobs.values():
            assert job["metrics_path"] == "/metrics"
            assert job["static_configs"][0]["targets"]


class TestGrafanaProvisioning:
    def test_the_datasources_match_what_the_dashboards_reference(self) -> None:
        config = _yaml(DEPLOY / "grafana" / "provisioning" / "datasources" / "datasources.yaml")
        uids = {ds["uid"] for ds in config["datasources"]}
        assert uids == {"prometheus", "tempo"}

    def test_the_dashboard_provider_points_at_the_committed_directory(self) -> None:
        config = _yaml(DEPLOY / "grafana" / "provisioning" / "dashboards" / "dashboards.yaml")
        provider = config["providers"][0]
        assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
        assert provider["type"] == "file"


class TestDecisionsDashboard:
    def _dashboard(self) -> dict[str, Any]:
        return _json(DEPLOY / "grafana" / "dashboards" / "decisions.json")

    def test_it_has_the_three_panels_the_accept_criterion_names(self) -> None:
        """PLAN.md §1197: "Decisions (rate, outcome mix, reason codes)"."""
        titles = {p["title"].lower() for p in self._dashboard()["panels"]}
        assert any("rate" in t for t in titles)
        assert any("outcome mix" in t for t in titles)
        assert any("reason code" in t for t in titles)

    def test_every_panel_targets_the_provisioned_prometheus_datasource(self) -> None:
        for panel in self._dashboard()["panels"]:
            for target in panel["targets"]:
                assert target["datasource"]["uid"] == "prometheus"

    def test_the_queries_use_the_metric_family_metrics_api_actually_exports(self) -> None:
        exprs = " ".join(
            target["expr"] for panel in self._dashboard()["panels"] for target in panel["targets"]
        )
        assert "agentiam_controlplane_decisions_total" in exprs


class TestBudgetsDashboard:
    def _dashboard(self) -> dict[str, Any]:
        return _json(DEPLOY / "grafana" / "dashboards" / "budgets.json")

    def test_it_has_the_panels_the_accept_criterion_names(self) -> None:
        """PLAN.md §1197: "Budgets (per-mandate spend, lease utilization)"."""
        titles = {p["title"].lower() for p in self._dashboard()["panels"]}
        assert any("spend" in t for t in titles)
        assert any("lease utilization" in t for t in titles)

    def test_every_panel_targets_the_provisioned_prometheus_datasource(self) -> None:
        for panel in self._dashboard()["panels"]:
            for target in panel["targets"]:
                assert target["datasource"]["uid"] == "prometheus"

    def test_the_queries_use_the_metric_families_metrics_api_actually_exports(self) -> None:
        exprs = " ".join(
            target["expr"] for panel in self._dashboard()["panels"] for target in panel["targets"]
        )
        assert "agentiam_controlplane_budget_committed_bdt" in exprs
        assert "agentiam_controlplane_budget_available_bdt" in exprs
        assert "agentiam_controlplane_lease_utilization_ratio" in exprs
        assert "agentiam_controlplane_invariant_ok" in exprs


class TestDashboardUidsAreUnique:
    def test_the_two_dashboards_do_not_collide(self) -> None:
        decisions = _json(DEPLOY / "grafana" / "dashboards" / "decisions.json")
        budgets = _json(DEPLOY / "grafana" / "dashboards" / "budgets.json")
        assert decisions["uid"] != budgets["uid"]
