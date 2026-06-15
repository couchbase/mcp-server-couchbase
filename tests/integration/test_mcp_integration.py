"""
General MCP server integration tests.

Tests for tool registration, basic connectivity, and MCP protocol compliance.
"""

from __future__ import annotations

import pytest
from conftest import (
    EXPECTED_TOOLS,
    TOOL_REQUIRED_PARAMS,
    TOOLS_BY_CATEGORY,
    create_mcp_session,
    extract_payload,
    is_error_response,
    require_test_bucket,
)


@pytest.mark.asyncio
async def test_tools_are_registered() -> None:
    """Ensure all expected tools are exposed by the server."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}
        missing = EXPECTED_TOOLS - tool_names
        assert not missing, f"Missing MCP tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_all_tools_have_descriptions() -> None:
    """Verify all tools have non-empty descriptions for discoverability."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()

        tools_without_description = []
        for tool in tools_response.tools:
            if tool.name in EXPECTED_TOOLS and (
                not tool.description or len(tool.description.strip()) == 0
            ):
                tools_without_description.append(tool.name)

        assert not tools_without_description, (
            f"Tools missing descriptions: {sorted(tools_without_description)}"
        )


@pytest.mark.asyncio
async def test_all_tools_have_input_schema() -> None:
    """Verify all tools have input schema defined."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()

        tools_without_schema = []
        for tool in tools_response.tools:
            if tool.name in EXPECTED_TOOLS and not tool.inputSchema:
                tools_without_schema.append(tool.name)

        assert not tools_without_schema, (
            f"Tools missing input schema: {sorted(tools_without_schema)}"
        )


@pytest.mark.asyncio
async def test_tools_have_required_parameters() -> None:
    """Verify tools that need parameters have them defined as required."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()

        # Build a map of tool name -> tool for easy lookup
        tools_map = {tool.name: tool for tool in tools_response.tools}

        errors = []
        for tool_name, expected_params in TOOL_REQUIRED_PARAMS.items():
            if tool_name not in tools_map:
                errors.append(f"{tool_name}: tool not found")
                continue

            tool = tools_map[tool_name]
            schema = tool.inputSchema or {}
            required = schema.get("required", [])
            properties = schema.get("properties", {})

            # Check all expected params exist in properties
            for param in expected_params:
                if param not in properties:
                    errors.append(f"{tool_name}: missing property '{param}'")
                elif param not in required:
                    errors.append(f"{tool_name}: '{param}' should be required")

        assert not errors, "Parameter validation errors:\n" + "\n".join(errors)


@pytest.mark.asyncio
async def test_server_tools_are_registered() -> None:
    """Verify all server category tools are registered."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}

        missing = TOOLS_BY_CATEGORY["server"] - tool_names
        assert not missing, f"Missing server tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_kv_tools_are_registered() -> None:
    """Verify all KV (document) tools are registered."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}

        missing = TOOLS_BY_CATEGORY["kv"] - tool_names
        assert not missing, f"Missing KV tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_query_tools_are_registered() -> None:
    """Verify all query tools are registered."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}

        missing = TOOLS_BY_CATEGORY["query"] - tool_names
        assert not missing, f"Missing query tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_index_tools_are_registered() -> None:
    """Verify all index tools are registered."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}

        missing = TOOLS_BY_CATEGORY["index"] - tool_names
        assert not missing, f"Missing index tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_performance_tools_are_registered() -> None:
    """Verify all performance analysis tools are registered."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}

        missing = TOOLS_BY_CATEGORY["performance"] - tool_names
        assert not missing, f"Missing performance tools: {sorted(missing)}"


@pytest.mark.asyncio
async def test_tool_descriptions_are_meaningful() -> None:
    """Verify tool descriptions contain meaningful content (not too short)."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()

        # Minimum description length to be considered meaningful
        min_length = 20

        short_descriptions = []
        for tool in tools_response.tools:
            if tool.name in EXPECTED_TOOLS:
                desc = tool.description or ""
                if len(desc.strip()) < min_length:
                    short_descriptions.append(
                        f"{tool.name}: '{desc[:50]}...' (len={len(desc)})"
                    )

        assert not short_descriptions, (
            "Tools with too-short descriptions:\n" + "\n".join(short_descriptions)
        )


