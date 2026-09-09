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

import contextlib
import os
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)
from couchbase.mutation_state import MutationState
from couchbase.options import SearchOptions
from couchbase.search import RawQuery, SearchRequest

from cb_mcp.utils.connection import connect_to_couchbase_cluster

try:
    from couchbase.management.search import SearchIndex
except ImportError:  # pragma: no cover - SDK always provides this in practice
    SearchIndex = None

# How long the fixture waits for the freshly-created Search index to actually
# index the seeded document. FTS indexing is asynchronous and, on a constrained
# CI node, a just-upserted index answers queries with HTTP 400 "pindex not
# available" for a while before becoming queryable. Env-overridable in the same
# spirit as conftest's CB_MCP_TEST_TIMEOUT.
FTS_READY_TIMEOUT = int(os.getenv("CB_MCP_FTS_READY_TIMEOUT", "300"))
FTS_READY_POLL_INTERVAL = 5
# Per-attempt server-side budget. Each attempt asks the Search service to wait
# (via consistent_with) until it has indexed our mutation, so an attempt that
# hits this is "still catching up", not a hard failure — the outer loop retries.
FTS_READY_ATTEMPT_TIMEOUT = 30


def _direct_cluster():
    """Open a direct (non-MCP) SDK connection using the same env vars the
    MCP server subprocess uses, for seeding/cleanup only."""
    connection_string = os.environ["CB_CONNECTION_STRING"]
    username = os.environ["CB_USERNAME"]
    password = os.environ["CB_PASSWORD"]
    return connect_to_couchbase_cluster(connection_string, username, password)


def _wait_for_seeded_document(
    scope, index_name: str, marker: str, mutation_state: MutationState
) -> tuple[bool, str | None]:
    """Block until the seeded document is findable through the Search index.

    Returns ``(indexed, last_error)`` — the error is carried out rather than
    swallowed so a failure says *why* (e.g. "pindex not available") instead of
    just reporting that the wait elapsed.

    Uses ``consistent_with`` (AT_PLUS) so the Search service itself waits until
    it has indexed our specific mutation, rather than us guessing with blind
    polling. The outer loop only exists to ride out the window where the index
    is too young to answer at all.

    Polls with the direct SDK rather than through an MCP session on purpose:
    conftest's ``create_mcp_session`` wraps the *entire* session in
    ``asyncio.timeout(DEFAULT_TIMEOUT)``, so a long wait inside one always
    trips that deadline. This is a plain synchronous fixture, so blocking here
    touches neither the event loop nor any MCP session.

    Queries the unique per-run ``marker`` token, not ``match_all`` — the index
    covers the whole test collection, which in CI holds pre-existing
    travel-sample documents, so ``match_all`` would return hits from that data
    and tell us nothing about whether *our* document has been indexed yet.
    """
    request = SearchRequest.create(RawQuery({"match": marker, "field": "marker"}))
    deadline = time.monotonic() + FTS_READY_TIMEOUT
    last_error: str | None = None
    while time.monotonic() < deadline:
        # A young index legitimately errors ("pindex not available") for a
        # while after creation, so a failed attempt is a retry, not a fault.
        try:
            options = SearchOptions(
                limit=5,
                consistent_with=mutation_state,
                timeout=timedelta(seconds=FTS_READY_ATTEMPT_TIMEOUT),
            )
            if list(scope.search(index_name, request, options).rows()):
                return True, None
            last_error = "query succeeded but returned no rows for the seeded marker"
        except Exception as e:
            # Reported back to the caller, not swallowed.
            last_error = f"{type(e).__name__}: {e}"
        time.sleep(FTS_READY_POLL_INTERVAL)
    return False, last_error


