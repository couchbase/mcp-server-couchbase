"""
Integration tests for vector_index.py (create_query_index).

Tests for:
- create_query_index: scalar parity with the deprecated create_index,
  Hyperscale vector creation, Composite vector creation with an explicit
  vector key position, rejecting "DOT_PRODUCT" (FTS's spelling, not GSI's),
  and the documented dimension-mismatch-is-silently-excluded behavior.

Hyperscale/Composite indexes are built immediately (deferred=False) rather
than left deferred, since the dimension-mismatch test needs the index
actually online to observe exclusion behavior via a query -- there is no
error to see right after create (confirmed against docs.couchbase.com and
live: a mismatched vector is treated as NULL and the document is silently
excluded, not a build failure). Seeding uses a direct (non-MCP) SDK
connection, same convention as test_vector_search_tools.py and
test_fts_tools.py -- there is no MCP write tool for KV seeding fixtures to
go through here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)

from cb_mcp.utils.operational.connection import (
    connect_to_couchbase_cluster,
    resolve_cluster_major_version,
)

INDEX_READY_TIMEOUT = int(os.getenv("CB_MCP_VECTOR_READY_TIMEOUT", "60"))
INDEX_READY_POLL_INTERVAL = 2


def _direct_cluster():
    """Open a direct (non-MCP) SDK connection, for seeding/cleanup only."""
    connection_string = os.environ["CB_CONNECTION_STRING"]
    username = os.environ["CB_USERNAME"]
    password = os.environ["CB_PASSWORD"]
    return connect_to_couchbase_cluster(connection_string, username, password)


def _skip_if_cluster_below(cluster, min_major: int, *, feature: str) -> None:
    """Skip only when the cluster's version *confirms* the feature is
    unsupported -- if version detection itself fails, let the real DDL
    attempt be the source of truth instead of silently skipping."""
    try:
        major = resolve_cluster_major_version(cluster)
    except Exception:
        return
    if major < min_major:
        pytest.skip(
            f"Cluster reports major version {major}; {feature} requires "
            f"Couchbase Server {min_major}.0+."
        )


async def _drop_query_index_quietly(
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


async def _poll_list_indexes(
    session, bucket, scope, collection, index_name, *, until_online=False
):
    """Poll list_indexes until the named index appears (and, if requested,
    reaches 'online'), mirroring test_index_tools.py's own retry pattern for
    a just-created index's metadata propagating asynchronously."""
    deadline = time.monotonic() + INDEX_READY_TIMEOUT
    indexes = None
    while time.monotonic() < deadline:
        response = await session.call_tool(
            "list_indexes",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "index_name": index_name,
            },
        )
        indexes = extract_payload(response)
        if indexes and (
            not until_online or indexes[0].get("status", "").lower() == "online"
        ):
            return indexes
        await asyncio.sleep(INDEX_READY_POLL_INTERVAL)
    return indexes