@pytest.mark.asyncio
async def test_no_unexpected_tools() -> None:
    """Verify no unexpected tools are registered (catches accidental additions)."""
    async with create_mcp_session() as session:
        tools_response = await session.list_tools()
        tool_names = {tool.name for tool in tools_response.tools}

        unexpected = tool_names - EXPECTED_TOOLS
        # This is informational - new tools should be added to EXPECTED_TOOLS
        if unexpected:
            pytest.skip(
                f"New tools found (add to EXPECTED_TOOLS): {sorted(unexpected)}"
            )


@pytest.mark.asyncio
async def test_cluster_connection_tool_invocation() -> None:
    """Verify the cluster connectivity tool executes against the demo cluster."""
    async with create_mcp_session() as session:
        # First test cluster connection without bucket (should always work)
        response = await session.call_tool("test_cluster_connection", arguments={})
        payload = extract_payload(response)

        assert payload, "No data returned from test_cluster_connection"
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") == "success", (
            f"Cluster connection failed: {payload}"
        )
        assert payload.get("cluster_connected") is True


@pytest.mark.asyncio
async def test_cluster_connection_with_bucket() -> None:
    """Verify cluster connection tool works with a bucket (if configured)."""
    bucket = require_test_bucket()
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "test_cluster_connection", arguments={"bucket_name": bucket}
        )
        payload = extract_payload(response)

        assert payload, "No data returned from test_cluster_connection"
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"

        # If bucket doesn't exist, we get an error - mark for skip (can't skip inside async cm)
        if payload.get("status") == "error":
            error = payload.get("error", "")
            if "BucketNotFoundException" in error:
                skip_reason = f"Test bucket '{bucket}' not found on cluster"
            else:
                # Other error - fail the test
                raise AssertionError(f"Connection failed: {payload}")
        else:
            assert payload.get("status") == "success", f"Connection failed: {payload}"
            assert payload.get("bucket_name") == bucket
            assert payload.get("bucket_connected") is True

    # Skip outside the async context manager if needed
    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_can_list_buckets() -> None:
    """Call a data-returning tool to ensure the session is usable."""
    async with create_mcp_session() as session:
        response = await session.call_tool("get_buckets_in_cluster", arguments={})
        payload = extract_payload(response)

        assert payload is not None, "No payload returned from get_buckets_in_cluster"
        # If the demo cluster has buckets, we should see them; otherwise we at least
        # confirm the tool executed without errors.
        if isinstance(payload, list):
            assert payload, "Expected at least one bucket from the demo cluster"


@pytest.mark.asyncio
async def test_server_status_without_connection() -> None:
    """Verify get_server_configuration_status works without establishing connection."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_server_configuration_status", arguments={}
        )
        payload = extract_payload(response)

        assert payload is not None, "No payload returned"
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") == "running"
        assert payload.get("server_name") == "couchbase"



@pytest.mark.asyncio
async def test_disabled_tool_not_exposed_via_mcp() -> None:
    """A tool named in CB_MCP_DISABLED_TOOLS must not appear in list_tools.

    Sibling tools that are NOT disabled must still be present — the
    disable list filters per-tool, not globally.
    """
    async with create_mcp_session(
        extra_env={"CB_MCP_DISABLED_TOOLS": "get_buckets_in_cluster"}
    ) as session:
        response = await session.list_tools()
        names = {tool.name for tool in response.tools}

        assert "get_buckets_in_cluster" not in names, (
            "Disabled tool should not be advertised over the MCP wire"
        )
        # Sibling read-only tool must still be present.
        assert "get_server_configuration_status" in names


@pytest.mark.asyncio
async def test_calling_disabled_tool_returns_error() -> None:
    """An LLM that tries to call a disabled tool must get a clean error response."""
    async with create_mcp_session(
        extra_env={"CB_MCP_DISABLED_TOOLS": "get_buckets_in_cluster"}
    ) as session:
        response = await session.call_tool(
            "get_buckets_in_cluster", arguments={}
        )

        assert is_error_response(response), (
            "Calling a disabled tool must produce an error response"
        )
