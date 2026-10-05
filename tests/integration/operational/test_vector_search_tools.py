"""
Integration tests for vector_search.py (run_vector_search, run_search_vector_search).

Requires, in addition to the usual CB_CONNECTION_STRING/CB_USERNAME/CB_PASSWORD/
CB_MCP_TEST_BUCKET: a configured embedding provider (EMBEDDING_PROVIDER plus
whatever that provider needs — EMBEDDING_MODEL/EMBEDDING_API_KEY/EMBEDDING_ENDPOINT/
EMBEDDING_AWS_*, see registry.PROVIDER_CONFIG_DOCS). Skips cleanly (not a
failure) when EMBEDDING_PROVIDER is unset, same convention as
CB_MCP_TEST_BUCKET. Because _build_env() (tests/_test_env.py) forwards the
whole current process environment to the spawned MCP server subprocess,
setting EMBEDDING_* before running pytest is sufficient -- no extra wiring.

There is no MCP write tool for either index kind here (out of scope for this
tool family, same as FTS indexes), so the fixtures below seed/drop indexes
directly: a GSI vector index via a raw SQL++ CREATE INDEX statement (there is
no Python SDK index-manager class for this -- it's SQL++-native), and a
Search vector index via the SDK's SearchIndex manager (test_fts_tools.py's
existing pattern, extended with a `vector` field mapping).

The fixtures compute the seeded document's embedding vector directly via
this project's own embedding-provider abstraction (bypassing MCP, same
spirit as _direct_cluster() bypassing MCP for seeding), both to learn the
real vector dimension for the index DDL/mapping (deploy-time dimension is
provider/model-dependent, so it can't be hardcoded) and to guarantee the
seeded document's vector is IDENTICAL to what run_vector_search /
run_search_vector_search will compute for the same marker text -- giving a
deterministic, distance ~ 0 top-1 match to assert on, the vector-space
equivalent of test_fts_tools.py's unique-marker proof.

NOTE: the exact CREATE INDEX ... WITH {...} and Search vector-field-mapping
JSON shapes below were confirmed against docs.couchbase.com/server/current/
vector-index/ during planning (see the implementation plan) but have not been
run against a live Couchbase Server 8.0+ cluster in this change -- verify
both against a real cluster before relying on this file's green/red result,
per CONTRIBUTING.md's "AI-generated code" expectations.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)
from couchbase.options import SearchOptions
from couchbase.search import SearchRequest
from couchbase.vector_search import VectorQuery, VectorSearch

from cb_mcp.utils.operational.connection import (
    connect_to_couchbase_cluster,
    resolve_cluster_major_version,
)
from cb_mcp.utils.operational.embeddings import (
    EmbeddingRequest,
    resolve_embedding_provider,
)

try:
    from couchbase.management.search import SearchIndex
except ImportError:  # pragma: no cover - SDK always provides this in practice
    SearchIndex = None

# How long to retry a just-built index before it's reliably queryable.
# GSI index build for a single-document collection is typically fast; this
# is a much shorter budget than test_fts_tools.py's FTS_READY_TIMEOUT.
INDEX_READY_TIMEOUT = int(os.getenv("CB_MCP_VECTOR_READY_TIMEOUT", "60"))
INDEX_READY_POLL_INTERVAL = 2


def _direct_cluster():
    """Open a direct (non-MCP) SDK connection, for seeding/cleanup only."""
    connection_string = os.environ["CB_CONNECTION_STRING"]
    username = os.environ["CB_USERNAME"]
    password = os.environ["CB_PASSWORD"]
    return connect_to_couchbase_cluster(connection_string, username, password)


def _embedding_settings_from_env() -> dict[str, Any]:
    return {
        "embedding_provider": os.getenv("EMBEDDING_PROVIDER"),
        "embedding_model": os.getenv("EMBEDDING_MODEL"),
        "embedding_api_key": os.getenv("EMBEDDING_API_KEY"),
        "embedding_endpoint": os.getenv("EMBEDDING_ENDPOINT"),
        "embedding_aws_access_key_id": os.getenv("EMBEDDING_AWS_ACCESS_KEY_ID"),
        "embedding_aws_secret_access_key": os.getenv("EMBEDDING_AWS_SECRET_ACCESS_KEY"),
        "embedding_aws_region": os.getenv("EMBEDDING_AWS_REGION"),
    }


def _require_embedding_provider() -> dict[str, Any]:
    settings = _embedding_settings_from_env()
    if not settings.get("embedding_provider"):
        pytest.skip("EMBEDDING_PROVIDER not set")
    return settings


def _skip_if_cluster_below(cluster, min_major: int, *, feature: str) -> None:
    """Skip only when the cluster's version *confirms* the feature is
    unsupported. If version detection itself fails, don't skip either way --
    "can't tell" isn't "confirmed unsupported"; let the real DDL attempt
    that follows be the source of truth, so a genuine bug in it fails the
    test loudly instead of silently blending into a version-gap skip.
    """
    try:
        major = resolve_cluster_major_version(cluster)
    except Exception:
        return
    if major < min_major:
        pytest.skip(
            f"Cluster reports major version {major}; {feature} requires "
            f"Couchbase Server {min_major}.0+."
        )


def _wait_for_search_vector_doc(
    bucket, scope_name: str, index_name: str, doc_id: str, vector_field: str, vector
) -> tuple[bool, str | None]:
    """Poll the Search vector index until it returns the seeded document.

    Mirrors test_fts_tools.py's _wait_for_seeded_document, but via a vector
    query (the seeded doc's own vector, so it's guaranteed the nearest match)
    rather than a marker-text match query.
    """
    deadline = time.monotonic() + INDEX_READY_TIMEOUT
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            vq = VectorQuery(vector_field, vector, num_candidates=1)
            request = SearchRequest.create(VectorSearch.from_vector_query(vq))
            rows = list(
                bucket.scope(scope_name)
                .search(index_name, request, SearchOptions(limit=1))
                .rows()
            )
            if rows and rows[0].id == doc_id:
                return True, None
            last_error = "query succeeded but did not return the seeded doc yet"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        time.sleep(INDEX_READY_POLL_INTERVAL)
    return False, last_error


@pytest.fixture(scope="module")
def seeded_gsi_vector_index() -> Iterator[dict[str, Any]]:
    """Seed one document with a marker-text embedding, create a Composite
    Vector Index on its embedding field, and drop both afterward."""
    settings = _require_embedding_provider()
    bucket_name = require_test_bucket()
    scope_name = get_test_scope()
    collection_name = get_test_collection()
    vector_field = "embedding"
    doc_id = f"test_vector_doc_{uuid.uuid4().hex[:8]}"
    marker = f"vector-search-marker-{uuid.uuid4().hex[:8]}"
    index_name = f"test_vec_idx_{uuid.uuid4().hex[:8]}"

    provider = resolve_embedding_provider(settings)
    embedding = provider.embed(
        EmbeddingRequest(text=marker, model=settings.get("embedding_model") or "")
    )

    cluster = _direct_cluster()
    try:
        _skip_if_cluster_below(cluster, 8, feature="GSI vector indexes")

        bucket = cluster.bucket(bucket_name)
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.upsert(
            doc_id, {"name": marker, "kind": "test", vector_field: embedding.vector}
        )

        scope = bucket.scope(scope_name)
        create_stmt = (
            f"CREATE INDEX `{index_name}` ON `{collection_name}`(`{vector_field}` VECTOR) "
            f'WITH {{"dimension": {embedding.dimensions}, "similarity": "cosine"}}'
        )
        # Not wrapped in try/skip: on a cluster confirmed (or unknown, see
        # _skip_if_cluster_below) to support vector indexing, any failure
        # here is a real bug in the DDL/mapping and must fail the test, not
        # vanish into a silent skip.
        list(scope.query(create_stmt))

        indexed = False
        last_error: str | None = None
        deadline = time.monotonic() + INDEX_READY_TIMEOUT
        probe = (
            f"SELECT META(b).id AS id FROM `{collection_name}` AS b "
            f"WHERE META(b).id = $doc_id "
            f'ORDER BY APPROX_VECTOR_DISTANCE(b.`{vector_field}`, $qvec, "cosine") '
            f"LIMIT 1"
        )
        while time.monotonic() < deadline:
            try:
                rows = list(
                    scope.query(
                        probe,
                        named_parameters={"doc_id": doc_id, "qvec": embedding.vector},
                    )
                )
                if rows:
                    indexed = True
                    break
                last_error = "query succeeded but returned no rows for the seeded doc"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            time.sleep(INDEX_READY_POLL_INTERVAL)

        try:
            yield {
                "bucket_name": bucket_name,
                "scope_name": scope_name,
                "collection_name": collection_name,
                "vector_field": vector_field,
                "doc_id": doc_id,
                "marker": marker,
                "indexed": indexed,
                "index_error": last_error,
            }
        finally:
            with contextlib.suppress(Exception):
                list(scope.query(f"DROP INDEX `{index_name}` ON `{collection_name}`"))
            with contextlib.suppress(Exception):
                collection.remove(doc_id)
    finally:
        with contextlib.suppress(Exception):
            cluster.close()


@pytest.fixture(scope="module")
def seeded_search_vector_index() -> Iterator[dict[str, Any]]:
    """Seed one document with a marker-text embedding into a scope-level
    Search index whose mapping includes a `vector` field, and drop both
    afterward."""
    if SearchIndex is None:
        pytest.skip("couchbase.management.search.SearchIndex is unavailable")

    settings = _require_embedding_provider()
    bucket_name = require_test_bucket()
    scope_name = get_test_scope()
    collection_name = get_test_collection()
    vector_field = "embedding"
    doc_id = f"test_search_vec_doc_{uuid.uuid4().hex[:8]}"
    marker = f"search-vector-marker-{uuid.uuid4().hex[:8]}"
    index_name = f"test_search_vec_idx_{uuid.uuid4().hex[:8]}"

    provider = resolve_embedding_provider(settings)
    embedding = provider.embed(
        EmbeddingRequest(text=marker, model=settings.get("embedding_model") or "")
    )

    cluster = _direct_cluster()
    try:
        # Floor is the best this can confirm: major-version granularity can't
        # distinguish 7.0-7.5 from the real 7.6+ requirement, but major < 7 is
        # still a confirmed-unsupported signal worth skipping on. See
        # _skip_if_cluster_below's docstring for why an unknown version
        # doesn't skip either way.
        _skip_if_cluster_below(cluster, 7, feature="Search-service vector indexes")

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
                            "dynamic": False,
                            "properties": {
                                vector_field: {
                                    "enabled": True,
                                    "fields": [
                                        {
                                            "name": vector_field,
                                            "type": "vector",
                                            "dims": embedding.dimensions,
                                            "similarity": "cosine",
                                            "index": True,
                                        }
                                    ],
                                },
                                # Indexed (not left to "dynamic": False's
                                # default of unindexed) so the hybrid test's
                                # scalar_query={"match": marker, "field":
                                # "name"} has real content to match -- without
                                # this, that scalar clause matches nothing and
                                # the test only passes on the vector half.
                                "name": {
                                    "enabled": True,
                                    "fields": [
                                        {"name": "name", "type": "text", "index": True}
                                    ],
                                },
                            },
                        }
                    },
                    "default_mapping": {"enabled": False},
                    "default_analyzer": "standard",
                },
            },
        )

        # Not wrapped in try/skip: see the GSI fixture's identical comment
        # above -- on a cluster confirmed (or unknown) to support it, any
        # failure here is a real bug, not a version gap, and must fail loud.
        scope_index_manager.upsert_index(definition)

        collection = bucket.scope(scope_name).collection(collection_name)
        collection.upsert(
            doc_id, {"name": marker, "kind": "test", vector_field: embedding.vector}
        )

        indexed, last_error = _wait_for_search_vector_doc(
            bucket, scope_name, index_name, doc_id, vector_field, embedding.vector
        )

        try:
            yield {
                "index_name": index_name,
                "bucket_name": bucket_name,
                "scope_name": scope_name,
                "vector_field": vector_field,
                "doc_id": doc_id,
                "marker": marker,
                "indexed": indexed,
                "index_error": last_error,
            }
        finally:
            with contextlib.suppress(Exception):
                scope_index_manager.drop_index(index_name)
            with contextlib.suppress(Exception):
                collection.remove(doc_id)
    finally:
        with contextlib.suppress(Exception):
            cluster.close()


def _assert_tool_succeeded(payload: object) -> dict[str, Any]:
    assert isinstance(payload, dict), f"Expected a dict payload, got: {payload!r}"
    assert payload.get("success") is True, f"tool call failed: {payload.get('error')}"
    return payload


@pytest.mark.asyncio
async def test_run_vector_search_finds_seeded_document(
    seeded_gsi_vector_index: dict[str, Any],
) -> None:
    """Querying the seeded marker text must return the seeded document as
    the top (and, for a single-document collection, only) hit."""
    fixture = seeded_gsi_vector_index
    assert fixture["indexed"], (
        f"GSI vector index never picked up the seeded document within "
        f"{INDEX_READY_TIMEOUT}s. Last error: {fixture['index_error']}"
    )

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_vector_search",
            arguments={
                "bucket_name": fixture["bucket_name"],
                "scope_name": fixture["scope_name"],
                "collection_name": fixture["collection_name"],
                "vector_field": fixture["vector_field"],
                "query_text": fixture["marker"],
                "limit": 1,
            },
        )
        payload = _assert_tool_succeeded(extract_payload(response))

    assert payload["total_hits"] == 1
    assert payload["hits"][0]["id"] == fixture["doc_id"]
    # Same text embedded through the same provider/model should be identical
    # (or near-identical) to the seeded vector -> ~0 distance.
    assert payload["hits"][0]["distance"] < 1e-3
    # The document is nested under "document", not flattened into the hit --
    # proves the id/distance metadata fields can't collide with same-named
    # document fields (see the "document" projection in run_vector_search).
    assert payload["hits"][0]["document"]["name"] == fixture["marker"]


@pytest.mark.asyncio
async def test_run_vector_search_missing_provider_returns_error(
    seeded_gsi_vector_index: dict[str, Any],
) -> None:
    """A cluster reachable but a subprocess started with no EMBEDDING_PROVIDER
    must return a tool_error, not raise or hang."""
    fixture = seeded_gsi_vector_index
    async with create_mcp_session(extra_env={"EMBEDDING_PROVIDER": ""}) as session:
        response = await session.call_tool(
            "run_vector_search",
            arguments={
                "bucket_name": fixture["bucket_name"],
                "scope_name": fixture["scope_name"],
                "collection_name": fixture["collection_name"],
                "vector_field": fixture["vector_field"],
                "query_text": "irrelevant, provider is unset",
            },
        )
        payload = extract_payload(response)

    assert isinstance(payload, dict)
    assert payload["success"] is False
    assert "EMBEDDING_PROVIDER" in payload["error"]


@pytest.mark.asyncio
async def test_run_search_vector_search_finds_seeded_document(
    seeded_search_vector_index: dict[str, Any],
) -> None:
    """Vector-only (no scalar_query) search against the seeded Search index
    must return the seeded document."""
    fixture = seeded_search_vector_index
    assert fixture["indexed"], (
        f"Search vector index never picked up the seeded document within "
        f"{INDEX_READY_TIMEOUT}s. Last error: {fixture['index_error']}"
    )

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_search_vector_search",
            arguments={
                "index_name": fixture["index_name"],
                "bucket_name": fixture["bucket_name"],
                "scope_name": fixture["scope_name"],
                "vector_field": fixture["vector_field"],
                "vector_query_text": fixture["marker"],
                "limit": 1,
            },
        )
        payload = _assert_tool_succeeded(extract_payload(response))

    assert payload["is_hybrid"] is False
    assert payload["total_hits"] == 1
    assert payload["hits"][0]["id"] == fixture["doc_id"]


@pytest.mark.asyncio
async def test_run_search_vector_search_hybrid_with_scalar_query(
    seeded_search_vector_index: dict[str, Any],
) -> None:
    """Passing scalar_query alongside the embedded vector query must set
    is_hybrid=True and still find the seeded document (its `name` field
    contains the marker, matched by both the vector and scalar halves)."""
    fixture = seeded_search_vector_index
    assert fixture["indexed"], (
        f"Search vector index never picked up the seeded document within "
        f"{INDEX_READY_TIMEOUT}s. Last error: {fixture['index_error']}"
    )

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "run_search_vector_search",
            arguments={
                "index_name": fixture["index_name"],
                "bucket_name": fixture["bucket_name"],
                "scope_name": fixture["scope_name"],
                "vector_field": fixture["vector_field"],
                "vector_query_text": fixture["marker"],
                "scalar_query": {"match": fixture["marker"], "field": "name"},
                "limit": 1,
            },
        )
        payload = _assert_tool_succeeded(extract_payload(response))

    assert payload["is_hybrid"] is True
    assert payload["total_hits"] == 1
    assert payload["hits"][0]["id"] == fixture["doc_id"]
