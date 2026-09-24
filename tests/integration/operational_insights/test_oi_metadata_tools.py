"""Integration tests for Operational Insights metadata tools, against a live
cluster (see conftest.py in this directory for connection details).

Ported from the ``analytics-mcp`` prototype (branch
``DA-2027/Add-enterprise-tools``). Each test creates its own uniquely-named
scope + collection via ``run_query_sync`` DDL, seeds a document, exercises
the tool under test, then drops the scope in a ``finally`` block so reruns
don't collide.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import extract_payload

from .conftest import create_oi_mcp_session

DATABASE = "Default"


def _collect_keys(obj: object) -> set[str]:
    """Recursively collect every dict key appearing anywhere in obj.

    array_infer_schema's exact envelope (top-level key names, nesting) isn't
    pinned down by the docs, but it's documented to return "JSON Schema
    format" — meaning inferred property names always end up as dict keys
    somewhere in the structure. Walking for keys lets tests check that a
    field was detected without assuming an unconfirmed envelope shape.
    """
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys |= _collect_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _collect_keys(item)
    return keys


@pytest.mark.asyncio
async def test_get_databases_in_cluster() -> None:
    async with create_oi_mcp_session() as session:
        response = await session.call_tool("get_databases_in_cluster", arguments={})
        payload = extract_payload(response)

        assert isinstance(payload, list)
        assert any(row.get("DatabaseName") == DATABASE for row in payload)


@pytest.mark.asyncio
async def test_get_scopes_in_database() -> None:
    async with create_oi_mcp_session() as session:
        response = await session.call_tool(
            "get_scopes_in_database", arguments={"database_name": DATABASE}
        )
        payload = extract_payload(response)

        assert isinstance(payload, list)
        assert any(row.get("ScopeName") == DATABASE for row in payload)


@pytest.mark.asyncio
async def test_get_collections_in_scope_and_schema() -> None:
    scope_name = f"oitest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"oitest_coll_{uuid.uuid4().hex[:8]}"

    async with create_oi_mcp_session() as session:
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
            assert isinstance(schema, list) and len(schema) > 0
            assert {"id", "name", "count"} <= _collect_keys(schema)
        finally:
            await session.call_tool(
                "run_query_sync",
                arguments={
                    "statement": f"DROP SCOPE `{DATABASE}`.`{scope_name}` IF EXISTS;"
                },
            )
