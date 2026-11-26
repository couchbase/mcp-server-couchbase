"""
End-to-end tests for named tool sets.

These spawn the real server and read what it advertises, so they prove the
wiring from the environment variable to the tool list. No cluster is needed:
the server starts in lazy mode and listing tools never connects.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import create_logging_test_session

from cb_mcp.tools.operational import TOOL_SETS

REPO_ROOT = Path(__file__).resolve().parents[3]


async def _listed(env: dict[str, str]) -> set[str]:
    async with create_logging_test_session(env_overrides=env) as session:
        return {tool.name for tool in (await session.list_tools()).tools}


@pytest.mark.asyncio
async def test_a_named_set_loads_only_that_sets_tools() -> None:
    listed = await _listed({"CB_MCP_TOOL_SETS": "query"})
    assert listed, "the query set should advertise at least one tool"
    assert listed <= set(TOOL_SETS["query"]), (
        f"tools outside the query set were advertised: "
        f"{sorted(listed - set(TOOL_SETS['query']))}"
    )


@pytest.mark.asyncio
async def test_several_sets_load_their_union() -> None:
    listed = await _listed({"CB_MCP_TOOL_SETS": "query,reference"})
    wanted = set(TOOL_SETS["query"]) | set(TOOL_SETS["reference"])
    assert listed <= wanted


@pytest.mark.asyncio
async def test_no_set_named_loads_more_than_any_one_set() -> None:
    everything = await _listed({})
    one_set = await _listed({"CB_MCP_TOOL_SETS": "query"})
    assert one_set < everything, "unset must keep today's full tool list"


@pytest.mark.asyncio
async def test_disabled_tools_still_subtracts_from_a_set() -> None:
    victim = sorted(TOOL_SETS["query"])[0]
    listed = await _listed(
        {"CB_MCP_TOOL_SETS": "query", "CB_MCP_DISABLED_TOOLS": victim}
    )
    assert victim not in listed


def test_an_unknown_set_stops_the_server_and_names_the_real_ones() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["CB_MCP_TRANSPORT"] = "stdio"
    env["CB_MCP_TOOL_SETS"] = "no_such_set"
    result = subprocess.run(
        [sys.executable, "-m", "mcp_server"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0
    assert "no_such_set" in result.stderr
    assert "kv" in result.stderr  # a real set name is offered
