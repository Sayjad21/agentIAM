"""PEP settings, and the timeout budget in particular (`agentiam_pep.config`) — T-018.

The measured fact behind most of this: **`httpx`'s transport-level `retries` covers
connection establishment only, and it multiplies the connect timeout.** With
`retries=2` and a 2 s timeout, a refused connection took **6.56 s** to surface — three
attempts, not one. A configuration that reads as "give up after 2 seconds" gives up after
six and a half.

That matters here more than in most services. NFR-1 budgets the in-process decision at
p99 < 1 ms, and a PEP that hangs for six seconds on a dead upstream has blown any
end-to-end budget regardless of how fast the decision was.
"""

from __future__ import annotations

import pytest

from agentiam_pep.config import PepSettings


class TestDefaults:
    def test_the_upstream_url_is_required(self) -> None:
        """No default. A proxy pointed somewhere by accident is worse than one that won't start."""
        with pytest.raises((TypeError, ValueError)):
            PepSettings()  # type: ignore[call-arg]

    def test_an_empty_upstream_url_is_refused(self) -> None:
        """Distinct from omitting it: an empty string is what a blank env var produces."""
        with pytest.raises(ValueError, match="upstream_base_url"):
            PepSettings(upstream_base_url="")

    def test_the_defaults_are_stated(self) -> None:
        settings = PepSettings(upstream_base_url="http://up.test")
        assert settings.connect_timeout_s > 0
        assert settings.read_timeout_s > 0
        assert settings.connect_retries >= 0


class TestTimeoutBudget:
    def test_the_worst_case_accounts_for_retries(self) -> None:
        """`retries=N` means `N+1` connect attempts, so the budget is `(N+1) * connect`.

        Stated as a property rather than a constant so it stays true when the defaults
        change.
        """
        settings = PepSettings(
            upstream_base_url="http://up.test", connect_timeout_s=2.0, connect_retries=2
        )
        assert settings.worst_case_connect_s == pytest.approx(6.0)

    def test_no_retries_means_one_attempt(self) -> None:
        settings = PepSettings(
            upstream_base_url="http://up.test", connect_timeout_s=2.0, connect_retries=0
        )
        assert settings.worst_case_connect_s == pytest.approx(2.0)

    def test_the_default_worst_case_is_bounded(self) -> None:
        """Whatever the defaults are, a dead upstream must not hold a request for long.

        Five seconds is the line: past that a caller has almost certainly given up, and
        the PEP is holding a connection for nobody.
        """
        settings = PepSettings(upstream_base_url="http://up.test")
        assert settings.worst_case_connect_s <= 5.0

    def test_retries_cannot_be_negative(self) -> None:
        with pytest.raises(ValueError, match="connect_retries"):
            PepSettings(upstream_base_url="http://up.test", connect_retries=-1)

    @pytest.mark.parametrize("field", ["connect_timeout_s", "read_timeout_s"])
    def test_timeouts_must_be_positive(self, field: str) -> None:
        """Zero would mean "no timeout" to httpx — the opposite of what it reads like."""
        with pytest.raises(ValueError, match=field):
            if field == "connect_timeout_s":
                PepSettings(upstream_base_url="http://up.test", connect_timeout_s=0)
            else:
                PepSettings(upstream_base_url="http://up.test", read_timeout_s=0)


class TestHttpxTimeout:
    def test_it_builds_a_timeout_httpx_understands(self) -> None:
        settings = PepSettings(
            upstream_base_url="http://up.test", connect_timeout_s=1.5, read_timeout_s=9.0
        )
        timeout = settings.timeout
        assert timeout.connect == pytest.approx(1.5)
        assert timeout.read == pytest.approx(9.0)

    def test_the_read_timeout_is_the_long_one(self) -> None:
        """Connecting should be quick; a slow upstream *response* is normal.

        Reversing these is a common misconfiguration and produces a proxy that gives up on
        healthy slow endpoints while waiting patiently for dead hosts.
        """
        settings = PepSettings(upstream_base_url="http://up.test")
        assert settings.read_timeout_s > settings.connect_timeout_s


class TestTheProductionClient:
    """`build_client` is what actually runs in deployment.

    Every other test in the suite injects its own client so the upstream can be an
    in-process app — which means the real construction path would otherwise never execute
    anywhere except production.
    """

    async def test_it_applies_the_configured_pool_limits(self) -> None:
        settings = PepSettings(
            upstream_base_url="http://up.test",
            max_connections=7,
            max_keepalive_connections=3,
        )
        assert settings.limits.max_connections == 7
        assert settings.limits.max_keepalive_connections == 3

    async def test_it_builds_a_usable_client_pointed_at_the_upstream(self) -> None:
        settings = PepSettings(upstream_base_url="http://up.test", read_timeout_s=12.0)
        client = settings.build_client()
        try:
            assert str(client.base_url) == "http://up.test"
            assert client.timeout.read == pytest.approx(12.0)
        finally:
            await client.aclose()


class TestEnvironment:
    def test_settings_can_come_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deployed configuration is environment, not code (12-factor, `PLAN.md` §4)."""
        monkeypatch.setenv("AGENTIAM_PEP_UPSTREAM_BASE_URL", "http://erp.internal:9000")
        monkeypatch.setenv("AGENTIAM_PEP_READ_TIMEOUT_S", "42")
        settings = PepSettings.from_env()
        assert settings.upstream_base_url == "http://erp.internal:9000"
        assert settings.read_timeout_s == pytest.approx(42.0)

    def test_a_missing_upstream_url_in_the_environment_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTIAM_PEP_UPSTREAM_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="AGENTIAM_PEP_UPSTREAM_BASE_URL"):
            PepSettings.from_env()

    def test_an_unparseable_number_names_the_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo in a deployment manifest should say which line to look at.

        `float("6s")` on its own raises `could not convert string to float: '6s'`, which
        tells whoever is on the pager nothing about where it came from.
        """
        monkeypatch.setenv("AGENTIAM_PEP_UPSTREAM_BASE_URL", "http://up.test")
        monkeypatch.setenv("AGENTIAM_PEP_READ_TIMEOUT_S", "6s")
        with pytest.raises(ValueError, match="AGENTIAM_PEP_READ_TIMEOUT_S"):
            PepSettings.from_env()