@pytest.fixture(scope="module")
def seeded_search_index() -> Iterator[dict[str, Any]]:
    """Create a scope-level Search index on the test bucket/scope/collection,
    seed one uniquely-markered document into that collection, wait for the
    index to actually pick that document up, and drop the index (and doc)
    afterward.

    Waiting here rather than in each test means the (potentially slow) FTS
    catch-up is paid once per module and stays out of every MCP session.
    """
    if SearchIndex is None:
        pytest.skip("couchbase.management.search.SearchIndex is unavailable")

    bucket_name = require_test_bucket()
    scope_name = get_test_scope()
    collection_name = get_test_collection()
    index_name = f"test_fts_idx_{uuid.uuid4().hex[:8]}"
    doc_id = f"test_fts_doc_{uuid.uuid4().hex[:8]}"
    # A single lowercase-alphanumeric token: the standard analyzer splits on
    # non-alphanumerics, so this stays one term and cannot collide with any
    # pre-existing document in the collection.
    marker = uuid.uuid4().hex[:8]

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
        mutation_result = collection.upsert(
            doc_id,
            {"name": "fts-seed-document", "kind": "test", "marker": marker},
        )

        indexed, index_error = _wait_for_seeded_document(
            bucket.scope(scope_name),
            index_name,
            marker,
            MutationState(mutation_result),
        )

        try:
            yield {
                "index_name": index_name,
                "bucket_name": bucket_name,
                "scope_name": scope_name,
                "collection_name": collection_name,
                "doc_id": doc_id,
                "marker": marker,
                "indexed": indexed,
                "index_error": index_error,
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
    always has a cluster-level index to find instead of skipping.

    The index deliberately matches **zero documents**: it is only ever listed,
    never queried, and an index left on its default dynamic mapping would
    index the entire bucket. On a CI node with the minimum ftsMemoryQuota
    that competes with the scope-level index this module actually queries,
    which is enough to keep the latter stuck on "pindex not available".
    Mapping a type_field value no document carries keeps it free. (The server
    rejects an index with no enabled mapping at all, and a mapping naming a
    nonexistent scope/collection, so this is the cheapest legal option.)
    """
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
            params={
                "doc_config": {"mode": "type_field", "type_field": "type"},
                "mapping": {
                    "default_mapping": {"enabled": False},
                    "types": {
                        "__cb_mcp_no_such_type__": {
                            "enabled": True,
                            "dynamic": False,
                            "properties": {},
                        }
                    },
                    "default_analyzer": "standard",
                },
            },
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
    seeded_search_index: dict[str, Any],
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
    seeded_search_index: dict[str, Any],
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
    seeded_search_index: dict[str, Any],
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
    seeded_search_index: dict[str, Any],
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


def _assert_query_succeeded(payload: object) -> dict[str, Any]:
    """Fail with the tool's own error message rather than a bare KeyError.

    run_fts_query reports Search-service failures as {"error": ...}, so
    indexing into a success key first turns a real, readable server error
    (e.g. "pindex not available") into an opaque KeyError.
    """
    assert isinstance(payload, dict), f"Expected a dict payload, got: {payload!r}"
    assert "error" not in payload, f"run_fts_query failed: {payload['error']}"
    return payload


@pytest.mark.asyncio
async def test_run_fts_query_match_all(seeded_search_index: dict[str, Any]) -> None:
    """A match_all query against the seeded index must return real hits with
    a well-formed shape.

    The fixture has already waited for the index to pick up the seeded
    document, so 0 hits here is a genuine failure rather than indexing lag.
    Note this deliberately does not assert *which* document comes back first:
    the index covers the whole test collection, which in CI holds ~1500
    travel-sample documents, so match_all returns arbitrary ones.
    test_run_fts_query_matches_seeded_document covers document identity.
    """
    assert seeded_search_index["indexed"], (
        f"Search index never picked up the seeded document within "
        f"{FTS_READY_TIMEOUT}s. Last error from the Search service: "
        f"{seeded_search_index['index_error']}"
    )

    async with create_mcp_session() as session:
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
        payload = _assert_query_succeeded(extract_payload(response))

    assert payload["index_name"] == seeded_search_index["index_name"]
    assert payload["total_hits"] > 0
    assert isinstance(payload["hits"], list) and payload["hits"]
    hit = payload["hits"][0]
    assert "id" in hit and "score" in hit and "fields" in hit
    assert "metadata" in payload


@pytest.mark.asyncio
async def test_run_fts_query_matches_seeded_document(
    seeded_search_index: dict[str, Any],
) -> None:
    """Querying the seeded document's unique marker must return exactly that
    document — the real proof that search returns the right result, and
    deterministic whether or not the collection holds other data."""
    assert seeded_search_index["indexed"], (
        f"Search index never picked up the seeded document within "
        f"{FTS_READY_TIMEOUT}s. Last error from the Search service: "
        f"{seeded_search_index['index_error']}"
    )

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_fts_query",
            arguments={
                "index_name": seeded_search_index["index_name"],
                "bucket_name": seeded_search_index["bucket_name"],
                "scope_name": seeded_search_index["scope_name"],
                "query": {"match": seeded_search_index["marker"], "field": "marker"},
                "limit": 5,
            },
        )
        payload = _assert_query_succeeded(extract_payload(response))

    assert payload["total_hits"] == 1
    assert payload["hits"][0]["id"] == seeded_search_index["doc_id"]


@pytest.mark.asyncio
async def test_run_fts_query_explain_default_limit(
    seeded_search_index: dict[str, Any],
) -> None:
    """run_fts_query with explain=True and no limit must default to 1 and
    return an explanation for the single returned hit."""
    assert seeded_search_index["indexed"], (
        f"Search index never picked up the seeded document within "
        f"{FTS_READY_TIMEOUT}s. Last error from the Search service: "
        f"{seeded_search_index['index_error']}"
    )

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
        payload = _assert_query_succeeded(extract_payload(response))

    assert payload["explain"] is True
    assert len(payload["hits"]) == 1
    assert "explanation" in payload["hits"][0]


# ---------------------------------------------------------------------------
# Schema-contract regression guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_search_indexes_entries_have_expected_keys(
    seeded_search_index: dict[str, Any],
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
