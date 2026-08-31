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
async def test_create_array_indexes() -> None:
    """Array (UNNEST) indexes, both the primitives and the SELECT form."""
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _create_collection(session, scope_name, collection_name)
            base = {
                "database_name": DATABASE,
                "scope_name": scope_name,
                "collection_name": collection_name,
                "exclude_unknown_key": True,
            }

            primitives = await session.call_tool(
                "create_index",
                arguments={
                    **base,
                    "index_name": "eatest_arr_prim",
                    "fields": [{"unnest": "likes", "type": "string"}],
                },
            )
            assert extract_payload(primitives)["success"] is True

            objects = await session.call_tool(
                "create_index",
                arguments={
                    **base,
                    "index_name": "eatest_arr_obj",
                    "fields": [
                        {
                            "unnest": "reviews",
                            "select": [{"name": "ratings.Lyrics", "type": "bigint"}],
                        }
                    ],
                },
            )
            assert extract_payload(objects)["success"] is True

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
            rows = extract_payload(verify)["rows"]
            assert _index_exists(rows, "eatest_arr_prim")
            assert _index_exists(rows, "eatest_arr_obj")
        finally:
            await _drop_scope(session, scope_name)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_array_index_requires_exclude_unknown_key() -> None:
    """EA rejects an array index without EXCLUDE UNKNOWN KEY; the tool forwards that."""
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _create_collection(session, scope_name, collection_name)

            response = await session.call_tool(
                "create_index",
                arguments={
                    "database_name": DATABASE,
                    "scope_name": scope_name,
                    "collection_name": collection_name,
                    "index_name": "eatest_arr_no_clause",
                    "fields": [{"unnest": "likes", "type": "string"}],
                },
            )
            payload = extract_payload(response)

            assert payload["success"] is False
            assert "EXCLUDE UNKNOWN KEY" in payload["error"]
        finally:
            await _drop_scope(session, scope_name)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_index_with_cast_default() -> None:
    """CAST (DEFAULT NULL ...), including a non-ISO-8601 date format."""
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _create_collection(session, scope_name, collection_name)
            base = {
                "database_name": DATABASE,
                "scope_name": scope_name,
                "collection_name": collection_name,
            }

            bare = await session.call_tool(
                "create_index",
                arguments={
                    **base,
                    "index_name": "eatest_cast_bare",
                    "fields": [{"name": "name", "type": "string"}],
                    "cast_default_null": True,
                },
            )
            payload = extract_payload(bare)
            assert payload["success"] is True
            assert payload["statement"].endswith("CAST (DEFAULT NULL);")

            formatted = await session.call_tool(
                "create_index",
                arguments={
                    **base,
                    "index_name": "eatest_cast_fmt",
                    "fields": [{"name": "hiredate", "type": "date"}],
                    "cast_formats": {"date": "MM/DD/YYYY"},
                },
            )
            payload = extract_payload(formatted)
            assert payload["success"] is True
            assert 'CAST (DEFAULT NULL DATE "MM/DD/YYYY");' in payload["statement"]
        finally:
            await _drop_scope(session, scope_name)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cast_is_rejected_on_an_array_index() -> None:
    """Undocumented: "CAST modifier is only allowed for B-Tree indexes"."""
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _create_collection(session, scope_name, collection_name)

            response = await session.call_tool(
                "create_index",
                arguments={
                    "database_name": DATABASE,
                    "scope_name": scope_name,
                    "collection_name": collection_name,
                    "index_name": "eatest_cast_arr",
                    "fields": [{"unnest": "likes", "type": "string"}],
                    "exclude_unknown_key": True,
                    "cast_default_null": True,
                },
            )
            payload = extract_payload(response)

            assert payload["success"] is False
            assert "B-Tree" in payload["error"]
        finally:
            await _drop_scope(session, scope_name)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_backticks_in_names_cannot_break_out() -> None:
    """A backtick in a collection name must be escaped, not close the identifier.

    The injected DROP must never run: the victim index has to survive, and the
    payload must come back as an unknown-collection error.
    """
    scope_name = f"eatest_scope_{uuid.uuid4().hex[:8]}"
    collection_name = f"eatest_coll_{uuid.uuid4().hex[:8]}"
    victim = f"eatest_victim_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _create_collection(session, scope_name, collection_name)
            base = {
                "database_name": DATABASE,
                "scope_name": scope_name,
                "collection_name": collection_name,
            }

            created = await session.call_tool(
                "create_index",
                arguments={
                    **base,
                    "index_name": victim,
                    "fields": [{"name": "name", "type": "string"}],
                },
            )
            assert extract_payload(created)["success"] is True

            keyspace = f"`{DATABASE}`.`{scope_name}`.`{collection_name}`"
            attack = await session.call_tool(
                "create_index",
                arguments={
                    **base,
                    "collection_name": (
                        f"{collection_name}`) ; DROP INDEX {keyspace}.`{victim}` --"
                    ),
                    "index_name": "eatest_injected",
                    "fields": [{"name": "name", "type": "string"}],
                },
            )
            assert extract_payload(attack)["success"] is False

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
            assert _index_exists(extract_payload(verify)["rows"], victim)
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
