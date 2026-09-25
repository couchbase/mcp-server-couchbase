"""
Tools for Full Text Search (FTS / Search service) index discovery, querying, and
management.

This module covers listing Search indexes, reading a single index's definition,
executing/explaining FTS queries, and creating/updating (upsert_fts_index) or
dropping (drop_fts_index) an index definition. Both scope-level (scoped) indexes and
cluster-level ("legacy") indexes are supported. Vector search is explicitly out of
scope here — a raw FTS query body (match, match_phrase, term, conjuncts, disjuncts,
geo, date/numeric range, query_string, ...) is supported via a raw-JSON passthrough,
but vector queries require the SDK's SearchRequest + VectorSearch combination, which
these tools do not build.

upsert_fts_index and drop_fts_index are write operations — not loaded when
READ_ONLY_MODE is True — and require the couchbase-mcp:write OAuth scope.

Error handling: these tools only let an exception propagate when the cluster itself
can't be reached (get_cluster_connection). Everything else — bad input combinations,
an index that doesn't exist, a malformed query — is reported back via the
tool_success/tool_error envelopes from utils.responses instead of raising, so the
caller (an LLM) sees an actionable message rather than a bare stack trace.
"""

import logging
from typing import Any

from couchbase.management.search import SearchIndex
from couchbase.options import SearchOptions
from couchbase.search import MatchNoneQuery, SearchRequest
from fastmcp import Context

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ...utils.operational.connection import connect_to_bucket
from ...utils.operational.context import get_cluster_connection
from ...utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.fts")


def list_fts_indexes(
    ctx: Context,
    bucket_name: str | None = None,
    scope_name: str | None = None,
) -> list[dict[str, Any]]:
    """List Search (FTS) indexes, optionally filtered by bucket and scope.

    Filtering behavior:
    - No filters: lists cluster-level ("legacy") Search indexes only — the original,
      pre-scoped FTS index model, defined at the cluster level.
    - bucket_name only: lists scope-level (scoped) Search indexes across every scope in
      that bucket.
    - bucket_name and scope_name: lists scope-level Search indexes in that one scope only.
    - scope_name without bucket_name is invalid — returns an error entry explaining that
      bucket_name is required.

    Each entry contains: name, uuid, source_name, source_type, idx_type, bucket, scope.
    bucket/scope are None for cluster-level (legacy) entries. This is a summary view — to
    get one index's full definition (mappings, analyzers, plan params), call
    get_fts_index_definition with that entry's name and its own bucket/scope values
    (both, for a scoped entry; neither, when they're None). Don't reuse this call's
    filters: listing by bucket_name alone spans several scopes, and
    get_fts_index_definition rejects a bucket without a scope.
    """
    if scope_name and not bucket_name:
        return [tool_error("bucket_name is required when filtering by scope_name")]

    cluster = get_cluster_connection(ctx)

    try:
        if not bucket_name:
            logger.info("Listing cluster-level (legacy) Search indexes")
            indexes = cluster.search_indexes().get_all_indexes()
            return [
                {
                    "name": idx.name,
                    "uuid": idx.uuid,
                    "source_name": idx.source_name,
                    "source_type": idx.source_type,
                    "idx_type": idx.idx_type,
                    "bucket": None,
                    "scope": None,
                }
                for idx in indexes
            ]

        bucket = connect_to_bucket(cluster, bucket_name)
        if scope_name:
            scope_names = [scope_name]
        else:
            scope_names = [s.name for s in bucket.collections().get_all_scopes()]

        logger.info(
            f"Listing scope-level Search indexes for bucket={bucket_name}, "
            f"scopes={scope_names}"
        )
        results: list[dict[str, Any]] = []
        for name in scope_names:
            scope_indexes = bucket.scope(name).search_indexes().get_all_indexes()
            for idx in scope_indexes:
                results.append(
                    {
                        "name": idx.name,
                        "uuid": idx.uuid,
                        "source_name": idx.source_name,
                        "source_type": idx.source_type,
                        "idx_type": idx.idx_type,
                        "bucket": bucket_name,
                        "scope": name,
                    }
                )
        logger.info(f"Found {len(results)} Search index(es)")
        return results
    except Exception as e:
        logger.error(f"Error listing Search indexes: {e}", exc_info=True)
        return [tool_error(e)]


