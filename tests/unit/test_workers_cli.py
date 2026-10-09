"""CLI wiring for --workers, --stateless-http and the hidden --thread-pool-size.

The resolution rules themselves are covered in test_serving.py; these tests
pin how the host applies them: which runner is called, what reaches it, what
the diagnostic settings record, and how a worker rebuilds the server.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import couchbase
import fastmcp
import pytest
from click.testing import CliRunner

import mcp_server
from cb_mcp.core.app import run_app
from cb_mcp.utils.cli_params import decode_worker_config, encode_worker_config
from cb_mcp.utils.logging import ParsedLogLevel, ParsedLogSinks


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    """The Couchbase SDK's configure_logging is one-shot per process."""
    with patch.object(couchbase, "configure_logging"):
        yield


@pytest.fixture(autouse=True)
def clean_worker_env(monkeypatch):
    """_run_workers publishes into os.environ; keep tests independent."""
    monkeypatch.delenv(mcp_server.WORKER_CONFIG_ENV_VAR, raising=False)
    yield
    os.environ.pop(mcp_server.WORKER_CONFIG_ENV_VAR, None)


def _invoke(args: list[str], env: dict[str, str] | None = None):
    """Run the CLI with every way of actually serving patched out."""
    captured: dict = {}

    def capture(*fastmcp_args, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        captured["fastmcp_name"] = fastmcp_args[0] if fastmcp_args else None
        return MagicMock()

    with (
        patch("cb_mcp.core.app.FastMCP", side_effect=capture),
        patch("mcp_server.run_app") as run_app,
        patch("mcp_server.uvicorn.run") as uvicorn_run,
        patch("mcp_server.send_install_ping") as ping,
    ):
        result = CliRunner().invoke(mcp_server.main, args, env=env)
    captured.update(run_app=run_app, uvicorn_run=uvicorn_run, ping=ping)
    return result, captured


def _settings_from(lifespan) -> dict:
    out: dict = {}

    async def drive():
        async with lifespan(MagicMock()) as app_context:
            out.update(app_context.settings)

    with patch("cb_mcp.core.app.send_install_ping"):
        asyncio.run(drive())
    return out


SUBCOMMANDS = [
    pytest.param([], id="operational"),
    pytest.param(["operational-insights"], id="operational-insights"),
]


class TestHelp:
    @pytest.mark.parametrize("argv", SUBCOMMANDS)
    def test_public_flags_listed_hidden_flag_not(self, argv):
        result = CliRunner().invoke(mcp_server.main, [*argv, "--help"])
        assert result.exit_code == 0, result.output
        assert "--workers" in result.output
        assert "--stateless-http" in result.output
        assert "--thread-pool-size" not in result.output


class TestSingleProcess:
    def test_default_is_stateful_single_process(self):
        result, cap = _invoke(["--transport", "http"])
        assert result.exit_code == 0, result.output
        cap["uvicorn_run"].assert_not_called()
        assert cap["run_app"].call_args.kwargs["stateless_http"] is False

    @pytest.mark.parametrize("argv", SUBCOMMANDS)
    def test_stateless_flag_reaches_run_app(self, argv):
        result, cap = _invoke(
            [*argv, "--transport", "http", "--stateless-http", "true"]
        )
        assert result.exit_code == 0, result.output
        assert cap["run_app"].call_args.kwargs["stateless_http"] is True

    def test_stateless_env_var(self):
        result, cap = _invoke(
            ["--transport", "http"], env={"CB_MCP_STATELESS_HTTP": "true"}
        )
        assert result.exit_code == 0, result.output
        assert cap["run_app"].call_args.kwargs["stateless_http"] is True

    def test_settings_report_topology_and_effective_pool_size(self):
        result, cap = _invoke(
            ["--transport", "http"], env={"CB_MCP_THREAD_POOL_SIZE": "80"}
        )
        assert result.exit_code == 0, result.output
        settings = _settings_from(cap["lifespan"])
        assert settings["workers"] == 1
        assert settings["stateless_http"] is False
        assert settings["thread_pool_size"] == 80

    def test_unset_pool_size_reports_the_runtime_default(self):
        result, cap = _invoke(["--transport", "http"])
        assert result.exit_code == 0, result.output
        assert _settings_from(cap["lifespan"])["thread_pool_size"] == 40

    def test_single_process_sends_its_own_startup_ping(self):
        result, cap = _invoke(["--transport", "http"])
        assert result.exit_code == 0, result.output
        cap["ping"].assert_not_called()  # the lifespan sends it instead


class TestMultipleWorkers:
    def test_runs_the_uvicorn_supervisor_not_run_app(self):
        result, cap = _invoke(
            ["--transport", "http", "--workers", "3", "--port", "9100"]
        )
        assert result.exit_code == 0, result.output
        cap["run_app"].assert_not_called()
        args, kwargs = cap["uvicorn_run"].call_args
        assert args == (mcp_server.WORKER_APP_IMPORT_STRING,)
        assert kwargs["factory"] is True
        assert kwargs["workers"] == 3
        assert kwargs["port"] == 9100
        assert kwargs["lifespan"] == "on"

    def test_supervisor_sends_exactly_one_ping(self):
        result, cap = _invoke(["--transport", "http", "--workers", "2"])
        assert result.exit_code == 0, result.output
        cap["ping"].assert_called_once_with("http", server_id="operational")

    def test_publishes_the_worker_config(self):
        result, _ = _invoke(
            ["--transport", "http", "--workers", "2", "--read-only-mode", "false"]
        )
        assert result.exit_code == 0, result.output
        server_id, params = decode_worker_config(
            os.environ[mcp_server.WORKER_CONFIG_ENV_VAR]
        )
        assert server_id == "operational"
        assert params["workers"] == 2
        assert params["read_only_mode"] is False

    def test_workers_env_var(self):
        result, cap = _invoke(["--transport", "http"], env={"CB_MCP_WORKERS": "2"})
        assert result.exit_code == 0, result.output
        assert cap["uvicorn_run"].call_args.kwargs["workers"] == 2


class TestUsageErrors:
    """Each of these must fail before anything is served."""

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["--workers", "2"], "requires --transport=http"),
            (
                ["--transport", "http", "--workers", "2", "--stateless-http", "false"],
                "requires stateless HTTP",
            ),
            (["--transport", "sse", "--stateless-http", "true"], "no stateless mode"),
            (
                [
                    "--transport",
                    "http",
                    "--stateless-http",
                    "true",
                    "--read-only-mode",
                    "false",
                    "--confirmation-required-tools",
                    "upsert_document_by_id",
                ],
                "upsert_document_by_id",
            ),
            (
                [
                    "operational-insights",
                    "--transport",
                    "http",
                    "--workers",
                    "2",
                ],
                "operational-insights",
            ),
            (["--transport", "http", "--workers", "0"], "0"),
        ],
        ids=[
            "workers-on-stdio",
            "workers-stateful",
            "sse-stateless",
            "confirmation-stateless",
            "oi-workers",
            "zero-workers",
        ],
    )
    def test_rejected(self, args, message):
        result, cap = _invoke(args)
        assert result.exit_code == 2, result.output
        assert message in result.output
        cap["run_app"].assert_not_called()
        cap["uvicorn_run"].assert_not_called()

    def test_confirmation_with_workers_is_rejected(self):
        result, cap = _invoke(
            [
                "--transport",
                "http",
                "--workers",
                "2",
                "--read-only-mode",
                "false",
                "--confirmation-required-tools",
                "upsert_document_by_id",
            ]
        )
        assert result.exit_code == 2, result.output
        assert "elicitation" in result.output
        cap["uvicorn_run"].assert_not_called()


