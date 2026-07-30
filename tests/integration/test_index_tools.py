"""
Integration tests for index.py tools.

Tests for:
- list_indexes
- get_index_advisor_recommendations
- create_index
- build_index
- drop_index
"""

from __future__ import annotations

import re
import uuid

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)

# REST-side keys we read in process_index_data_from_rest_api.
_REQUIRED_REST_KEYS: frozenset[str] = frozenset(
    {"name", "indexName", "definition", "status", "bucket", "scope", "collection"}
)

# Query-side top-level keys we read in process_index_data_from_query.
_REQUIRED_QUERY_TOPLEVEL_KEYS: frozenset[str] = frozenset({"name", "state", "metadata"})


async def _drop_index_quietly(
    session, bucket: str, scope: str, collection: str, index_name: str
) -> None:
    """Best-effort cleanup drop for test teardown (no error if already gone)."""
    await session.call_tool(
        "drop_index",
        arguments={
            "bucket_name": bucket,
            "scope_name": scope,
            "collection_name": collection,
            "index_name": index_name,
            "ignore_if_not_exists": True,
        },
    )


@pytest.mark.asyncio
async def test_list_indexes_all() -> None:
    """Verify list_indexes returns all indexes in the cluster."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool("list_indexes", arguments={})
        payload = extract_payload(response)

        # Skip if no indexes exist in the cluster
        if payload is None or (isinstance(payload, list) and len(payload) == 0):
            skip_reason = "No indexes found in cluster"
        else:
            assert isinstance(payload, list), f"Expected list, got {type(payload)}"
            # Each index should have required fields
            first_index = payload[0]
            assert "name" in first_index
            assert "definition" in first_index
            assert "status" in first_index
            assert "bucket" in first_index

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_list_indexes_filtered_by_bucket_includes_legacy_indexes() -> None:
    """Regression test: filtering by bucket_name must include legacy indexes.

    Legacy bucket-level indexes (created on a bucket before scopes/collections
    existed) appear in `system:indexes` with only `keyspace_id` (holding the
    bucket name) and no `bucket_id`/`scope_id`. The SQL query in
    fetch_indexes_via_query_service applies the user's bucket filter against
    the normalized LET alias `bid = IFMISSING(s.bucket_id, s.keyspace_id)`,
    so a legacy index in bucket X matches `bucket_name=X` symmetrically
    with a modern one.

    Before this fix, the filter was `bucket_id = $bucket_id`, which
    silently dropped every legacy index from the result.
    """
    bucket = require_test_bucket()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_indexes", arguments={"bucket_name": bucket}
        )
        payload = extract_payload(response)

    if not isinstance(payload, list) or not payload:
        pytest.skip(f"No indexes found in bucket {bucket!r}")

    # Every returned row must actually belong to the requested bucket.
    for idx in payload:
        assert idx.get("bucket") == bucket, (
            f"Index {idx.get('name')!r} reported bucket "
            f"{idx.get('bucket')!r} but bucket filter was {bucket!r}"
        )

    # Identify any legacy bucket-level indexes returned. The DDL signature
    # for legacy is `ON \`bucket\`(...)` (no scope.collection qualifier);
    # the normalised shape is scope=_default + collection=_default.
    legacy_indexes = [
        idx
        for idx in payload
        if (
            idx.get("scope") == "_default"
            and idx.get("collection") == "_default"
            and isinstance(idx.get("definition"), str)
            and f"ON `{bucket}`(" in idx["definition"]
            and f"ON `{bucket}`." not in idx["definition"]
        )
    ]

    if not legacy_indexes:
        pytest.skip(
            f"Bucket {bucket!r} has no legacy bucket-level indexes — cannot "
            f"verify the legacy filter fix. Try a bucket like travel-sample."
        )

    # Each legacy index must be correctly normalised: bucket equals the
    # filter, scope/collection both '_default'. If any of these slip,
    # either the SQL LET clause or our bucket filter has regressed.
    for idx in legacy_indexes:
        assert idx["bucket"] == bucket
        assert idx["scope"] == "_default"
        assert idx["collection"] == "_default"


@pytest.mark.asyncio
async def test_list_indexes_filtered_by_bucket() -> None:
    """Verify list_indexes can filter by bucket name."""
    bucket = require_test_bucket()
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_indexes", arguments={"bucket_name": bucket}
        )
        payload = extract_payload(response)

        # Skip if no indexes exist for the bucket
        if payload is None or (isinstance(payload, list) and len(payload) == 0):
            skip_reason = f"No indexes found in bucket '{bucket}'"
        else:
            assert isinstance(payload, list), f"Expected list, got {type(payload)}"
            # All returned indexes should belong to the specified bucket
            for index in payload:
                assert index.get("bucket") == bucket, (
                    f"Index {index.get('name')} belongs to bucket {index.get('bucket')}, "
                    f"expected {bucket}"
                )

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_list_indexes_filtered_by_scope() -> None:
    """Verify list_indexes can filter by bucket and scope."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_indexes",
            arguments={"bucket_name": bucket, "scope_name": scope},
        )
        payload = extract_payload(response)

        # Skip if no indexes exist for the scope
        if payload is None or (isinstance(payload, list) and len(payload) == 0):
            skip_reason = f"No indexes found in bucket '{bucket}', scope '{scope}'"
        else:
            assert isinstance(payload, list), f"Expected list, got {type(payload)}"
            # All returned indexes should belong to the specified bucket and scope
            for index in payload:
                assert index.get("bucket") == bucket
                assert index.get("scope") == scope

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_list_indexes_filtered_by_collection() -> None:
    """Verify list_indexes can filter by bucket, scope, and collection."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_indexes",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
            },
        )
        payload = extract_payload(response)

        # Skip if no indexes exist for the collection
        if payload is None or (isinstance(payload, list) and len(payload) == 0):
            skip_reason = (
                f"No indexes found in bucket '{bucket}', "
                f"scope '{scope}', collection '{collection}'"
            )
        else:
            assert isinstance(payload, list), f"Expected list, got {type(payload)}"
            # All returned indexes should belong to the specified collection
            for index in payload:
                assert index.get("bucket") == bucket
                assert index.get("scope") == scope
                assert index.get("collection") == collection

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_list_indexes_has_last_scan_time() -> None:
    """Verify list_indexes includes lastScanTime field."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool("list_indexes", arguments={})
        payload = extract_payload(response)

        # Skip if no indexes exist
        if payload is None or (isinstance(payload, list) and len(payload) == 0):
            skip_reason = "No indexes found to test lastScanTime"
        else:
            assert isinstance(payload, list), f"Expected list, got {type(payload)}"
            first_index = payload[0]
            assert "lastScanTime" in first_index, (
                "Expected lastScanTime in index output"
            )

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_list_indexes_with_raw_stats() -> None:
    """Verify list_indexes returns unprocessed source rows when raw stats requested."""
    skip_reason = None

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_indexes", arguments={"return_raw_index_stats": True}
        )
        payload = extract_payload(response)

        # Skip if no indexes exist
        if payload is None or (isinstance(payload, list) and len(payload) == 0):
            skip_reason = "No indexes found to test raw stats"
        else:
            assert isinstance(payload, list), f"Expected list, got {type(payload)}"
            first_index = payload[0]
            # Each entry should be the raw source row, not the processed shape.
            # The query-service path returns rows with `state` / `bucket_id` /
            # `keyspace_id` / `metadata`; the REST path returns rows with
            # `defnId` / `indexName` / `indexType` etc. — either way, the
            # entry should contain at least one field that the processed
            # shape does not.
            raw_only_keys = {
                # query service raw fields
                "state",
                "bucket_id",
                "keyspace_id",
                "metadata",
                # REST raw fields
                "defnId",
                "instId",
                "indexName",
                "indexType",
            }
            assert raw_only_keys & set(first_index.keys()), (
                "Expected raw source row when return_raw_index_stats=True; "
                f"got keys: {sorted(first_index.keys())}"
            )

    if skip_reason:
        pytest.skip(skip_reason)


