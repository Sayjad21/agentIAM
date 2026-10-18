"""`scripts/run_load_test.py`'s two-machine split — APICTA-08.

On one host the generator, three uvicorn processes and Postgres share a CPU, and the
committed 100 RPS enforcement p99 ranged 1.75-74.7 ms across three runs. `--role server` runs
the system under test on one machine; `--role client` runs the same three tiers from another,
over the link between them. The default — no `--role` — stays today's single-host run.

Nothing here starts Docker or a server. The measurement loop is driven with a fake `drive`,
because what these protect is the plumbing: which URL each tier hits, which token it sends,
what the result file says about where it was measured.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import run_load_test


def parse(*argv: str) -> Any:
    args = run_load_test.build_parser().parse_args(list(argv))
    run_load_test.validate(args)
    return args


CLIENT = (
    "--role",
    "client",
    "--pep-url",
    "http://10.0.0.5:18080",
    "--passthrough-url",
    "http://10.0.0.5:18081",
    "--upstream-url",
    "http://10.0.0.5:18082",
)


class TestRoles:
    def test_no_role_is_the_single_host_run(self) -> None:
        args = parse()
        assert args.role is None
        assert args.profiles == [100, 500]

    def test_a_client_needs_every_address_and_a_token(self, tmp_path: Path) -> None:
        token = tmp_path / "token"
        token.write_text("t", encoding="utf-8")
        for missing in ("--pep-url", "--passthrough-url", "--upstream-url"):
            argv = list(CLIENT)
            at = argv.index(missing)
            del argv[at : at + 2]
            with pytest.raises(SystemExit):
                parse(*argv, "--token-file", str(token))
        with pytest.raises(SystemExit):
            parse(*CLIENT)

    def test_rps_is_the_ticket_s_name_for_profiles(self, tmp_path: Path) -> None:
        token = tmp_path / "token"
        token.write_text("t", encoding="utf-8")
        args = parse(*CLIENT, "--token-file", str(token), "--rps", "500", "--seconds", "30")
        assert args.profiles == [500]
        assert args.seconds == 30.0

    def test_client_flags_are_refused_without_the_client_role(self) -> None:
        with pytest.raises(SystemExit):
            parse("--pep-url", "http://10.0.0.5:18080")

    def test_a_server_binds_every_interface_by_default(self) -> None:
        assert parse("--role", "server").bind == "0.0.0.0"  # noqa: S104

    def test_a_single_host_run_stays_on_loopback(self) -> None:
        assert parse().bind == "127.0.0.1"

    def test_each_topology_writes_its_own_file(self, tmp_path: Path) -> None:
        """A two-machine run must not overwrite the committed single-host figures."""
        token = tmp_path / "token"
        token.write_text("t", encoding="utf-8")
        single = run_load_test.results_path(parse())
        two = run_load_test.results_path(parse(*CLIENT, "--token-file", str(token)))
        assert single.name == "nfr2-load.json"
        assert two.name == "nfr2-two-host.json"


class TestClientProfile:
    def test_the_profile_points_each_tier_at_the_server(self, tmp_path: Path) -> None:
        token = tmp_path / "token"
        token.write_text("tok-123\n", encoding="utf-8")
        profile = run_load_test.client_profile(parse(*CLIENT, "--token-file", str(token)))
        assert profile == {
            "base_url": "http://10.0.0.5:18080",
            "passthrough_url": "http://10.0.0.5:18081",
            "upstream_url": "http://10.0.0.5:18082",
            "token": "tok-123",
        }

    def test_trailing_slashes_are_dropped(self, tmp_path: Path) -> None:
        token = tmp_path / "token"
        token.write_text("t", encoding="utf-8")
        argv = [a + "/" if a.startswith("http") else a for a in CLIENT]
        profile = run_load_test.client_profile(parse(*argv, "--token-file", str(token)))
        assert not any(profile[k].endswith("/") for k in ("base_url", "upstream_url"))

    def test_an_empty_token_file_is_refused(self, tmp_path: Path) -> None:
        token = tmp_path / "token"
        token.write_text("\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            run_load_test.client_profile(parse(*CLIENT, "--token-file", str(token)))


class TestServerHandoff:
    def test_the_printed_command_is_the_client_invocation(self) -> None:
        command = run_load_test.client_command(
            advertise="10.0.0.5",
            pep_port=18080,
            passthrough_port=18081,
            tools_port=18082,
            token_file=Path("/srv/token"),
        )
        args = run_load_test.build_parser().parse_args(command.split()[4:])
        assert command.startswith("uv run python scripts/run_load_test.py ")
        assert args.role == "client"
        assert args.pep_url == "http://10.0.0.5:18080"
        assert args.passthrough_url == "http://10.0.0.5:18081"
        assert args.upstream_url == "http://10.0.0.5:18082"
        assert args.token_file == Path("/srv/token")

    def test_the_advertised_address_is_routable(self) -> None:
        """Never the wildcard the server binds: a client cannot connect to 0.0.0.0."""
        address = run_load_test.primary_address()
        assert address != "0.0.0.0"  # noqa: S104
        assert address.count(".") == 3

    def test_the_token_file_is_one_git_ignores(self) -> None:
        ignore = Path(run_load_test._REPO_ROOT, ".gitignore").read_text(encoding="utf-8")
        assert "docs/benchmarks/.perf-profile-*" in ignore
        assert run_load_test.SERVER_TOKEN_FILE.name.startswith(".perf-profile-")


class FakeSample:
    def __init__(self, url: str, token: str | None, rps: int) -> None:
        """A sample whose latency says which tier produced it."""
        self.url = url
        self.token = token
        self.rps = rps
        self.base = 1.0 if ":18082" in url else 2.0 if ":18081" in url else 3.0
        self.achieved_rps = float(rps)

    def percentile(self, p: float) -> float:
        return self.base + p / 1000

    def as_dict(self) -> dict[str, Any]:
        series = {f"p{p}": self.percentile(p) for p in (50, 95, 99)}
        return {"achieved_rps": self.achieved_rps, "service_ms": series, "target_rps": self.rps}


class TestMeasureRecordsWhereItRan:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list[FakeSample]:
        import tests.perf.driver as driver

        seen: list[FakeSample] = []

        def fake(url: str, *, token: str | None, rps: int, seconds: float) -> FakeSample:
            sample = FakeSample(url, token, rps)
            seen.append(sample)
            return sample

        monkeypatch.setattr(driver, "drive", fake)
        return seen

    PROFILE: dict[str, str] = {  # noqa: RUF012 - read-only fixture
        "base_url": "http://10.0.0.5:18080",
        "passthrough_url": "http://10.0.0.5:18081",
        "upstream_url": "http://10.0.0.5:18082",
        "token": "tok",
    }

    def test_only_the_enforcing_tier_sends_the_token(self, calls: list[FakeSample]) -> None:
        run_load_test._measure(self.PROFILE, [100], 1.0, topology="two-host")
        by_tier = {c.base: c.token for c in calls}
        assert by_tier == {1.0: None, 2.0: None, 3.0: "tok"}

    def test_the_result_says_which_topology_and_harness(self, calls: list[FakeSample]) -> None:
        results = run_load_test._measure(self.PROFILE, [100], 1.0, topology="two-host")
        assert results["topology"] == "two-host"
        assert results["harness"] == run_load_test.HARNESS
        assert {"platform", "python", "cpus"} <= set(results["host"])

    def test_the_subtraction_is_still_tier_three_minus_tier_two(
        self, calls: list[FakeSample]
    ) -> None:
        results = run_load_test._measure(self.PROFILE, [100], 1.0, topology="two-host")
        assert results["profiles"][0]["enforcement_overhead_ms"]["p99"] == pytest.approx(1.0)

    def test_the_result_round_trips_through_json(self, calls: list[FakeSample]) -> None:
        results = run_load_test._measure(self.PROFILE, [100, 500], 1.0, topology="single-host")
        assert json.loads(json.dumps(results)) == results


class TestTheFlameGraphIsNotMeasured:
    """py-spy samples the PEP it attaches to, and that costs latency.

    Measured: the profiled run's 100 RPS enforcement p99 was 14.2 ms, the two unprofiled runs
    7.0 and 7.7 ms. So the flame graph is taken in a pass of its own, before the counted runs,
    and never enters the statistics.
    """

    def test_the_profiled_pass_is_extra_and_uncounted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import tests.perf.driver as driver

        profiled: list[bool] = []
        spying = {"on": False}

        class Spy:
            stdout = None

            def wait(self, timeout: float) -> int:
                spying["on"] = False
                return 1

        def start(pid: int, output: Path, seconds: float) -> Spy:
            spying["on"] = True
            return Spy()

        def fake(url: str, *, token: str | None, rps: int, seconds: float) -> FakeSample:
            if token is not None:
                profiled.append(spying["on"])
            return FakeSample(url, token, rps)

        monkeypatch.setattr(driver, "drive", fake)
        monkeypatch.setattr(run_load_test, "_start_pyspy", start)
        results = run_load_test._measure(
            TestMeasureRecordsWhereItRan.PROFILE,
            [100, 500],
            1.0,
            flamegraph=tmp_path / "f.svg",
            server_pid=1,
            repeat=3,
        )
        # One profiled pass, then three unprofiled runs at each rate.
        assert profiled == [True] + [False] * 6
        assert [p["runs"] for p in results["profiles"]] == [3, 3]
        assert len(results["profiles"][0]["all_runs"]) == 3


class TestTheHandoffIsVisible:
    def test_output_is_line_buffered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Measured: `--role server > log` showed nothing for five minutes.

        Redirected, Python block-buffers stdout, so the client command the server prints —
        the one thing the person at the other machine needs — sat in a buffer until exit.
        """
        import sys

        calls: list[dict[str, object]] = []

        class Stdout:
            def reconfigure(self, **kwargs: object) -> None:
                calls.append(kwargs)

            def write(self, text: str) -> int:
                return len(text)

            def flush(self) -> None:
                return None

        monkeypatch.setattr(sys, "stdout", Stdout())
        with pytest.raises(SystemExit):
            run_load_test.main(["--role", "client"])
        assert calls == [{"line_buffering": True}]