class TestWorkerConfigCodec:
    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(
                [
                    "--transport",
                    "http",
                    "--workers",
                    "2",
                    "--log-level",
                    "nonsense",
                    "--log-sinks",
                    "stderr,bogus",
                ],
                id="operational",
            ),
            pytest.param(
                [
                    "operational-insights",
                    "--transport",
                    "http",
                    "--stateless-http",
                    "true",
                    "--client-cert-password",
                    "s3cret",
                ],
                id="operational-insights",
            ),
        ],
    )
    def test_round_trips_real_click_params(self, argv):
        """Decode must reproduce exactly what Click handed the subcommand."""
        seen: dict = {}

        def record(server_id, params):
            seen["server_id"], seen["params"] = server_id, dict(params)

        with patch("mcp_server._start_server", side_effect=record):
            result = CliRunner().invoke(
                mcp_server.main,
                argv,
                env={"EMBEDDING_API_KEY": "k", "EMBEDDING_PROVIDER": "openai"},
            )
        assert result.exit_code == 0, result.output

        server_id, params = decode_worker_config(
            encode_worker_config(seen["server_id"], seen["params"])
        )
        assert server_id == seen["server_id"]
        assert params == seen["params"]

    def test_rejects_values_that_are_not_json_native(self):
        params = {
            "log_level": ParsedLogLevel("INFO", None),
            "log_sinks": ParsedLogSinks({"stderr"}, []),
            "surprise": object(),
        }
        with pytest.raises(TypeError):
            encode_worker_config("operational", params)


