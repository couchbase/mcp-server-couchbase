"""Integration tests for the Server Async Request API tools, against a live
Enterprise Analytics cluster (see tests/integration/conftest.py for connection
details).

Requires EA 2.2+ / analytics SDK >= 1.1.0 for the async request API.

Each test opens its own MCP session. That matters here in a way it does not for
the sync tools: query_handle tokens live in the server subprocess's in-memory
registry, so a token is only valid inside the session that minted it.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import create_mcp_session, extract_payload

# A trivial query can finish before the first poll, so readiness is polled
# rather than assumed.
POLL_ATTEMPTS = 30
POLL_INTERVAL_SECONDS = 0.5


async def _poll_until_ready(session, query_handle: str) -> bool:
    """Poll get_async_query_status until results are ready or attempts run out."""
    for _ in range(POLL_ATTEMPTS):
        response = await session.call_tool(
            "get_async_query_status", arguments={"query_handle": query_handle}
        )
        payload = extract_payload(response)
        assert payload["success"] is True, payload
        if payload["ready"]:
            return True
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return False


async def _start_query(session, statement: str) -> str:
    """Start an async query and return its query_handle token."""
    response = await session.call_tool(
        "run_query_async", arguments={"statement": statement}
    )
    payload = extract_payload(response)
    assert payload["success"] is True, payload
    assert payload["query_handle"]
    return payload["query_handle"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_async_query_full_lifecycle() -> None:
    """Start -> poll to ready -> fetch rows and metadata."""
    async with create_mcp_session() as session:
        handle = await _start_query(session, "SELECT 1 AS one")

        assert await _poll_until_ready(session, handle), "query never became ready"

        response = await session.call_tool(
            "get_async_query_results", arguments={"query_handle": handle}
        )
        payload = extract_payload(response)

        assert payload["success"] is True
        assert payload["rows"] == [{"one": 1}]
        assert payload["row_count"] == 1
        assert payload["metadata"]["request_id"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_results_can_be_fetched_twice() -> None:
    """Fetching is a read, not a consume: EA keeps the buffers, so the same
    handle can be fetched again and returns identical rows."""
    async with create_mcp_session() as session:
        handle = await _start_query(session, "SELECT 1 AS one")
        assert await _poll_until_ready(session, handle)

        first = extract_payload(
            await session.call_tool(
                "get_async_query_results", arguments={"query_handle": handle}
            )
        )
        second = extract_payload(
            await session.call_tool(
                "get_async_query_results", arguments={"query_handle": handle}
            )
        )

        assert first["success"] is True
        assert second["success"] is True
        assert first["rows"] == second["rows"] == [{"one": 1}]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discard_after_fetch_ends_the_lifecycle() -> None:
    """Discard is the cleanup step even after a successful fetch."""
    async with create_mcp_session() as session:
        handle = await _start_query(session, "SELECT 1 AS one")
        assert await _poll_until_ready(session, handle)

        await session.call_tool(
            "get_async_query_results", arguments={"query_handle": handle}
        )
        discarded = extract_payload(
            await session.call_tool(
                "discard_async_query_results", arguments={"query_handle": handle}
            )
        )
        assert discarded["success"] is True
        assert discarded["discarded"] is True

        # Only now is the token gone.
        after = extract_payload(
            await session.call_tool(
                "get_async_query_results", arguments={"query_handle": handle}
            )
        )
        assert after["success"] is False
        assert handle in after["error"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discard_async_query_results() -> None:
    """Ready results can be released without fetching them."""
    async with create_mcp_session() as session:
        handle = await _start_query(session, "SELECT 1 AS one")
        assert await _poll_until_ready(session, handle)

        response = await session.call_tool(
            "discard_async_query_results", arguments={"query_handle": handle}
        )
        payload = extract_payload(response)

        assert payload["success"] is True
        assert payload["discarded"] is True

        # Discarding evicts the token.
        response = await session.call_tool(
            "get_async_query_results", arguments={"query_handle": handle}
        )
        assert extract_payload(response)["success"] is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancel_refused_for_completed_query() -> None:
    """A finished query cannot be cancelled, and saying so must not strand its
    result buffers: the token stays usable so the results can be discarded.

    EA answers a cancel for a completed query with a bare 404 and the SDK
    treats 404 as success, so without the tool's own status check this would
    report cancelled: true and evict the token.
    """
    async with create_mcp_session() as session:
        handle = await _start_query(session, "SELECT 1 AS one")
        assert await _poll_until_ready(session, handle), "query never became ready"

        payload = extract_payload(
            await session.call_tool(
                "cancel_async_query", arguments={"query_handle": handle}
            )
        )
        assert payload["success"] is True
        assert payload["cancelled"] is False
        assert "discard_async_query_results" in payload["message"]

        # The token survived, so the recommended cleanup actually works.
        discarded = extract_payload(
            await session.call_tool(
                "discard_async_query_results", arguments={"query_handle": handle}
            )
        )
        assert discarded["success"] is True
        assert discarded["discarded"] is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancel_running_query() -> None:
    """A query still running can be cancelled, which evicts its token."""
    async with create_mcp_session() as session:
        # Heavy enough to still be running on the immediately following cancel.
        handle = await _start_query(
            session,
            "SELECT COUNT(*) AS n FROM ARRAY_RANGE(0, 12000000) AS x "
            "WHERE x % 7 = 0 AND x % 11 = 3",
        )

        payload = extract_payload(
            await session.call_tool(
                "cancel_async_query", arguments={"query_handle": handle}
            )
        )
        assert payload["success"] is True

        if payload["cancelled"]:
            # Cancelled: the token is evicted, so a second cancel fails.
            second = extract_payload(
                await session.call_tool(
                    "cancel_async_query", arguments={"query_handle": handle}
                )
            )
            assert second["success"] is False
        else:
            # It finished before the cancel landed; clean up its buffers.
            await session.call_tool(
                "discard_async_query_results", arguments={"query_handle": handle}
            )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unknown_handle_returns_error_envelope() -> None:
    async with create_mcp_session() as session:
        for tool in (
            "get_async_query_status",
            "get_async_query_results",
            "discard_async_query_results",
            "cancel_async_query",
        ):
            response = await session.call_tool(
                tool, arguments={"query_handle": "not-a-real-handle"}
            )
            payload = extract_payload(response)

            assert payload["success"] is False, tool
            assert "not-a-real-handle" in payload["error"], tool


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invalid_statement_returns_error_envelope() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_query_async", arguments={"statement": "SELECT bad("}
        )
        payload = extract_payload(response)

        assert payload["success"] is False
        assert "error" in payload
