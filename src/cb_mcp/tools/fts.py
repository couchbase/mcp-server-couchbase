"""
Tools for Full Text Search (FTS / Search service) index discovery and querying.

This module covers listing Search indexes, reading a single index's definition, and
executing/explaining FTS queries. Both scope-level (scoped) indexes and cluster-level
("legacy") indexes are supported. Vector search is explicitly out of scope here — a raw
FTS query body (match, match_phrase, term, conjuncts, disjuncts, geo, date/numeric range,
query_string, ...) is supported via a raw-JSON passthrough, but vector queries require the
SDK's SearchRequest + VectorSearch combination, which these tools do not build.

Error handling: these tools only let an exception propagate when the cluster itself
can't be reached (get_cluster_connection). Everything else — bad input combinations,
an index that doesn't exist, a malformed query — is reported back via the
tool_success/tool_error envelopes from utils.responses instead of raising, so the
caller (an LLM) sees an actionable message rather than a bare stack trace.
"""

import logging
from typing import Any

from couchbase.options import SearchOptions
from couchbase.search import RawQuery, SearchRequest
from fastmcp import Context

from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection
from ..utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.fts")


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
    against an unmapped field matches nothing rather than erroring.

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
            options = SearchOptions(explain=True, limit=applied_limit)
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
                raw=raw,
            )
        request = SearchRequest.create(RawQuery(query))
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
