"""
Integration tests for query.py tools.

Tests for:
- get_schema_for_collection
- run_sql_plus_plus_query
- explain_sql_plus_plus_query
"""

from __future__ import annotations

import pytest
from conftest import (
    create_mcp_session,
    ensure_list,
    extract_payload,
    get_test_collection,
    get_test_scope,
    is_error_response,
    require_test_bucket,
)


@pytest.mark.asyncio
async def test_get_schema_for_collection() -> None:
    """Verify get_schema_for_collection returns schema information."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_schema_for_collection",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
            },
        )
        payload = extract_payload(response)

        # Handle error case (e.g., empty collection can't infer schema)
        if isinstance(payload, str):
            if "No documents found" in payload or "unable to infer schema" in payload:
                skip_reason = (
                    f"Collection '{collection}' has no documents to infer schema"
                )
            else:
                raise AssertionError(f"Tool returned error: {payload}")
        else:
            assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
            assert "collection_name" in payload
            assert payload["collection_name"] == collection
            assert "schema" in payload
            # Schema is a list - skip if empty
            assert isinstance(payload["schema"], list)
            if len(payload["schema"]) == 0:
                skip_reason = f"Collection '{collection}' returned empty schema"

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_run_sql_plus_plus_query_select() -> None:
    """Verify run_sql_plus_plus_query can execute a SELECT query."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Simple query to count documents (works even on empty collection)
    query = f"SELECT COUNT(*) as doc_count FROM `{collection}`"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_sql_plus_plus_query",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"
        # Query should return at least one row
        assert len(payload) >= 1
        # First row should have doc_count field
        assert "doc_count" in payload[0]


@pytest.mark.asyncio
async def test_run_sql_plus_plus_query_with_limit() -> None:
    """Verify run_sql_plus_plus_query respects LIMIT clause."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    skip_reason = None

    query = f"SELECT * FROM `{collection}` LIMIT 5"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_sql_plus_plus_query",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        # Skip if collection is empty
        if len(payload) == 0:
            skip_reason = f"Collection '{collection}' has no documents"
        else:
            # Should return at most 5 documents
            assert len(payload) <= 5

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_run_sql_plus_plus_query_meta() -> None:
    """Verify run_sql_plus_plus_query can retrieve document metadata."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    skip_reason = None

    # Query to get document IDs using META()
    query = f"SELECT META().id as doc_id FROM `{collection}` LIMIT 1"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_sql_plus_plus_query",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        # Skip if collection is empty
        if len(payload) == 0:
            skip_reason = f"Collection '{collection}' has no documents"
        else:
            assert "doc_id" in payload[0]

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_explain_sql_plus_plus_query_with_query() -> None:
    """Verify explain_sql_plus_plus_query returns plan and evaluation for a query."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    query = f"SELECT COUNT(*) as doc_count FROM `{collection}`"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "explain_sql_plus_plus_query",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("query") == query
        assert "plan" in payload
        assert "plan_evaluation" in payload

        plan_evaluation = payload["plan_evaluation"]
        assert isinstance(plan_evaluation, dict)
        assert "summary" in plan_evaluation
        assert "operators" in plan_evaluation
        assert "findings" in plan_evaluation

        query_context = payload.get("query_context", {})
        assert query_context.get("bucket_name") == bucket
        assert query_context.get("scope_name") == scope


@pytest.mark.asyncio
async def test_explain_sql_plus_plus_query_rejects_empty_query() -> None:
    """An empty or whitespace-only query must surface a clear error response.

    The input validation in explain_sql_plus_plus_query raises ValueError
    before any work is done. This test verifies the error reaches the MCP
    client (not just the server-side logs)
    """
    bucket = require_test_bucket()
    scope = get_test_scope()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "explain_sql_plus_plus_query",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": "   \n\t  ",
            },
        )

        assert is_error_response(response), (
            "Empty query must produce an error response, "
            f"got payload: {extract_payload(response)}"
        )