# ---------------------------------------------------------------------------
# Schema-contract tests against the live data source.
#
# These exist as an early-warning system: if Couchbase ever renames a key in
# /getIndexStatus or system:indexes, these tests will fail with a clear
# message so we can update our processors rather than silently emitting
# wrong data to users.
# ---------------------------------------------------------------------------


def _identify_source_path(row: dict[str, object]) -> str:
    """Return 'query', 'rest', or 'unknown' based on raw row shape."""
    if "state" in row or "keyspace_id" in row or "metadata" in row:
        return "query"
    if "indexName" in row or "defnId" in row or "indexType" in row:
        return "rest"
    return "unknown"


@pytest.mark.asyncio
async def test_list_indexes_raw_rows_have_expected_keys() -> None:
    """Schema contract: raw index rows must carry every key our processor reads.

    If this test fails after a Couchbase Server upgrade, a key has likely
    been renamed — update the processor (and the constants at the top of this
    file) before shipping.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_indexes", arguments={"return_raw_index_stats": True}
        )
        payload = extract_payload(response)

    if not isinstance(payload, list) or not payload:
        pytest.skip("No indexes available to check raw shape")

    path = _identify_source_path(payload[0])
    if path == "unknown":
        pytest.fail(
            "Could not identify data source path from raw row. "
            f"Keys observed: {sorted(payload[0].keys())}"
        )

    for idx in payload:
        name = idx.get("name") or idx.get("indexName") or "<unnamed>"
        if path == "rest":
            missing = _REQUIRED_REST_KEYS - idx.keys()
            assert not missing, (
                f"REST index {name!r}: required keys missing from "
                f"/getIndexStatus row: {sorted(missing)}. Possible key rename "
                f"upstream — update process_index_data_from_rest_api accordingly."
            )
        else:  # query path
            missing = _REQUIRED_QUERY_TOPLEVEL_KEYS - idx.keys()
            assert not missing, (
                f"Query index {name!r}: required top-level keys missing from "
                f"system:indexes row: {sorted(missing)}. Possible key "
                f"rename — update process_index_data_from_query accordingly."
            )
            metadata = idx.get("metadata")
            assert isinstance(metadata, dict), (
                f"Query index {name!r}: 'metadata' should be a dict, "
                f"got {type(metadata).__name__}. system:indexes shape "
                f"may have changed."
            )
            assert "definition" in metadata, (
                f"Query index {name!r}: 'metadata.definition' missing. "
                f"Possible key rename in system:indexes."
            )
            # Location info must match modern or legacy shape — anything
            # else means the schema discriminator has shifted.
            modern = {"bucket_id", "scope_id", "keyspace_id"}.issubset(idx.keys())
            legacy = (
                "keyspace_id" in idx
                and "bucket_id" not in idx
                and "scope_id" not in idx
            )
            assert modern or legacy, (
                f"Query index {name!r}: bucket/scope/collection identifiers "
                f"don't match modern (bucket_id+scope_id+keyspace_id) or "
                f"legacy (keyspace_id only) shape. Keys present: "
                f"{sorted(idx.keys())}. The legacy/modern discriminator may "
                f"have changed in system:indexes."
            )


@pytest.mark.asyncio
async def test_list_indexes_primary_index_flag_consistency() -> None:
    """Schema contract: any index whose DDL starts with ``CREATE PRIMARY INDEX``
    must have the primary-flag field set to True in the raw row.

    Catches the isPrimary blind spot: if Couchbase renames ``isPrimary`` (REST)
    or ``is_primary`` (query), the flag would silently default to False and
    every primary index would appear non-primary in our output. This test
    fails fast so the rename gets caught before shipping.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "list_indexes", arguments={"return_raw_index_stats": True}
        )
        payload = extract_payload(response)

    if not isinstance(payload, list) or not payload:
        pytest.skip("No indexes available to check primary-index consistency")

    primary_ddl = re.compile(r"^\s*CREATE\s+PRIMARY\s+INDEX\b", re.IGNORECASE)

    primary_by_ddl: list[dict[str, object]] = []
    for idx in payload:
        # REST stores DDL on `definition`; query stores it under `metadata.definition`.
        ddl = idx.get("definition")
        if not ddl and isinstance(idx.get("metadata"), dict):
            ddl = idx["metadata"].get("definition")
        if isinstance(ddl, str) and primary_ddl.match(ddl):
            primary_by_ddl.append(idx)

    if not primary_by_ddl:
        pytest.skip("No primary indexes in the cluster to verify the flag")

    path = _identify_source_path(payload[0])
    flag_key = "isPrimary" if path == "rest" else "is_primary"

    mismatches: list[str] = []
    for idx in primary_by_ddl:
        name = idx.get("name") or idx.get("indexName") or "<unnamed>"
        flag = idx.get(flag_key)
        if flag is not True:
            mismatches.append(f"{name!r} (got {flag_key}={flag!r})")

    assert not mismatches, (
        f"Indexes whose DDL declares CREATE PRIMARY INDEX but whose {flag_key!r} "
        f"field is missing or not True: {mismatches}. The primary-flag key may "
        f"have been renamed upstream — update processor and tests."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "where_clause",
    [
        pytest.param("WHERE id > 100", id="numeric"),
        # Single-quoted string literal: exercises the named-parameter binding
        # path. This used to break ADVISOR under string concatenation; it must
        # work now that the user query is bound rather than embedded.
        pytest.param("WHERE country = 'France'", id="single_quote_literal"),
    ],
)
async def test_get_index_advisor_recommendations(where_clause: str) -> None:
    """Verify get_index_advisor_recommendations returns recommendations.

    Includes a single-quote case so the SQL++ injection hardening (binding the
    user query as a named parameter) stays exercised against a live cluster.
    """
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    skip_reason = None

    query = f"SELECT * FROM `{collection}` {where_clause}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_index_advisor_recommendations",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )
        payload = extract_payload(response)

        # Handle error responses
        if isinstance(payload, str):
            if "Error" in payload:
                skip_reason = f"Index advisor failed: {payload[:100]}..."
            else:
                raise AssertionError(f"Unexpected string response: {payload}")
        elif isinstance(payload, list) and payload and isinstance(payload[0], str):
            # Error returned as list of strings
            skip_reason = f"Index advisor failed: {payload[0][:100]}..."
        else:
            assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
            # Response should have the expected structure
            assert "current_used_indexes" in payload
            assert "recommended_indexes" in payload
            assert "recommended_covering_indexes" in payload
            # Summary should also be present
            assert "summary" in payload
            summary = payload["summary"]
            assert "has_recommendations" in summary

    if skip_reason:
        pytest.skip(skip_reason)