def get_fts_index_definition(
    ctx: Context,
    index_name: str,
    bucket_name: str | None = None,
    scope_name: str | None = None,
) -> dict[str, Any]:
    """Get the full definition of a single Search (FTS) index.

    Pass both bucket_name and scope_name together to look up a scope-level (scoped) index
    in that scope, or omit both to look up a cluster-level ("legacy") index. Passing only
    one of the two is invalid — Couchbase allows the same index name to exist in different
    scopes, so the location must be stated explicitly rather than guessed.

    Returns {"success": True, "name", "source_type", "idx_type", "source_name", "uuid",
    "params" (mapping/analyzer configuration), "source_uuid", "source_params",
    "plan_params" (num replicas/partitions), "bucket", "scope"}. If no index with this
    name exists at the given location, returns {"success": False, "error": ...} —
    confirm the exact name and location first with list_fts_indexes.
    """
    if (bucket_name is None) != (scope_name is None):
        return tool_error(
            "bucket_name and scope_name must be provided together, or omitted together"
        )

    cluster = get_cluster_connection(ctx)

    try:
        if bucket_name and scope_name:
            logger.debug(
                f"Fetching Search index {index_name!r} in {bucket_name}.{scope_name}"
            )
            bucket = connect_to_bucket(cluster, bucket_name)
            index = bucket.scope(scope_name).search_indexes().get_index(index_name)
        else:
            logger.debug(f"Fetching cluster-level Search index {index_name!r}")
            index = cluster.search_indexes().get_index(index_name)

        logger.info(f"Fetched Search index {index_name!r}")
        return tool_success(
            name=index.name,
            source_type=index.source_type,
            idx_type=index.idx_type,
            source_name=index.source_name,
            uuid=index.uuid,
            params=index.params,
            source_uuid=index.source_uuid,
            source_params=index.source_params,
            plan_params=index.plan_params,
            bucket=bucket_name,
            scope=scope_name,
        )
    except Exception as e:
        logger.error(f"Error fetching Search index {index_name!r}: {e}", exc_info=True)
        return tool_error(e, index_name=index_name)


