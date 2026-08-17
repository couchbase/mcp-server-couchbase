"""Integration tests for query.py tools, against a live Enterprise Analytics
cluster (see tests/integration/conftest.py for connection details).
"""

from __future__ import annotations

import pytest
from conftest import create_mcp_session, extract_payload


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_query_sync_select() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_query_sync", arguments={"statement": "SELECT 1 AS one"}
        )
        payload = extract_payload(response)

        assert payload == {"success": True, "rows": [{"one": 1}], "row_count": 1}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_query_sync_invalid_statement_returns_error_envelope() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_query_sync", arguments={"statement": "SELECT bad("}
        )
        payload = extract_payload(response)

        assert payload["success"] is False
        assert "error" in payload
