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

import pytest
from click.testing import CliRunner

import cb_mcp.utils.logging as logmod
import mcp_server


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