class TestCreateApp:
    def test_requires_the_supervisor_config(self):
        with pytest.raises(RuntimeError, match=mcp_server.WORKER_CONFIG_ENV_VAR):
            mcp_server.create_app()

    def test_rebuilds_the_supervised_server_stateless_with_host_pid_logs(
        self, tmp_path
    ):
        log_file = str(tmp_path / "mcp_server.log")
        result, _ = _invoke(
            [
                "--transport",
                "http",
                "--workers",
                "2",
                "--log-file",
                log_file,
            ]
        )
        assert result.exit_code == 0, result.output

        built_with: dict = {}
        real_build = mcp_server._build_server

        def spy(server_id, params, **kwargs):
            built_with.update(server_id=server_id, params=dict(params), **kwargs)
            return real_build(server_id, params, **kwargs)

        app = MagicMock()
        with (
            patch("mcp_server._build_server", side_effect=spy),
            patch("cb_mcp.core.app.FastMCP", return_value=app),
            patch("mcp_server.socket.gethostname", return_value="web-1.example"),
        ):
            returned = mcp_server.create_app()

        assert built_with["server_id"] == "operational"
        assert built_with["send_startup_ping"] is False
        assert built_with["params"]["log_file"] == str(
            tmp_path / f"mcp_server.web-1.{os.getpid()}.log"
        )
        app.http_app.assert_called_once_with(stateless_http=True)
        assert returned is app.http_app.return_value


class TestFastMCPStatelessSetting:
    """FastMCP's own FASTMCP_STATELESS_HTTP, read once at import into settings."""

    @pytest.fixture
    def fastmcp_stateless(self, monkeypatch):
        monkeypatch.setattr(fastmcp.settings, "stateless_http", True)

    @pytest.mark.usefixtures("fastmcp_stateless")
    def test_is_honoured_and_reported(self):
        result, cap = _invoke(["--transport", "http"])
        assert result.exit_code == 0, result.output
        assert cap["run_app"].call_args.kwargs["stateless_http"] is True
        assert _settings_from(cap["lifespan"])["stateless_http"] is True

    @pytest.mark.usefixtures("fastmcp_stateless")
    def test_explicit_flag_overrides_it(self):
        result, cap = _invoke(["--transport", "http", "--stateless-http", "false"])
        assert result.exit_code == 0, result.output
        assert cap["run_app"].call_args.kwargs["stateless_http"] is False

    @pytest.mark.usefixtures("fastmcp_stateless")
    def test_rejects_confirmation_tools(self):
        """The gap this closes: previously startup passed, then prompts failed."""
        result, cap = _invoke(
            [
                "--transport",
                "http",
                "--read-only-mode",
                "false",
                "--confirmation-required-tools",
                "upsert_document_by_id",
            ]
        )
        assert result.exit_code == 2, result.output
        assert "FASTMCP_STATELESS_HTTP" in result.output
        cap["run_app"].assert_not_called()


@pytest.mark.parametrize(
    ("transport", "requested", "expected"),
    [
        ("http", False, {"host": "h", "port": 1, "stateless_http": False}),
        ("http", True, {"host": "h", "port": 1, "stateless_http": True}),
        ("sse", True, {"host": "h", "port": 1, "stateless_http": False}),
        ("stdio", True, {}),
    ],
)
def test_run_app_always_passes_the_resolved_mode(transport, requested, expected):
    """Never left unset, so FastMCP cannot fall back to its own env var."""
    mcp = MagicMock()
    run_app(mcp, transport=transport, host="h", port=1, stateless_http=requested)
    kwargs = mcp.run.call_args.kwargs
    kwargs.pop("transport"), kwargs.pop("show_banner")
    assert kwargs == expected
