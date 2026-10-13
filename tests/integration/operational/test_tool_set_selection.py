"""
End-to-end tests for the opt-in ``select_tool_sets`` tool.

The behaviour worth proving over a real stdio session is that the selection
sticks for the rest of the session, narrows what ``tools/list`` returns, can be
undone, and cannot be switched on in a deployment that keeps no session. No
cluster is needed: the server starts in lazy mode and none of these calls touch
it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import create_logging_test_session, extract_payload

from cb_mcp.tools.operational import TOOL_SETS

REPO_ROOT = Path(__file__).resolve().parents[3]
ENABLE = {"CB_MCP_ENABLE_TOOL_SET_SELECTION": "true"}


async def _names(session) -> set[str]:
    return {tool.name for tool in (await session.list_tools()).tools}


@pytest.mark.asyncio
async def test_the_tool_is_absent_unless_the_operator_opts_in() -> None:
    async with create_logging_test_session() as session:
        assert "select_tool_sets" not in await _names(session)


@pytest.mark.asyncio
async def test_a_selection_narrows_the_list_and_keeps_the_way_back() -> None:
    async with create_logging_test_session(env_overrides=ENABLE) as session:
        before = await _names(session)
        assert "select_tool_sets" in before

        response = await session.call_tool(
            "select_tool_sets", arguments={"tool_sets": ["query"]}
        )
        payload = extract_payload(response)
        assert payload["success"] is True
        assert payload["selected_tool_sets"] == ["query"]

        after = await _names(session)
        assert after <= set(TOOL_SETS["query"]) | {"select_tool_sets"}
        assert "select_tool_sets" in after, "the way back must never be narrowed away"
        assert after < before


@pytest.mark.asyncio
async def test_an_empty_selection_restores_the_full_list() -> None:
    async with create_logging_test_session(env_overrides=ENABLE) as session:
        before = await _names(session)
        await session.call_tool("select_tool_sets", arguments={"tool_sets": ["query"]})
        response = await session.call_tool(
            "select_tool_sets", arguments={"tool_sets": []}
        )
        assert extract_payload(response)["success"] is True
        assert await _names(session) == before


@pytest.mark.asyncio
async def test_an_unknown_set_is_refused_and_changes_nothing() -> None:
    async with create_logging_test_session(env_overrides=ENABLE) as session:
        before = await _names(session)
        response = await session.call_tool(
            "select_tool_sets", arguments={"tool_sets": ["no_such_set"]}
        )
        payload = extract_payload(response)
        assert payload["success"] is False
        assert "kv" in payload["available_tool_sets"]
        assert await _names(session) == before


@pytest.mark.asyncio
async def test_a_selection_cannot_add_what_the_operator_removed() -> None:
    victim = sorted(TOOL_SETS["query"])[0]
    env = {**ENABLE, "CB_MCP_DISABLED_TOOLS": victim}
    async with create_logging_test_session(env_overrides=env) as session:
        await session.call_tool("select_tool_sets", arguments={"tool_sets": ["query"]})
        assert victim not in await _names(session)


def test_selection_is_refused_with_stateless_http() -> None:
    """Stateless mode keeps no session, so a selection could never be recalled.
    Refusing at startup beats a tool that reports success and does nothing."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["CB_MCP_TRANSPORT"] = "http"
    env["CB_MCP_STATELESS_HTTP"] = "true"
    env.update(ENABLE)
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
    assert "enable-tool-set-selection" in result.stderr