@pytest.mark.asyncio
async def test_create_query_index_scalar_behaves_like_legacy_create_index() -> None:
    """index_type="scalar" produces the same observable outcome as the
    deprecated create_index: a deferred scalar index list_indexes can see,
    classified is_vector=False."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_cqi_scalar_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            response = await session.call_tool(
                "create_query_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "index_type": "scalar",
                    "keys": ["email"],
                },
            )
            payload = extract_payload(response)
            assert payload["success"] is True, f"create_query_index failed: {payload}"
            assert payload["deferred"] is True
            assert payload["index_type"] == "scalar"

            indexes = await _poll_list_indexes(
                session, bucket, scope, collection, index_name
            )
            assert isinstance(indexes, list) and len(indexes) == 1, (
                f"Expected exactly one index named {index_name!r}; got: {indexes!r}"
            )
            assert indexes[0]["is_vector"] is False
            assert "vector_type" not in indexes[0]
        finally:
            await _drop_query_index_quietly(
                session, bucket, scope, collection, index_name
            )


@pytest.mark.asyncio
async def test_create_query_index_hyperscale_with_explicit_dimension() -> None:
    """index_type="hyperscale_vector" creates a real Hyperscale Vector Index,
    confirmed online and correctly classified by list_indexes's
    is_vector/vector_type (see index_utils.classify_vector_index)."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_cqi_hs_{uuid.uuid4().hex[:8]}"
    vector_field = "test_cqi_embedding"
    tag = uuid.uuid4().hex[:8]

    cluster = _direct_cluster()
    doc_ids: list[str] = []
    try:
        _skip_if_cluster_below(cluster, 8, feature="Hyperscale Vector Indexes")
        coll = cluster.bucket(bucket).scope(scope).collection(collection)
        # Enough documents for IVF to train on -- too few can leave the
        # index build retrying indefinitely rather than reaching "online".
        for i in range(40):
            doc_id = f"test_cqi_doc_{tag}_{i}"
            coll.upsert(
                doc_id, {"test_cqi_tag": tag, vector_field: [0.1 * i, 0.2, 0.3, 0.4]}
            )
            doc_ids.append(doc_id)

        async with create_mcp_session() as session:
            try:
                response = await session.call_tool(
                    "create_query_index",
                    arguments={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": index_name,
                        "index_type": "hyperscale_vector",
                        "vector_field": vector_field,
                        "dimension": 4,
                        "similarity": "COSINE",
                        "deferred": False,
                    },
                )
                payload = extract_payload(response)
                assert payload["success"] is True, (
                    f"create_query_index failed: {payload}"
                )
                assert "next_step" not in payload

                indexes = await _poll_list_indexes(
                    session, bucket, scope, collection, index_name, until_online=True
                )
                assert indexes and indexes[0].get("status", "").lower() == "online", (
                    f"index did not reach online within {INDEX_READY_TIMEOUT}s: {indexes!r}"
                )
                assert indexes[0]["is_vector"] is True
                assert indexes[0]["vector_type"] == "hyperscale"
            finally:
                await _drop_query_index_quietly(
                    session, bucket, scope, collection, index_name
                )
    finally:
        for doc_id in doc_ids:
            with contextlib.suppress(Exception):
                coll.remove(doc_id)
        with contextlib.suppress(Exception):
            cluster.close()


@pytest.mark.asyncio
async def test_create_query_index_composite_vector_key_position() -> None:
    """index_type="composite_vector" with an explicit vector_index_position
    places the vector key where asked, and classifies as
    vector_type="composite" (multi-key index_key, per classify_vector_index)."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_cqi_comp_{uuid.uuid4().hex[:8]}"
    vector_field = "test_cqi_embedding"

    # Unlike the Hyperscale/dimension-mismatch tests, this one has no other
    # reason to open a direct SDK connection -- but it still needs one for
    # the version check, since a pre-8.0 cluster's SQL++ parser doesn't even
    # recognize VECTOR as a keyword (a syntax error, not a clean tool_error
    # from a version-aware check) -- confirmed by this test failing exactly
    # that way in CI before this guard was added.
    cluster = _direct_cluster()
    try:
        _skip_if_cluster_below(cluster, 8, feature="Composite Vector Indexes")
    finally:
        with contextlib.suppress(Exception):
            cluster.close()

    async with create_mcp_session() as session:
        try:
            response = await session.call_tool(
                "create_query_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "index_type": "composite_vector",
                    "keys": ["type"],
                    "vector_field": vector_field,
                    "vector_index_position": 0,
                    "dimension": 4,
                    "similarity": "COSINE",
                },
            )
            payload = extract_payload(response)
            assert payload["success"] is True, f"create_query_index failed: {payload}"
            assert "USING GSI" in payload["statement"]
            # vector_index_position=0 puts the vector key before "type" in
            # the statement we sent -- confirm the key order survived.
            assert payload["statement"].index("VECTOR") < payload["statement"].index(
                "type"
            )

            indexes = await _poll_list_indexes(
                session, bucket, scope, collection, index_name
            )
            assert isinstance(indexes, list) and len(indexes) == 1
            entry = indexes[0]
            assert entry["is_vector"] is True
            assert entry["vector_type"] == "composite"
            # Couchbase re-serializes the definition it echoes back (e.g.
            # quoting bare identifiers) -- confirm key order there too,
            # rather than assuming it matches our submitted text verbatim.
            assert entry["definition"].index("VECTOR") < entry["definition"].index(
                "type"
            )
        finally:
            await _drop_query_index_quietly(
                session, bucket, scope, collection, index_name
            )


@pytest.mark.asyncio
async def test_create_query_index_rejects_dot_product_similarity() -> None:
    """ "DOT_PRODUCT" is the Search (FTS) service's spelling, not GSI's -- it
    must be rejected with a tool_error, and no index actually created."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_cqi_dotproduct_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            response = await session.call_tool(
                "create_query_index",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                    "index_type": "hyperscale_vector",
                    "vector_field": "embedding",
                    "dimension": 4,
                    "similarity": "DOT_PRODUCT",
                },
            )
            payload = extract_payload(response)
            assert payload["success"] is False
            assert "DOT_PRODUCT" in payload["error"]

            list_response = await session.call_tool(
                "list_indexes",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "index_name": index_name,
                },
            )
            assert extract_payload(list_response) is None
        finally:
            await _drop_query_index_quietly(
                session, bucket, scope, collection, index_name
            )


