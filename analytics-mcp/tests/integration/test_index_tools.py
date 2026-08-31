"""Integration tests for index.py tools, against a live Enterprise Analytics
cluster (see tests/integration/conftest.py for connection details).

Follows the same pattern as test_metadata_tools.py: each test creates its own
uniquely-named scope + collection via run_query_sync DDL, exercises
create_index, then drops the scope in a finally block so reruns don't collide
(dropping the scope also drops any indexes on its collections).
"""

from __future__ import annotations

import uuid

import pytest
from conftest import create_mcp_session, extract_payload

DATABASE = "Default"


async def _create_collection(session, scope_name: str, collection_name: str) -> None:
    """Create a scope + collection to hang test indexes off."""
    await session.call_tool(
        "run_query_sync",
        arguments={
            "statement": f"CREATE SCOPE `{DATABASE}`.`{scope_name}` IF NOT EXISTS;"
        },
    )
    create_coll = await session.call_tool(
        "run_query_sync",
        arguments={
            "statement": (
                f"CREATE COLLECTION `{DATABASE}`.`{scope_name}`.`{collection_name}` "
                "IF NOT EXISTS PRIMARY KEY (id: string);"
            )
        },
    )
    assert extract_payload(create_coll)["success"] is True


async def _drop_scope(session, scope_name: str) -> None:
    await session.call_tool(
        "run_query_sync",
        arguments={"statement": f"DROP SCOPE `{DATABASE}`.`{scope_name}` IF EXISTS;"},
    )


def _index_exists(rows, index_name: str) -> bool:
    return any(row.get("IndexName") == index_name for row in rows)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_index_single_field() -> None:
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"
    index_name = f"eatest_idx_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _create_collection(session, scope_name, collection_name)

            response = await session.call_tool(
                "create_index",
                arguments={
                    "database_name": DATABASE,
                    "scope_name": scope_name,
                    "collection_name": collection_name,
                    "index_name": index_name,
                    "fields": [{"name": "name", "type": "string"}],
                },
            )
            payload = extract_payload(response)
            assert payload["success"] is True, payload
            assert payload["index_name"] == index_name

            # Confirm the index actually landed in the metadata catalog.
            verify = await session.call_tool(
                "run_query_sync",
                arguments={
                    "statement": (
                        "SELECT i.IndexName FROM System.Metadata.`Index` i "
                        f'WHERE i.DatabaseName = "{DATABASE}" '
                        f'AND i.DataverseName = "{scope_name}" '
                        f'AND i.DatasetName = "{collection_name}";'
                    )
                },
            )
            assert _index_exists(extract_payload(verify)["rows"], index_name)
        finally:
            await _drop_scope(session, scope_name)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_index_composite_and_if_not_exists() -> None:
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"
    index_name = f"eatest_idx_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _create_collection(session, scope_name, collection_name)

            arguments = {
                "database_name": DATABASE,
                "scope_name": scope_name,
                "collection_name": collection_name,
                "index_name": index_name,
                "fields": [
                    {"name": "name", "type": "string"},
                    {"name": "count", "type": "bigint"},
                ],
            }

            first = await session.call_tool("create_index", arguments=arguments)
            assert extract_payload(first)["success"] is True

            # Re-creating the same index errors without if_not_exists...
            second = await session.call_tool("create_index", arguments=arguments)
            assert extract_payload(second)["success"] is False

            # ...and is a no-op with it.
            third = await session.call_tool(
                "create_index", arguments={**arguments, "if_not_exists": True}
            )
            assert extract_payload(third)["success"] is True
        finally:
            await _drop_scope(session, scope_name)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_index_on_missing_collection_returns_error_envelope() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "create_index",
            arguments={
                "database_name": DATABASE,
                "scope_name": DATABASE,
                "collection_name": f"eatest_missing_{uuid.uuid4().hex[:8]}",
                "index_name": "eatest_idx_missing",
                "fields": [{"name": "name", "type": "string"}],
            },
        )
        payload = extract_payload(response)

        assert payload["success"] is False
        assert "error" in payload
