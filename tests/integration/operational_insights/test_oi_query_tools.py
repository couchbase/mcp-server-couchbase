"""Integration tests for Operational Insights query tools.

Ported from the ``analytics-mcp`` prototype (branch
``DA-2027/Add-enterprise-tools``), against a live Operational Insights
cluster. Skipped automatically (see ``conftest.py`` in this directory)
unless credentials are configured.
"""

from __future__ import annotations

import pytest
from conftest import extract_payload

from .conftest import create_oi_mcp_session


@pytest.mark.asyncio
async def test_run_query_sync_select_one():
    async with create_oi_mcp_session() as session:
        response = await session.call_tool(
            "run_query_sync", {"statement": "SELECT 1 AS one"}
        )
        payload = extract_payload(response)

    assert payload["success"] is True
    assert payload["rows"] == [{"one": 1}]
    assert payload["row_count"] == 1


@pytest.mark.asyncio
async def test_run_query_sync_invalid_statement_returns_error_envelope():
    async with create_oi_mcp_session() as session:
        response = await session.call_tool(
            "run_query_sync", {"statement": "SELECT bad("}
        )
        payload = extract_payload(response)

    assert payload["success"] is False
    assert "error" in payload


@pytest.mark.asyncio
async def test_explain_query_returns_a_plan():
    async with create_oi_mcp_session() as session:
        response = await session.call_tool(
            "explain_query", {"statement": "SELECT 1 AS one"}
        )
        payload = extract_payload(response)

    assert payload["success"] is True
    assert payload["plan"]


@pytest.mark.asyncio
async def test_explain_query_does_not_execute_the_statement():
    """EXPLAIN plans a statement without running it — dividing by zero here
    would only fail if the statement were actually executed."""
    async with create_oi_mcp_session() as session:
        response = await session.call_tool(
            "explain_query", {"statement": "SELECT 1 / 0 AS boom"}
        )
        payload = extract_payload(response)

    assert payload["success"] is True


@pytest.mark.asyncio
async def test_explain_query_invalid_statement_returns_error_envelope():
    async with create_oi_mcp_session() as session:
        response = await session.call_tool(
            "explain_query", {"statement": "SELECT bad("}
        )
        payload = extract_payload(response)

    assert payload["success"] is False
    assert "error" in payload
