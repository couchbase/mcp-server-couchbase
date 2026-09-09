"""
Integration tests for search.py (FTS/Search) tools.

Tests for:
- list_search_indexes (list mode and full-definition mode via index_name)
- run_fts_query (query mode and explain mode via explain=True)

There is no MCP write tool for Search index management (out of scope for this
tool family), so the fixtures below seed/drop Search indexes directly via the
Couchbase Python SDK, mirroring how ``test_index.py``'s local
``_create_index``/``_drop_index`` helpers work but SDK-direct instead of
going through ``call_tool_silent`` (there's no MCP tool to call).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Iterator

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)

from cb_mcp.utils.connection import connect_to_couchbase_cluster

try:
    from couchbase.management.search import SearchIndex
except ImportError:  # pragma: no cover - SDK always provides this in practice
    SearchIndex = None


def _direct_cluster():
    """Open a direct (non-MCP) SDK connection using the same env vars the
    MCP server subprocess uses, for seeding/cleanup only."""
    connection_string = os.environ["CB_CONNECTION_STRING"]
    username = os.environ["CB_USERNAME"]
    password = os.environ["CB_PASSWORD"]
    return connect_to_couchbase_cluster(connection_string, username, password)


@pytest.fixture(scope="module")
def seeded_search_index() -> Iterator[dict[str, str]]:
    """Create a scope-level Search index on the test bucket/scope/collection,
    seed one document into that collection so match_all queries have
    something to find, and drop the index (and doc) afterward."""
    if SearchIndex is None:
        pytest.skip("couchbase.management.search.SearchIndex is unavailable")

    bucket_name = require_test_bucket()
    scope_name = get_test_scope()
    collection_name = get_test_collection()
    index_name = f"test_fts_idx_{uuid.uuid4().hex[:8]}"
    doc_id = f"test_fts_doc_{uuid.uuid4().hex[:8]}"

    cluster = _direct_cluster()
    try:
        bucket = cluster.bucket(bucket_name)
        scope_index_manager = bucket.scope(scope_name).search_indexes()

        definition = SearchIndex(
            name=index_name,
            source_type="couchbase",
            idx_type="fulltext-index",
            source_name=bucket_name,
            params={
                "doc_config": {"mode": "scope.collection.type_field"},
                "mapping": {
                    "types": {
                        f"{scope_name}.{collection_name}": {
                            "enabled": True,
                            "dynamic": True,
                        }
                    },
                    "default_mapping": {"enabled": False},
                    "default_analyzer": "standard",
                },
            },
        )

        try:
            scope_index_manager.upsert_index(definition)
        except Exception as e:
            pytest.skip(f"Could not create Search index for tests: {e}")

        collection = bucket.scope(scope_name).collection(collection_name)
        collection.upsert(doc_id, {"name": "fts-seed-document", "kind": "test"})

        try:
            yield {
                "index_name": index_name,
                "bucket_name": bucket_name,
                "scope_name": scope_name,
                "collection_name": collection_name,
                "doc_id": doc_id,
            }
        finally:
            with contextlib.suppress(Exception):
                scope_index_manager.drop_index(index_name)
            with contextlib.suppress(Exception):
                collection.remove(doc_id)
    finally:
        with contextlib.suppress(Exception):
            cluster.close()


@pytest.fixture(scope="module")
def seeded_cluster_level_search_index() -> Iterator[dict[str, str]]:
    """Create a cluster-level (legacy) Search index for the duration of the
    module, and drop it afterward. Ensures test_list_search_indexes_no_filters
    always has a cluster-level index to find instead of skipping."""
    if SearchIndex is None:
        pytest.skip("couchbase.management.search.SearchIndex is unavailable")

    bucket_name = require_test_bucket()
    index_name = f"test_fts_cluster_idx_{uuid.uuid4().hex[:8]}"

    cluster = _direct_cluster()
    try:
        index_manager = cluster.search_indexes()

        definition = SearchIndex(
            name=index_name,
            source_type="couchbase",
            idx_type="fulltext-index",
            source_name=bucket_name,
        )

        try:
            index_manager.upsert_index(definition)
        except Exception as e:
            pytest.skip(f"Could not create cluster-level Search index for tests: {e}")

        try:
            yield {"index_name": index_name, "bucket_name": bucket_name}
        finally:
            with contextlib.suppress(Exception):
                index_manager.drop_index(index_name)
    finally:
        with contextlib.suppress(Exception):
            cluster.close()


@pytest.mark.asyncio
async def test_list_search_indexes_no_filters(
    seeded_cluster_level_search_index: dict[str, str],
) -> None:
    """No-filter call must return cluster-level (legacy) indexes only, and
    must surface the seeded cluster-level index."""
    async with create_mcp_session() as session:
        response = await session.call_tool("list_search_indexes", arguments={})
        payload = extract_payload(response)

    assert isinstance(payload, list)
    names = {entry["name"] for entry in payload}
    assert seeded_cluster_level_search_index["index_name"] in names
    for entry in payload:
        assert entry["bucket"] is None
        assert entry["scope"] is None


@pytest.mark.asyncio
async def test_list_search_indexes_by_bucket_and_scope(
    seeded_search_index: dict[str, str],
) -> None:
    """Filtering by bucket_name + scope_name must return the seeded index."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_search_indexes",
            arguments={
                "bucket_name": seeded_search_index["bucket_name"],
                "scope_name": seeded_search_index["scope_name"],
            },
        )
        payload = extract_payload(response)

    assert isinstance(payload, list)
    names = {entry["name"] for entry in payload}
    assert seeded_search_index["index_name"] in names
    for entry in payload:
        assert entry["bucket"] == seeded_search_index["bucket_name"]
        assert entry["scope"] == seeded_search_index["scope_name"]