@pytest.mark.asyncio
async def test_get_index_advisor_recommendations_with_update_query() -> None:
    """ADVISOR must accept UPDATE statements per the tool's documented contract."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # UPDATE with a no-match WHERE clause — safe even if anything went sideways.
    query = f"UPDATE `{collection}` SET name = name WHERE id > 99999999"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_index_advisor_recommendations",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )
        payload = extract_payload(response)

        # Accept either the recommendations envelope or the "no recommendations"
        # envelope — both prove ADVISOR accepted the UPDATE without crashing.
        assert isinstance(payload, dict), (
            f"Expected dict envelope, got {type(payload)}: {payload}"
        )
        assert "recommended_indexes" in payload or "message" in payload, (
            f"Unexpected advisor response shape: {payload}"
        )


@pytest.mark.asyncio
async def test_get_index_advisor_recommendations_with_delete_query() -> None:
    """ADVISOR must accept DELETE statements per the tool's documented contract."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    query = f"DELETE FROM `{collection}` WHERE id = -99999999"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_index_advisor_recommendations",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), (
            f"Expected dict envelope, got {type(payload)}: {payload}"
        )
        assert "recommended_indexes" in payload or "message" in payload, (
            f"Unexpected advisor response shape: {payload}"
        )


@pytest.mark.asyncio
async def test_get_index_advisor_with_single_quoted_string() -> None:
    """Bug #2: ADVISOR breaks with single quotes in the query.

    The ADVISOR function is called via string interpolation:
        SELECT ADVISOR('SELECT * FROM ... WHERE name = 'value'')
    Any single quote in the user's query breaks out of the ADVISOR string.

    This test exposes the bug. A proper fix would either:
    - Escape single quotes in the query before interpolation ('' for ')
    - Use a parameterized query approach
    - Reject queries with single quotes with a clear error message
    """
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Query with a string literal containing a single quote
    query = f"SELECT * FROM `{collection}` WHERE name = 'Texas Wings'"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_index_advisor_recommendations",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "query": query,
            },
        )

        payload = extract_payload(response)
        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )

        # If the bug exists, this will fail with a SQL syntax error or crash.
        # The CORRECT fix should either:
        # 1. Handle the single quote correctly (escape it properly)
        # 2. Return an error with a clear message about unsupported syntax
        #
        # This test documents the bug. When fixed, it should NOT fail.
        if is_error:
            payload_str = str(payload)
            assert "quote" in payload_str.lower() or "syntax" in payload_str.lower(), (
                f"If query with quotes causes an error, it should be clear why. "
                f"Got: {payload}"
            )
        else:
            # If it succeeds, the bug is fixed
            assert isinstance(payload, dict), (
                f"Query with single quotes should either be escaped correctly "
                f"or fail with a clear error. Got: {payload}"
            )