@pytest.mark.asyncio
async def test_create_query_index_dimension_mismatch_silently_excluded() -> None:
    """A document whose vector doesn't match the index's dimension is not a
    build error -- per create_query_index's docstring (confirmed against
    docs.couchbase.com), Couchbase treats it as NULL and the document is
    silently excluded from the index. Confirmed here by querying the live
    index and asserting the mismatched document never comes back as a hit,
    while correctly-dimensioned documents do."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    index_name = f"test_cqi_dimmismatch_{uuid.uuid4().hex[:8]}"
    vector_field = "test_cqi_embedding"
    tag = uuid.uuid4().hex[:8]

    cluster = _direct_cluster()
    doc_ids: list[str] = []
    try:
        _skip_if_cluster_below(cluster, 8, feature="Hyperscale Vector Indexes")
        coll = cluster.bucket(bucket).scope(scope).collection(collection)
        for i in range(40):
            doc_id = f"test_cqi_dim_doc_{tag}_{i}"
            coll.upsert(
                doc_id, {"test_cqi_tag": tag, vector_field: [0.1 * i, 0.2, 0.3, 0.4]}
            )
            doc_ids.append(doc_id)
        bad_doc_id = f"test_cqi_dim_doc_{tag}_bad"
        # One extra dimension than the index will be created with -- this
        # must never appear in a query against the index.
        coll.upsert(
            bad_doc_id, {"test_cqi_tag": tag, vector_field: [0.1, 0.2, 0.3, 0.4, 0.5]}
        )
        doc_ids.append(bad_doc_id)

        async with create_mcp_session() as session:
            try:
                response = await session.call_tool(
                    "create_query_index",
                    arguments={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": index_name,
                        "index_type": "hyperscale_vector",
                        "vector_field": vector_field,
                        "dimension": 4,
                        "similarity": "COSINE",
                        "deferred": False,
                    },
                )
                payload = extract_payload(response)
                assert payload["success"] is True, (
                    f"create_query_index failed: {payload}"
                )

                indexes = await _poll_list_indexes(
                    session, bucket, scope, collection, index_name, until_online=True
                )
                assert indexes and indexes[0].get("status", "").lower() == "online", (
                    f"index did not reach online within {INDEX_READY_TIMEOUT}s: {indexes!r}"
                )

                # Raw SQL++, not run_vector_search: this is a GSI-indexing
                # behavior check, independent of the embedding pipeline, and
                # run_vector_search always embeds query_text rather than
                # taking a literal vector.
                query = (
                    f"SELECT META(doc).id AS id FROM `{collection}` AS doc "
                    f"WHERE doc.test_cqi_tag = $tag "
                    f"ORDER BY APPROX_VECTOR_DISTANCE(doc.`{vector_field}`, "
                    f'$qvec, "COSINE") LIMIT 100'
                )
                query_response = await session.call_tool(
                    "run_sql_plus_plus_query",
                    arguments={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "query": query,
                        "named_parameters": {
                            "tag": tag,
                            "qvec": [0.1, 0.2, 0.3, 0.4],
                        },
                    },
                )
                rows = extract_payload(query_response)
                hit_ids = {row["id"] for row in rows} if rows else set()
                assert bad_doc_id not in hit_ids, (
                    f"dimension-mismatched document {bad_doc_id!r} was indexed "
                    f"(should have been silently excluded): {hit_ids!r}"
                )
                # ANN search doesn't guarantee exhaustive recall, but at
                # least one correctly-dimensioned document should come back.
                assert hit_ids, "expected at least one correctly-dimensioned hit"
            finally:
                await _drop_query_index_quietly(
                    session, bucket, scope, collection, index_name
                )
    finally:
        for doc_id in doc_ids:
            with contextlib.suppress(Exception):
                coll.remove(doc_id)
        with contextlib.suppress(Exception):
            cluster.close()