def run_fts_query(
    ctx: Context,
    index_name: str,
    query: dict[str, Any],
    bucket_name: str | None = None,
    scope_name: str | None = None,
    explain: bool = False,
    limit: int | None = None,
    skip: int | None = None,
    fields: list[str] | None = None,
    sort: list[Any] | None = None,
    facets: dict[str, Any] | None = None,
    highlight_fields: list[str] | None = None,
    disable_scoring: bool = False,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an FTS (Search) query against a Search index, or fetch its execution plan.

    Use this instead of run_sql_plus_plus_query's SEARCH() function when you need
    relevance-scored, fuzzy, or linguistic full-text matching (scoring, highlighting,
    faceting, fuzzy/phrase/wildcard matching) — SQL++ can invoke FTS predicates too, but
    this tool gives clearer tracing of what data was searched and keeps FTS-specific
    result shape (score, fragments, facets) out of general query results.

    query is the raw FTS query JSON body, passed through unvalidated to the Search
    service — this supports every non-vector FTS query type, e.g.:
    - {"match": "ale", "field": "type"}
    - {"conjuncts": [{"match": "ale"}, {"term": "beer", "field": "type"}]}
    - {"query": "type:beer +name:ale"}  (query_string)
    Vector search is NOT supported by this tool (separate feature).

    Pass both bucket_name and scope_name together to query a scope-level (scoped) index,
    or omit both to query a cluster-level ("legacy") index. Passing only one is invalid.
    Confirm the index's exact name/location first with list_fts_indexes. Don't guess which
    fields a query body can target — if you don't already know what the index maps and how
    those fields are analyzed, call get_fts_index_definition first; a field-scoped query
    against an unmapped field matches nothing rather than erroring. Note this only works for
    indexes with explicit static field mappings — a dynamically mapped index (dynamic: true)
    has no fixed field list to inspect, since it indexes whatever fields appear in each
    document; for those, look at actual document contents instead to know what's queryable.

    explain: if True, fetches the execution plan instead of normal results. The Search
    service exposes the query plan per matched document, not as a separate plan-only/
    dry-run call — so explain=True still executes the query (with explain enabled) and
    returns the explanation for each returned hit. limit defaults to 1 in this mode to
    keep it cheap (pass a larger limit to see the plan for more matched documents), and
    skip/fields/sort/facets/highlight_fields/disable_scoring/raw are ignored — they only
    apply when explain=False.

    limit/skip/fields/sort/facets/highlight_fields/disable_scoring map to the equivalent
    Search options. limit defaults to 10 when explain=False (matching the other query
    tools in this server) and to 1 when explain=True. raw is a passthrough dict for any
    other SearchOptions field not exposed directly (e.g. highlight_style, consistent_with).

    Returns {"success": True, "index_name", "explain", "limit" (the limit actually
    applied, since it defaults differently depending on explain), "total_hits",
    "hits", "facets", "metadata": {"errors","metrics"}} on success, or
    {"success": False, "error": ...} if the input is invalid or the query fails (e.g. bad
    query syntax, index not found). total_hits counts the hits actually returned, so it is
    capped by limit — for how many documents matched overall, read
    metadata.metrics.total_rows instead, and don't report total_hits as the size of the
    match set. hits entries are {"id","score","fields","fragments"} when explain=False, or
    {"id","score","explanation"} when explain=True (facets is empty in that case).
    """
    if (bucket_name is None) != (scope_name is None):
        return tool_error(
            "bucket_name and scope_name must be provided together, or omitted together"
        )

    cluster = get_cluster_connection(ctx)

    try:
        if explain:
            applied_limit = limit if limit is not None else 1
            options = SearchOptions(
                explain=True, limit=applied_limit, raw={"query": query}
            )
        else:
            applied_limit = limit if limit is not None else 10
            options = SearchOptions(
                limit=applied_limit,
                skip=skip,
                fields=fields,
                sort=sort,
                facets=facets,
                highlight_fields=highlight_fields,
                disable_scoring=disable_scoring,
                raw={**(raw or {}), "query": query},
            )
        request = SearchRequest.create(MatchNoneQuery())
        if bucket_name and scope_name:
            bucket = connect_to_bucket(cluster, bucket_name)
            result = bucket.scope(scope_name).search(index_name, request, options)
        else:
            result = cluster.search(index_name, request, options)

        if explain:
            hits = [
                {"id": row.id, "score": row.score, "explanation": row.explanation}
                for row in result.rows()
            ]
            facets_result: dict[str, Any] = {}
        else:
            hits = [
                {
                    "id": row.id,
                    "score": row.score,
                    "fields": row.fields,
                    "fragments": row.fragments,
                }
                for row in result.rows()
            ]
            facets_result = result.facets()

        metadata = result.metadata()
        metrics = metadata.metrics()
        logger.info(
            f"run_fts_query on {index_name!r} (explain={explain}) returned "
            f"{len(hits)} hit(s)"
        )
        return tool_success(
            index_name=index_name,
            explain=explain,
            limit=applied_limit,
            total_hits=len(hits),
            hits=hits,
            facets=facets_result,
            metadata={
                "errors": metadata.errors(),
                "metrics": None
                if metrics is None
                else {
                    "success_partition_count": metrics.success_partition_count(),
                    "error_partition_count": metrics.error_partition_count(),
                    "total_partition_count": metrics.total_partition_count(),
                    "max_score": metrics.max_score(),
                    "total_rows": metrics.total_rows(),
                    "took": str(metrics.took()),
                },
            },
        )
    except Exception as e:
        logger.error(
            f"Error running Search query on {index_name!r}: {e}", exc_info=True
        )
        return tool_error(e, index_name=index_name)


def upsert_fts_index(
    ctx: Context,
    index_name: str,
    source_name: str,
    params: dict[str, Any] | None = None,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    source_type: str = "couchbase",
    idx_type: str = "fulltext-index",
    plan_params: dict[str, Any] | None = None,
    source_params: dict[str, Any] | None = None,
    source_uuid: str | None = None,
    uuid: str | None = None,
) -> dict[str, Any]:
    """Create or update a Search (FTS) index definition.

    This is an upsert: if no index named index_name exists at the given location, it
    is created; otherwise its definition is fully REPLACED (not merged) with what you
    pass here. Updating an existing index triggers a rebuild, which can disrupt search
    availability while it reindexes. The recommended workflow for modifying an
    existing index is: call get_fts_index_definition first, change only the fields you
    need, and pass everything (including its uuid) back to this tool — passing uuid
    for an update tells the Search service which revision you started from, so it can
    detect and reject a conflicting concurrent modification rather than silently
    overwriting it. Leave uuid unset when creating a brand new index.

    Pass both bucket_name and scope_name together to create/update a scope-level
    (scoped) index, or omit both for a cluster-level ("legacy") index. Passing only one
    is invalid.

    source_name is the bucket whose documents this index indexes over. It is
    independent of bucket_name/scope_name (which only say where the index *definition*
    is registered) — usually the same bucket, but always pass it explicitly, since a
    cluster-level index has no bucket_name to infer it from.

    params holds the mapping/analyzer configuration (as returned by
    get_fts_index_definition's "params" field) — e.g. {"doc_config": {"mode":
    "scope.collection.type_field"}, "mapping": {...}}. Omitting it produces the Search
    service's default mapping (typically dynamic — indexes every field it finds);
    pass an explicit params.mapping for anything more specific. plan_params controls
    index partitioning/replicas (e.g. numReplicas); source_params and source_uuid are
    passed through to the SDK as-is and rarely need to be set.

    Returns {"success": True, "index_name", "bucket", "scope"} on success, or
    {"success": False, "error": ...} on failure — e.g. an invalid mapping or a stale
    uuid on an update.
    """
    if (bucket_name is None) != (scope_name is None):
        return tool_error(
            "bucket_name and scope_name must be provided together, or omitted together"
        )

    cluster = get_cluster_connection(ctx)

    definition = SearchIndex(
        name=index_name,
        source_type=source_type,
        idx_type=idx_type,
        source_name=source_name,
        uuid=uuid,
        params=params or {},
        source_uuid=source_uuid,
        source_params=source_params or {},
        plan_params=plan_params or {},
    )

    try:
        if bucket_name and scope_name:
            logger.debug(
                f"Upserting Search index {index_name!r} in {bucket_name}.{scope_name}"
            )
            bucket = connect_to_bucket(cluster, bucket_name)
            bucket.scope(scope_name).search_indexes().upsert_index(definition)
        else:
            logger.debug(f"Upserting cluster-level Search index {index_name!r}")
            cluster.search_indexes().upsert_index(definition)

        logger.info(f"Upserted Search index {index_name!r}")
        return tool_success(index_name=index_name, bucket=bucket_name, scope=scope_name)
    except Exception as e:
        logger.error(f"Error upserting Search index {index_name!r}: {e}", exc_info=True)
        return tool_error(e, index_name=index_name)


def drop_fts_index(
    ctx: Context,
    index_name: str,
    bucket_name: str | None = None,
    scope_name: str | None = None,
) -> dict[str, Any]:
    """Drop an existing Search (FTS) index.

    Works with both scope-level (scoped) indexes and cluster-level ("legacy")
    indexes — the pre-scoped FTS index model, still supported alongside scoped
    indexes.

    This permanently removes the index and cannot be undone — queries and
    applications that relied on it will fail until it is recreated (and rebuilt).
    Prefer confirming the index's exact name and location with list_fts_indexes first.

    Pass both bucket_name and scope_name together to drop a scope-level (scoped)
    index, or omit both to drop a cluster-level ("legacy") index. Passing only one is
    invalid.

    Returns {"success": True, "index_name", "bucket", "scope"} on success, or
    {"success": False, "error": ...} on failure — e.g. no index with this name exists
    at the given location.
    """
    if (bucket_name is None) != (scope_name is None):
        return tool_error(
            "bucket_name and scope_name must be provided together, or omitted together"
        )

    cluster = get_cluster_connection(ctx)

    try:
        if bucket_name and scope_name:
            logger.debug(
                f"Dropping Search index {index_name!r} in {bucket_name}.{scope_name}"
            )
            bucket = connect_to_bucket(cluster, bucket_name)
            bucket.scope(scope_name).search_indexes().drop_index(index_name)
        else:
            logger.debug(f"Dropping cluster-level Search index {index_name!r}")
            cluster.search_indexes().drop_index(index_name)

        logger.info(f"Dropped Search index {index_name!r}")
        return tool_success(index_name=index_name, bucket=bucket_name, scope=scope_name)
    except Exception as e:
        logger.error(f"Error dropping Search index {index_name!r}: {e}", exc_info=True)
        return tool_error(e, index_name=index_name)