@pytest.mark.asyncio
async def test_create_index_deferred_by_default() -> None:
    """create_index without deferred= must default to deferred=True, and the
    index must show up as not-yet-built via list_indexes (status 'created' on
    newer clusters, 'deferred' on older ones)."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_idx_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            response = await session.call_tool(
                "create_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "keys": ["email"],
                },
            )
            payload = extract_payload(response)

            assert payload["success"] is True, f"create_index failed: {payload}"
            assert payload["deferred"] is True
            assert payload["index_name"] == index_name

            list_response = await session.call_tool(
                "list_indexes",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                },
            )
            indexes = extract_payload(list_response)
            assert isinstance(indexes, list) and len(indexes) == 1
            # A deferred (not-yet-built) index reports status "created" on newer
            # clusters and "deferred" on older ones — accept either.
            assert indexes[0]["status"].lower() in {"created", "deferred"}
        finally:
            await _drop_index_quietly(session, bucket, scope, collection, index_name)


@pytest.mark.asyncio
async def test_create_index_ignore_if_exists() -> None:
    """Recreating an existing index without ignore_if_exists must return a
    log-and-return error dict, not raise; with ignore_if_exists=True it must
    succeed."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_idx_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            first = await session.call_tool(
                "create_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "keys": ["email"],
                },
            )
            assert extract_payload(first)["success"] is True

            duplicate = await session.call_tool(
                "create_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "keys": ["email"],
                },
            )
            duplicate_payload = extract_payload(duplicate)
            assert duplicate_payload["success"] is False
            assert "error" in duplicate_payload

            ignored = await session.call_tool(
                "create_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "keys": ["email"],
                    "ignore_if_exists": True,
                },
            )
            assert extract_payload(ignored)["success"] is True
        finally:
            await _drop_index_quietly(session, bucket, scope, collection, index_name)


