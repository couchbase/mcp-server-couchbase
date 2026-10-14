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

from .conftest import create_operational_insights_mcp_session

OPERATIONAL_INSIGHTS_EXPECTED_TOOLS = {
    # Shared with every other server — the same function object, not an Operational Insights
    # reimplementation. See cb_mcp/tools/status.py.
    "get_server_configuration_status",
    "operational_insights_get_databases_in_cluster",
    "operational_insights_get_scopes_in_database",
    "operational_insights_get_collections_in_scope",
    "operational_insights_get_schema_for_collection",
    "operational_insights_list_indexes",
    "operational_insights_explain_query",
    "operational_insights_run_query_sync",
    "operational_insights_run_query_async",
    "operational_insights_get_async_query_results",
    "operational_insights_discard_async_query_results",
    "operational_insights_create_index",
    "operational_insights_cancel_async_query",
}


@pytest.mark.asyncio
async def test_all_expected_tools_registered() -> None:
    async with create_operational_insights_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in ensure_list(tools_response.tools)}

    missing = OPERATIONAL_INSIGHTS_EXPECTED_TOOLS - tool_names
    assert not missing, (
        f"Expected Operational Insights tools missing from registration: {missing}"
    )


@pytest.mark.asyncio
async def test_no_unexpected_tools() -> None:
    async with create_operational_insights_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in ensure_list(tools_response.tools)}

    unexpected = tool_names - OPERATIONAL_INSIGHTS_EXPECTED_TOOLS
    assert not unexpected, (
        f"New Operational Insights tools found (add to OPERATIONAL_INSIGHTS_EXPECTED_TOOLS): {sorted(unexpected)}"
    )
