"""
Integration tests verifying the server's read-only-mode behavior end-to-end.
"""

from __future__ import annotations

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    is_error_response,
    require_test_bucket,
)

# Write tool names that must NOT be exposed when read-only mode is on.
WRITE_TOOL_NAMES = frozenset(
    {
        "upsert_document_by_id",
        "insert_document_by_id",
        "replace_document_by_id",
        "delete_document_by_id",
    }
)

# Env var override that flips the server into read-only mode for one session.
READ_ONLY_ENV = {"CB_MCP_READ_ONLY_MODE": "true"}


@pytest.mark.asyncio
async def test_read_only_mode_filters_write_tools_from_listing() -> None:
    """In read-only mode, the KV write tools must not appear in list_tools.

    This is the strongest layer of the safety guarantee — the write tools
    aren't even registered with FastMCP, so there's nothing for an LLM to
    discover and call. Unit-tested via ``prepare_tools_for_registration``
    but only locked in at the wire level here.
    """
    async with create_mcp_session(extra_env=READ_ONLY_ENV) as session:
        response = await session.list_tools()
        names = {tool.name for tool in response.tools}

        leaked = WRITE_TOOL_NAMES & names
        assert not leaked, (
            f"Write tools leaked into read-only mode: {sorted(leaked)}"
        )

        # Sanity: read tools must still be present.
        assert "get_buckets_in_cluster" in names
        assert "get_document_by_id" in names


@pytest.mark.asyncio
async def test_read_only_mode_blocks_dml_query_at_runtime() -> None:
    """In read-only mode, run_sql_plus_plus_query must reject DML."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    async with create_mcp_session(extra_env=READ_ONLY_ENV) as session:
        response = await session.call_tool(
            "run_sql_plus_plus_query",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                # Predicate matches no rows so the query would be a no-op
                # even if read-only protection failed — defense in depth.
                "query": f"DELETE FROM `{collection}` WHERE META().id = '__nonexistent_for_test__'",
            },
        )

        assert is_error_response(response), (
            "DML query must fail in read-only mode. "
            f"Got non-error response: {extract_payload(response)}"
        )


@pytest.mark.asyncio
async def test_read_only_mode_allows_explain_of_dml_query() -> None:
    """EXPLAIN of a DML query must pass through even in read-only mode."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    async with create_mcp_session(extra_env=READ_ONLY_ENV) as session:
        response = await session.call_tool(
            "run_sql_plus_plus_query",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": (
                    f"EXPLAIN UPDATE `{collection}` SET x = x "
                    f"WHERE META().id = '__nonexistent_for_test__'"
                ),
            },
        )

        assert not is_error_response(response), (
            "EXPLAIN should bypass the read-only check. "
            f"Error payload: {extract_payload(response)}"
        )


@pytest.mark.asyncio
async def test_read_only_mode_reflected_in_server_configuration_status() -> None:
    """The status tool must report read_only_mode=true when configured."""
    async with create_mcp_session(extra_env=READ_ONLY_ENV) as session:
        response = await session.call_tool(
            "get_server_configuration_status", arguments={}
        )
        payload = extract_payload(response)

        config = payload.get("configuration", {})
        assert config.get("read_only_mode") is True, (
            f"read_only_mode should be reported as True, got: {config}"
        )
