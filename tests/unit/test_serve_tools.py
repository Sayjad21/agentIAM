"""`scripts/serve_tools.py` — the NFR-2 baseline upstream, with `--workers` (APICTA-08).

Nothing binds a port here: `uvicorn.run` is replaced, and what is asserted is what it was
asked to do.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import serve_tools


class Recorder:
    def __init__(self) -> None:
        """Record nothing yet."""
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    import uvicorn

    recorder = Recorder()
    monkeypatch.setattr(uvicorn, "run", recorder)
    return recorder


class TestWorkers:
    def test_one_worker_by_default(self) -> None:
        assert serve_tools.build_parser().parse_args([]).workers == 1

    def test_the_count_reaches_uvicorn(self, recorder: Recorder) -> None:
        serve_tools.main(["--workers", "4", "--port", "9"])
        (_args, kwargs) = recorder.calls[0]
        assert kwargs["workers"] == 4

    def test_the_app_is_passed_as_a_factory_import_string(self, recorder: Recorder) -> None:
        """Uvicorn refuses `workers > 1` for an app object: each worker must import it."""
        serve_tools.main(["--workers", "2"])
        (args, kwargs) = recorder.calls[0]
        assert args[0] == "agentiam_demo.tools:create_tools_app"
        assert kwargs["factory"] is True

    def test_the_import_string_names_a_real_factory(self) -> None:
        import importlib

        module, _, name = serve_tools.APP.partition(":")
        assert callable(getattr(importlib.import_module(module), name))

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_fewer_than_one_worker_is_refused(
        self, value: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            serve_tools.build_parser().parse_args(["--workers", value])
        assert "at least 1" in capsys.readouterr().err

    def test_host_and_port_still_reach_uvicorn(self, recorder: Recorder) -> None:
        serve_tools.main(["--host", "0.0.0.0", "--port", "18081"])  # noqa: S104
        (_args, kwargs) = recorder.calls[0]
        assert (kwargs["host"], kwargs["port"]) == ("0.0.0.0", 18081)  # noqa: S104
