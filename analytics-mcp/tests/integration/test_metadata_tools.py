"""Integration tests for metadata.py tools, against a live Enterprise Analytics
cluster (see tests/integration/conftest.py for connection details).

Each test creates its own uniquely-named scope + collection via
run_query_sync DDL, seeds a document, exercises the tool under test, then
drops the scope in a finally block so reruns don't collide.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import create_mcp_session, extract_payload

DATABASE = "Default"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_databases_in_cluster() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool("get_databases_in_cluster", arguments={})
        payload = extract_payload(response)

        assert isinstance(payload, list)
        assert any(row.get("DatabaseName") == DATABASE for row in payload)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_scopes_in_database() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_scopes_in_database", arguments={"database_name": DATABASE}
        )
        payload = extract_payload(response)

        assert isinstance(payload, list)
        assert any(row.get("ScopeName") == DATABASE for row in payload)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_collections_in_scope_and_schema() -> None:
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
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

            insert = await session.call_tool(
                "run_query_sync",
                arguments={
                    "statement": (
                        f"INSERT INTO `{DATABASE}`.`{scope_name}`.`{collection_name}` "
                        "([{'id': '1', 'name': 'a', 'count': 1}]);"
                    )
                },
            )
            assert extract_payload(insert)["success"] is True

            collections_response = await session.call_tool(
                "get_collections_in_scope",
                arguments={"database_name": DATABASE, "scope_name": scope_name},
            )
            collections = extract_payload(collections_response)
            assert any(
                row.get("CollectionName") == collection_name for row in collections
            )

            schema_response = await session.call_tool(
                "get_schema_for_collection",
                arguments={
                    "database_name": DATABASE,
                    "scope_name": scope_name,
                    "collection_name": collection_name,
                },
            )
            schema = extract_payload(schema_response)
            fields = {row["field"] for row in schema}
            assert {"id", "name", "count"} <= fields
        finally:
            await session.call_tool(
                "run_query_sync",
                arguments={
                    "statement": f"DROP SCOPE `{DATABASE}`.`{scope_name}` IF EXISTS;"
                },
            )
