"""
End-to-end tests for ``--session-posture``.

The promise is that a declared posture which contradicts the configuration
stops the server at startup with a message, instead of starting a deployment
whose controls do not do what the declaration says. These spawn the real
server. No cluster is needed: every case is decided before any connection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import create_logging_test_session

REPO_ROOT = Path(__file__).resolve().parents[3]


def _start(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    for var in ("CB_MCP_TRANSPORT", "MCP_TRANSPORT", "CB_MCP_SESSION_POSTURE"):
        env.pop(var, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "mcp_server"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.asyncio
async def test_workstation_on_stdio_starts_normally() -> None:
    async with create_logging_test_session(
        env_overrides={"CB_MCP_SESSION_POSTURE": "workstation"}
    ) as session:
        assert (await session.list_tools()).tools


@pytest.mark.asyncio
async def test_an_undeclared_posture_changes_nothing() -> None:
    async with create_logging_test_session() as session:
        assert (await session.list_tools()).tools


def test_workstation_over_a_network_transport_is_refused() -> None:
    result = _start(
        {"CB_MCP_TRANSPORT": "http", "CB_MCP_SESSION_POSTURE": "workstation"}
    )
    assert result.returncode != 0
    assert "workstation" in result.stderr
    assert "stdio" in result.stderr


def test_enterprise_on_stdio_is_refused() -> None:
    result = _start(
        {"CB_MCP_TRANSPORT": "stdio", "CB_MCP_SESSION_POSTURE": "enterprise"}
    )
    assert result.returncode != 0
    assert "enterprise" in result.stderr
    assert "network transport" in result.stderr


def test_enterprise_without_oauth_is_refused() -> None:
    result = _start(
        {"CB_MCP_TRANSPORT": "http", "CB_MCP_SESSION_POSTURE": "enterprise"}
    )
    assert result.returncode != 0
    assert "OAuth is not active" in result.stderr


def test_an_unknown_posture_is_rejected_by_the_cli() -> None:
    result = _start({"CB_MCP_SESSION_POSTURE": "datacentre"})
    assert result.returncode != 0
    assert "workstation" in result.stderr and "enterprise" in result.stderr