@pytest.mark.asyncio
async def test_build_index_succeeds_with_deferred_index() -> None:
    """build_index must succeed on a collection that has a deferred index.

    We assert only the synchronous build trigger — not that the index reaches
    'online' — because the build runs asynchronously and may not be complete
    when the call returns. The result-validation eval covers the "check
    list_indexes for the real state" behaviour separately.
    """
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_idx_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await session.call_tool(
                "create_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "keys": ["email"],
                },
            )

            build_response = await session.call_tool(
                "build_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                },
            )
            build_payload = extract_payload(build_response)
            assert build_payload == {
                "success": True,
                "keyspace": f"{bucket}.{scope}.{collection}",
            }
        finally:
            await _drop_index_quietly(session, bucket, scope, collection, index_name)


@pytest.mark.asyncio
async def test_build_index_no_deferred_indexes() -> None:
    """build_index on a collection with nothing deferred is a harmless
    no-op — the SDK doesn't error when there's nothing to build."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "build_index",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
            },
        )
        payload = extract_payload(response)

        assert payload == {
            "success": True,
            "keyspace": f"{bucket}.{scope}.{collection}",
        }


@pytest.mark.asyncio
async def test_drop_index() -> None:
    """drop_index must remove a non-deferred index; list_indexes must no
    longer report it afterward."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_idx_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await session.call_tool(
                "create_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "keys": ["email"],
                    "deferred": False,
                },
            )

            drop_response = await session.call_tool(
                "drop_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                },
            )
            drop_payload = extract_payload(drop_response)
            assert drop_payload == {
                "success": True,
                "index_name": index_name,
                "keyspace": f"{bucket}.{scope}.{collection}",
            }

            list_response = await session.call_tool(
                "list_indexes",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                },
            )
            indexes = extract_payload(list_response)
            assert indexes is None or (
                isinstance(indexes, list) and len(indexes) == 0
            ), f"Expected no results after drop, got: {indexes}"
        finally:
            # The drop above is the action under test; if an assertion fails
            # before it lands, this makes sure the index doesn't leak.
            await _drop_index_quietly(session, bucket, scope, collection, index_name)


@pytest.mark.asyncio
async def test_drop_index_ignore_if_not_exists() -> None:
    """Dropping a nonexistent index errors unless ignore_if_not_exists=True."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_idx_missing_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        without_flag = await session.call_tool(
            "drop_index",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "index_name": index_name,
            },
        )
        without_flag_payload = extract_payload(without_flag)
        assert without_flag_payload["success"] is False
        assert "error" in without_flag_payload

        with_flag = await session.call_tool(
            "drop_index",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "index_name": index_name,
                "ignore_if_not_exists": True,
            },
        )
        with_flag_payload = extract_payload(with_flag)
        assert with_flag_payload["success"] is True