@pytest.mark.asyncio
async def test_list_search_indexes_by_bucket_only(
    seeded_search_index: dict[str, str],
) -> None:
    """Filtering by bucket_name only must enumerate across all scopes and
    still surface the seeded index."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_search_indexes",
            arguments={"bucket_name": seeded_search_index["bucket_name"]},
        )
        payload = extract_payload(response)

    assert isinstance(payload, list)
    names = {entry["name"] for entry in payload}
    assert seeded_search_index["index_name"] in names


@pytest.mark.asyncio
async def test_list_search_indexes_scope_without_bucket_returns_error() -> None:
    """scope_name without bucket_name must be rejected with a descriptive
    error entry, not a raised MCP error."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_search_indexes", arguments={"scope_name": get_test_scope()}
        )
        payload = extract_payload(response)

    assert isinstance(payload, list) and len(payload) == 1
    assert "bucket_name is required" in payload[0]["error"]


@pytest.mark.asyncio
async def test_list_search_indexes_by_index_name_scope_level(
    seeded_search_index: dict[str, str],
) -> None:
    """Fetching the seeded scope-level index via index_name must return a
    single-entry list with native nested dicts for params (not JSON-encoded
    strings)."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_search_indexes",
            arguments={
                "index_name": seeded_search_index["index_name"],
                "bucket_name": seeded_search_index["bucket_name"],
                "scope_name": seeded_search_index["scope_name"],
            },
        )
        payload = extract_payload(response)

    assert isinstance(payload, list) and len(payload) == 1
    entry = payload[0]
    assert entry["name"] == seeded_search_index["index_name"]
    assert isinstance(entry["params"], dict)
    assert entry["bucket"] == seeded_search_index["bucket_name"]
    assert entry["scope"] == seeded_search_index["scope_name"]


@pytest.mark.asyncio
async def test_list_search_indexes_by_index_name_partial_pair_returns_error() -> None:
    """Passing index_name with only bucket_name (no scope_name) must return
    a descriptive error entry, not a raised MCP error."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_search_indexes",
            arguments={"index_name": "whatever", "bucket_name": "b"},
        )
        payload = extract_payload(response)

    assert isinstance(payload, list) and len(payload) == 1
    assert "must be provided together" in payload[0]["error"]


