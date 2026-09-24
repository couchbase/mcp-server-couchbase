"""
Integration tests for performance analysis tools in query.py.

Tests for:
- get_longest_running_queries
- get_most_frequent_queries
- get_queries_with_largest_response_sizes
- get_queries_with_large_result_count
- get_queries_using_primary_index
- get_queries_not_using_covering_index
- get_queries_not_selective

These tools query the system:completed_requests catalog to analyze query performance.
Note: Results depend on cluster activity - tests skip gracefully when no data is available.
"""

from __future__ import annotations

import pytest
from conftest import (
    create_mcp_session,
    ensure_list,
    extract_payload,
)


def _check_empty_message(payload: list, message_contains: str) -> str | None:
    """Check if payload contains an empty message response.

    Returns skip reason if empty message found, None otherwise.
    """
    if not payload:
        return "No data returned from tool"

    first_item = payload[0]
    if (
        isinstance(first_item, dict)
        and "message" in first_item
        and message_contains in first_item.get("message", "")
    ):
        return first_item.get("message", "No data available")
    return None


@pytest.mark.asyncio
async def test_get_longest_running_queries() -> None:
    """Verify get_longest_running_queries returns query performance data."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool("get_longest_running_queries", arguments={})
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No completed queries")
        if skip_reason:
            pass  # Will skip after context manager exits
        else:
            # If we have actual results, verify structure
            first_item = payload[0]
            if isinstance(first_item, dict) and "statement" in first_item:
                assert "statement" in first_item
                assert "avgServiceTime" in first_item or "queries" in first_item

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_longest_running_queries_with_limit() -> None:
    """Verify get_longest_running_queries respects the limit parameter."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_longest_running_queries", arguments={"limit": 5}
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No completed queries")
        if not skip_reason:
            # Should return at most 5 results
            assert len(payload) <= 5

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_most_frequent_queries() -> None:
    """Verify get_most_frequent_queries returns query frequency data."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool("get_most_frequent_queries", arguments={})
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No completed queries")
        if not skip_reason:
            # If we have actual results, verify structure
            first_item = payload[0]
            if isinstance(first_item, dict) and "statement" in first_item:
                assert "statement" in first_item
                assert "queries" in first_item

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_most_frequent_queries_with_limit() -> None:
    """Verify get_most_frequent_queries respects the limit parameter."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_most_frequent_queries", arguments={"limit": 3}
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No completed queries")
        if not skip_reason:
            assert len(payload) <= 3

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_queries_with_largest_response_sizes() -> None:
    """Verify get_queries_with_largest_response_sizes returns response size data."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_queries_with_largest_response_sizes", arguments={}
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No completed queries")
        if not skip_reason:
            # If we have actual results, verify structure
            first_item = payload[0]
            if isinstance(first_item, dict) and "statement" in first_item:
                assert "statement" in first_item
                # Should have size metrics
                assert any(
                    key in first_item
                    for key in [
                        "avgResultSizeBytes",
                        "avgResultSizeKB",
                        "avgResultSizeMB",
                    ]
                )

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_queries_with_large_result_count() -> None:
    """Verify get_queries_with_large_result_count returns result count data."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_queries_with_large_result_count", arguments={}
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No completed queries")
        if not skip_reason:
            # If we have actual results, verify structure
            first_item = payload[0]
            if isinstance(first_item, dict) and "statement" in first_item:
                assert "statement" in first_item
                assert "avgResultCount" in first_item

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_queries_using_primary_index() -> None:
    """Verify get_queries_using_primary_index returns queries using primary indexes."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_queries_using_primary_index", arguments={}
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(
            payload, "No queries using the primary index"
        )
        if not skip_reason:
            # If we have actual results, verify it has query data
            first_item = payload[0]
            if isinstance(first_item, dict) and "statement" in first_item:
                assert "statement" in first_item
                # Should have phaseCounts with primaryScan
                assert "phaseCounts" in first_item

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_queries_not_using_covering_index() -> None:
    """Verify get_queries_not_using_covering_index returns non-covering queries."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_queries_not_using_covering_index", arguments={}
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No queries that require fetches")
        if not skip_reason:
            # If we have actual results, verify it has query data
            first_item = payload[0]
            if isinstance(first_item, dict) and "statement" in first_item:
                assert "statement" in first_item
                # Should have phaseCounts with fetch
                assert "phaseCounts" in first_item

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_queries_not_selective() -> None:
    """Verify get_queries_not_selective returns non-selective queries."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool("get_queries_not_selective", arguments={})
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No non-selective queries")
        if not skip_reason:
            # If we have actual results, verify structure
            first_item = payload[0]
            if isinstance(first_item, dict) and "statement" in first_item:
                assert "statement" in first_item
                assert (
                    "diff" in first_item
                )  # difference between indexScan and resultCount

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_queries_not_selective_with_limit() -> None:
    """Verify get_queries_not_selective respects the limit parameter."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_queries_not_selective", arguments={"limit": 2}
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), f"Expected list, got {type(payload)}"

        skip_reason = _check_empty_message(payload, "No non-selective queries")
        if not skip_reason:
            assert len(payload) <= 2

    if skip_reason:
        pytest.skip(skip_reason)
