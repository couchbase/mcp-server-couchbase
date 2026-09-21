"""Tool-census checks for the Operational Insights server.

Small, deliberately not a clone of ``tests/integration/test_mcp_integration.py``'s
~13 operational census tests — most of those (descriptions, annotations) are
better expressed once, spec-driven, in ``tests/unit/test_server_specs.py``,
which needs no live cluster. These two are the ones that genuinely need a
live session: confirming the server actually registers what the spec says it
should, and nothing extra.
"""

from __future__ import annotations

import pytest
from conftest import ensure_list

from .conftest import create_oi_mcp_session

OI_EXPECTED_TOOLS = {
    "get_databases_in_cluster",
    "get_scopes_in_database",
    "get_collections_in_scope",
    "get_schema_for_collection",
    "list_indexes",
    "explain_query",
    "run_query_sync",
    "run_query_async",
    "get_async_query_results",
    "discard_async_query_results",
    "create_index",
    "cancel_async_query",
}


@pytest.mark.asyncio
async def test_all_expected_tools_registered() -> None:
    async with create_oi_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in ensure_list(tools_response.tools)}

    missing = OI_EXPECTED_TOOLS - tool_names
    assert not missing, f"Expected OI tools missing from registration: {missing}"


@pytest.mark.asyncio
async def test_no_unexpected_tools() -> None:
    async with create_oi_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in ensure_list(tools_response.tools)}

    unexpected = tool_names - OI_EXPECTED_TOOLS
    assert not unexpected, (
        f"New OI tools found (add to OI_EXPECTED_TOOLS): {sorted(unexpected)}"
    )