@pytest.mark.asyncio
async def test_list_search_indexes_by_index_name_not_found_returns_error(
    seeded_search_index: dict[str, str],
) -> None:
    """Looking up a nonexistent index name must return an error entry
    rather than raising or returning None."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_search_indexes",
            arguments={
                "index_name": f"does_not_exist_{uuid.uuid4().hex[:8]}",
                "bucket_name": seeded_search_index["bucket_name"],
                "scope_name": seeded_search_index["scope_name"],
            },
        )
        payload = extract_payload(response)

    assert isinstance(payload, list) and len(payload) == 1
    assert "error" in payload[0]


@pytest.mark.asyncio
async def test_run_fts_query_match_all(seeded_search_index: dict[str, str]) -> None:
    """A match_all query against the seeded index must eventually find the
    seeded document. FTS indexing is asynchronous, so immediately after
    upsert_index/document upsert the index may not have caught up yet — poll
    with a bounded timeout instead of accepting 0 hits as a pass (GSI's
    build_index has a synchronous build-trigger response that's sufficient to
    assert on its own; FTS has no equivalent synchronous completion signal,
    so unlike test_index_tools.py's deferred-index test, polling here is
    necessary rather than a convention we're deviating from casually)."""
    deadline = asyncio.get_event_loop().time() + 30
    payload = None
    async with create_mcp_session() as session:
        while asyncio.get_event_loop().time() < deadline:
            response = await session.call_tool(
                "run_fts_query",
                arguments={
                    "index_name": seeded_search_index["index_name"],
                    "bucket_name": seeded_search_index["bucket_name"],
                    "scope_name": seeded_search_index["scope_name"],
                    "query": {"match_all": {}},
                    "limit": 5,
                },
            )
            payload = extract_payload(response)
            if payload.get("total_hits", 0) > 0:
                break
            await asyncio.sleep(2)

    assert payload is not None
    assert payload["index_name"] == seeded_search_index["index_name"]
    assert payload["total_hits"] > 0, (
        "Expected at least one hit for the seeded document within 30s of "
        f"polling; last payload: {payload}"
    )
    assert isinstance(payload["hits"], list) and payload["hits"]
    hit = payload["hits"][0]
    assert hit["id"] == seeded_search_index["doc_id"]
    assert "score" in hit and "fields" in hit
    assert "metadata" in payload


@pytest.mark.asyncio
async def test_run_fts_query_explain_default_limit(
    seeded_search_index: dict[str, str],
) -> None:
    """run_fts_query with explain=True and no limit must default to 1 and
    return an explanation per (at most one) hit."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_fts_query",
            arguments={
                "index_name": seeded_search_index["index_name"],
                "bucket_name": seeded_search_index["bucket_name"],
                "scope_name": seeded_search_index["scope_name"],
                "query": {"match_all": {}},
                "explain": True,
            },
        )
        payload = extract_payload(response)

    assert payload["explain"] is True
    assert len(payload["hits"]) <= 1
    for hit in payload["hits"]:
        assert "explanation" in hit


# ---------------------------------------------------------------------------
# Schema-contract regression guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_search_indexes_entries_have_expected_keys(
    seeded_search_index: dict[str, str],
) -> None:
    """Schema contract: every list_search_indexes entry must carry the keys
    our summary formatter promises, so an SDK-shape change is caught early."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_search_indexes",
            arguments={
                "bucket_name": seeded_search_index["bucket_name"],
                "scope_name": seeded_search_index["scope_name"],
            },
        )
        payload = extract_payload(response)

    assert isinstance(payload, list) and payload
    required_keys = {
        "name",
        "uuid",
        "source_name",
        "source_type",
        "idx_type",
        "bucket",
        "scope",
    }
    for entry in payload:
        missing = required_keys - entry.keys()
        assert not missing, f"Search index entry missing keys: {sorted(missing)}"
