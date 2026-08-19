"""Targeted CLI option behavior tests.
These tests are about pinning
down specific user-visible CLI behaviors that integration tests don't
actively verify, like flag-over-env-var precedence.

Add tests sparingly here. If a behavior is already exercised end-to-end
by an integration test, prefer adding an assertion there instead of
adding an in-process Click test.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import anyio
import anyio.to_thread
import pytest
from click.testing import CliRunner

import cb_mcp.utils.logging as logmod
import mcp_server
from cb_mcp.auth import OAuthConfigError, resolve_oauth
from cb_mcp.utils.constants import SCOPE_READ, SCOPE_WRITE


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    """Couchbase SDK ``configure_logging`` is one-shot per process.

    ``mcp_server.main`` calls it for real via ``configure_logging``; without
    this patch the second test in the process raises
    ``InvalidArgumentException`` ("Another logger has already been
    initialized"). Patch the ``couchbase`` symbol as imported into our logging
    module, matching the fixture in test_configure_logging.py.
    """
    with patch.object(logmod.couchbase, "configure_logging"):
        yield


def _capture_lifespan(args: list[str], env: dict[str, str]):
    """Invoke ``mcp_server.main`` with FastMCP mocked and capture the
    lifespan closure for inspection.

    The lifespan closes over the resolved settings dict, so driving it
    lets the test assert which value (flag vs env var) actually won.
    """
    fake_instance = MagicMock()
    captured: dict = {}

    def capture(*args_, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        return fake_instance

    runner = CliRunner()
    with patch("mcp_server.FastMCP", side_effect=capture):
        result = runner.invoke(mcp_server.main, args, env=env, catch_exceptions=False)

    assert result.exit_code == 0, result.output
    return captured["lifespan"], fake_instance


def test_command_line_flag_overrides_env_var() -> None:
    """A ``--connection-string`` flag must win over the
    ``CB_CONNECTION_STRING`` env var.

    This is Click's documented default precedence (CLI > env). The test
    exists so a future option-config refactor — e.g., adding an
    ``envvar=`` precedence override or switching to a custom resolver —
    can't silently flip the precedence without a failing test.
    """
    env = {
        **os.environ,
        "CB_CONNECTION_STRING": "couchbase://from-env",
    }

    lifespan_fn, fake_mcp = _capture_lifespan(
        ["--connection-string", "couchbase://from-flag"],
        env=env,
    )

    async def drive() -> None:
        async with lifespan_fn(fake_mcp) as app_context:
            assert app_context.settings["connection_string"] == "couchbase://from-flag"

    asyncio.run(drive())


def test_env_var_used_when_flag_absent() -> None:
    """When only the env var is set (no flag), the env var value must
    flow through to ``app_context.settings`` — i.e., env vars are still
    consulted, they just lose to explicit flags."""
    env = {
        **os.environ,
        "CB_CONNECTION_STRING": "couchbase://from-env",
    }

    lifespan_fn, fake_mcp = _capture_lifespan([], env=env)

    async def drive() -> None:
        async with lifespan_fn(fake_mcp) as app_context:
            assert app_context.settings["connection_string"] == "couchbase://from-env"

    asyncio.run(drive())


class TestWorkerOptions:
    """``--workers``/``--stateless-http`` wiring at the CLI boundary.

    The helpers in test_multiprocess.py cover the resolution rules; these pin
    down that ``main`` routes to the right serving path and hands the *resolved*
    topology onward, since that is what worker processes replay.
    """

    def _invoke(self, args: list[str]):
        """Run ``main`` with both serving paths stubbed out.

        Returns ``(result, fake_mcp, run_workers)`` so a test can assert which
        path ran and with what.
        """
        fake_mcp = MagicMock()
        runner = CliRunner()
        with (
            patch("mcp_server.FastMCP", return_value=fake_mcp),
            patch("mcp_server.run_workers") as run_workers,
        ):
            result = runner.invoke(
                mcp_server.main, args, env=os.environ.copy(), catch_exceptions=False
            )
        return result, fake_mcp, run_workers

    def test_multi_worker_hands_resolved_params_to_supervisor(self):
        result, fake_mcp, run_workers = self._invoke(
            ["--transport", "http", "--workers", "3"]
        )

        assert result.exit_code == 0, result.output
        run_workers.assert_called_once()
        params = run_workers.call_args.args[0]
        assert params["workers"] == 3
        # Resolved from "None means decide for me" — workers replay this value
        # rather than re-deriving it.
        assert params["stateless_http"] is True
        # The supervisor sends the one startup telemetry event itself.
        assert params["send_startup_ping"] is False
        # The in-process server must not also be started.
        fake_mcp.run.assert_not_called()

    def test_single_worker_runs_in_process_and_forwards_stateless_mode(self):
        result, fake_mcp, run_workers = self._invoke(
            ["--transport", "http", "--stateless-http", "true"]
        )

        assert result.exit_code == 0, result.output
        run_workers.assert_not_called()
        fake_mcp.run.assert_called_once()
        assert fake_mcp.run.call_args.kwargs["stateless_http"] is True

    def test_single_worker_http_defaults_to_stateful(self):
        _, fake_mcp, _ = self._invoke(["--transport", "http"])
        assert fake_mcp.run.call_args.kwargs["stateless_http"] is False

    def test_stdio_run_kwargs_omit_network_options(self):
        """``run_stdio_async`` takes no host/port/stateless_http, so passing
        them through would raise instead of starting the server."""
        _, fake_mcp, _ = self._invoke([])
        assert fake_mcp.run.call_args.kwargs == {
            "transport": "stdio",
            "show_banner": False,
        }

    def test_multi_worker_on_stdio_is_a_usage_error(self):
        runner = CliRunner()
        result = runner.invoke(
            mcp_server.main, ["--workers", "2"], env=os.environ.copy()
        )
        assert result.exit_code == 2
        assert "requires --transport=http" in result.output

    def test_invalid_stateless_http_starts_anyway_on_the_default(self):
        """A malformed boolean must not stop the server from booting: it falls
        back to the worker-count default and the server still starts."""
        result, fake_mcp, run_workers = self._invoke(
            ["--transport", "http", "--stateless-http", "mabye"]
        )

        assert result.exit_code == 0, result.output
        run_workers.assert_not_called()
        assert fake_mcp.run.call_args.kwargs["stateless_http"] is False

    def test_invalid_stateless_http_does_not_block_multi_worker(self):
        """Same input under --workers: the default resolves to True, which is
        what multi-worker needs, so the run proceeds."""
        result, _, run_workers = self._invoke(
            ["--transport", "http", "--workers", "2", "--stateless-http", "nope"]
        )

        assert result.exit_code == 0, result.output
        assert run_workers.call_args.args[0]["stateless_http"] is True

    def test_stateless_http_false_is_overridden_under_multi_worker(self):
        """Previously a usage error; now the workable value wins so the server
        starts, since sessions cannot be shared across processes anyway."""
        result, _, run_workers = self._invoke(
            ["--transport", "http", "--workers", "2", "--stateless-http", "false"]
        )

        assert result.exit_code == 0, result.output
        assert run_workers.call_args.args[0]["stateless_http"] is True

    def test_stateless_http_on_sse_is_overridden_not_rejected(self):
        result, fake_mcp, _ = self._invoke(
            ["--transport", "sse", "--stateless-http", "true"]
        )

        assert result.exit_code == 0, result.output
        assert fake_mcp.run.call_args.kwargs["stateless_http"] is False

    def test_thread_pool_size_applied_during_startup(self):
        """``--thread-pool-size`` must reach the live limiter, and the settings
        must then report the limit that took effect rather than the input."""
        lifespan_fn, fake_mcp = _capture_lifespan(
            ["--thread-pool-size", "128"], env=os.environ.copy()
        )

        async def drive() -> None:
            async with lifespan_fn(fake_mcp) as app_context:
                limiter = anyio.to_thread.current_default_thread_limiter()
                assert limiter.total_tokens == 128
                assert app_context.settings["thread_pool_size"] == 128

        anyio.run(drive)

    def test_thread_pool_size_unset_reports_runtime_default(self):
        """Unset must leave AnyIO's default alone, and still report a concrete
        number so a support bundle never shows ``None`` for the real ceiling."""
        lifespan_fn, fake_mcp = _capture_lifespan([], env=os.environ.copy())

        async def drive() -> None:
            async with lifespan_fn(fake_mcp) as app_context:
                default = anyio.to_thread.current_default_thread_limiter().total_tokens
                assert app_context.settings["thread_pool_size"] == default

        anyio.run(drive)

    def test_workers_env_var_honored(self):
        """``CB_MCP_WORKERS`` must reach the same resolution as the flag."""
        fake_mcp = MagicMock()
        runner = CliRunner()
        env = {**os.environ, "CB_MCP_WORKERS": "2", "CB_MCP_TRANSPORT": "http"}
        with (
            patch("mcp_server.FastMCP", return_value=fake_mcp),
            patch("mcp_server.run_workers") as run_workers,
        ):
            result = runner.invoke(mcp_server.main, [], env=env, catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert run_workers.call_args.args[0]["workers"] == 2


def _resolve_oauth_kwargs(**overrides):
    """Minimal OAuth-enabled kwargs for ``resolve_oauth`` (http + all JWT
    fields present), so only the scope-label behavior under test varies."""
    base = {
        "transport": "http",
        "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        "issuer": "https://idp.example.com",
        "audience": "mcp-couchbase",
        "algorithm": "RS256",
        "base_url": None,
    }
    base.update(overrides)
    return base


class TestResolveOauthScopeLabelCollision:
    """``resolve_oauth`` must reject read/write scope labels that collapse to
    the same string — otherwise the alias map loses one canonical scope and a
    whole tool class becomes unreachable."""

    def test_identical_custom_labels_rejected(self):
        with pytest.raises(OAuthConfigError, match="must be distinct"):
            resolve_oauth(
                **_resolve_oauth_kwargs(
                    scope_read="couchbase-mcp:access",
                    scope_write="couchbase-mcp:access",
                )
            )

    def test_read_override_equal_to_write_canonical_rejected(self):
        """A single override that equals the *other* scope's canonical default
        also collides (read label == canonical write, write left default)."""
        with pytest.raises(OAuthConfigError, match="must be distinct"):
            resolve_oauth(**_resolve_oauth_kwargs(scope_read=SCOPE_WRITE))

    def test_distinct_labels_reach_build_oauth(self):
        sentinel = object()
        with patch("cb_mcp.auth.build_oauth", return_value=sentinel) as m:
            result = resolve_oauth(
                **_resolve_oauth_kwargs(scope_read="idp-read", scope_write="idp-write")
            )
        assert result is sentinel
        m.assert_called_once()

    def test_defaults_do_not_collide(self):
        """Both labels unset (None) → canonical defaults, which differ; must
        not trip the guard."""
        sentinel = object()
        with patch("cb_mcp.auth.build_oauth", return_value=sentinel):
            result = resolve_oauth(**_resolve_oauth_kwargs())
        assert result is sentinel
        # Sanity: the defaults the guard compares against are genuinely distinct.
        assert SCOPE_READ != SCOPE_WRITE

    def test_cli_translates_oauth_config_error_to_usage_error(self):
        """The CLI layer converts ``OAuthConfigError`` into a click usage error
        (exit code 2), not an uncaught traceback."""
        result = CliRunner().invoke(
            mcp_server.main,
            [
                "--transport",
                "http",
                "--connection-string",
                "couchbase://example",
                "--username",
                "u",
                "--password",
                "p",
                "--oauth-jwks-uri",
                "https://idp.example.com/.well-known/jwks.json",
                "--oauth-issuer",
                "https://idp.example.com",
                "--oauth-audience",
                "mcp-couchbase",
                "--oauth-scope-read-label",
                "couchbase-mcp:access",
                "--oauth-scope-write-label",
                "couchbase-mcp:access",
            ],
        )
        assert result.exit_code == 2
        assert "must be distinct" in result.output
