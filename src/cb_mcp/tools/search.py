"""
Tools for Full Text Search (FTS / Search service) index discovery and querying.

This module covers listing/looking-up Search indexes and executing/explaining FTS
queries. Both scope-level (scoped) indexes and cluster-level ("legacy") indexes are
supported. Vector search is explicitly out of scope here — a raw FTS query body (match,
match_phrase, term, conjuncts, disjuncts, geo, date/numeric range, query_string, ...) is
supported via a raw-JSON passthrough, but vector queries require the SDK's SearchRequest +
VectorSearch combination, which these tools do not build.

Error handling: these tools only let an exception propagate when the cluster itself
can't be reached (get_cluster_connection). Everything else — bad input combinations,
an index that doesn't exist, a malformed query — is reported back as
{"error": "<what's wrong>"} instead of raising, so the caller (an LLM) sees an
actionable message rather than a bare stack trace.
"""

import logging
from typing import Any

from couchbase.options import SearchOptions
from couchbase.search import RawQuery, SearchRequest
from fastmcp import Context

from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.search")


def list_search_indexes(
    ctx: Context,
    index_name: str | None = None,
    bucket_name: str | None = None,
    scope_name: str | None = None,
) -> list[dict[str, Any]]:
    """List Search (FTS) indexes, or fetch one index's full definition.

    Two modes, selected by whether index_name is given. Always returns a list.

    Summary mode (index_name omitted) — filtering behavior:
    - No bucket_name/scope_name: lists cluster-level ("legacy") Search indexes only — the
      original, pre-scoped FTS index model, defined at the cluster level.
    - bucket_name only: lists scope-level (scoped) Search indexes across every scope in
      that bucket.
    - bucket_name and scope_name: lists scope-level Search indexes in that one scope only.
    - scope_name without bucket_name is invalid — returns an error entry explaining that
      bucket_name is required.
    Each entry contains: name, uuid, source_name, source_type, idx_type, bucket, scope.
    bucket/scope are None for cluster-level (legacy) entries.

    Full-definition mode (index_name given) — returns a list containing at most one entry:
    the full definition of that single index (name, source_type, idx_type, source_name,
    uuid, params [mapping/analyzer configuration], source_uuid, source_params, and
    plan_params [num replicas/partitions], plus the bucket/scope it was looked up in). Pass
    both bucket_name and scope_name together to look up a scope-level (scoped) index in
    that scope, or omit both to look up a cluster-level ("legacy") index. Passing only one
    of the two is invalid — Couchbase allows the same index name to exist in different
    scopes, so the location must be stated explicitly rather than guessed. If no index with
    this name exists at the given location, returns [{"error": ...}] — confirm the exact
    name and location first by calling this tool without index_name.
    """
    if index_name is not None:
        return _get_search_index_definition(ctx, index_name, bucket_name, scope_name)

    if scope_name and not bucket_name:
        return [{"error": "bucket_name is required when filtering by scope_name"}]

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
        return [{"error": str(e)}]


def _get_search_index_definition(
    ctx: Context,
    index_name: str,
    bucket_name: str | None,
    scope_name: str | None,
) -> list[dict[str, Any]]:
    """Full-definition lookup for a single named Search index — the
    index_name branch of list_search_indexes, split out only to keep that
    public function's branch count manageable."""
    if (bucket_name is None) != (scope_name is None):
        return [
            {
                "error": "bucket_name and scope_name must be provided together, or omitted together"
            }
        ]

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
        return [
            {
                "name": index.name,
                "source_type": index.source_type,
                "idx_type": index.idx_type,
                "source_name": index.source_name,
                "uuid": index.uuid,
                "params": index.params,
                "source_uuid": index.source_uuid,
                "source_params": index.source_params,
                "plan_params": index.plan_params,
                "bucket": bucket_name,
                "scope": scope_name,
            }
        ]
    except Exception as e:
        logger.error(f"Error fetching Search index {index_name!r}: {e}", exc_info=True)
        return [{"error": str(e), "index_name": index_name}]


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
    Confirm the index's exact name/location first with list_search_indexes.

    explain: if True, fetches the execution plan instead of normal results. The Search
    service exposes the query plan per matched document, not as a separate plan-only/
    dry-run call — so explain=True still executes the query (with explain enabled) and
    returns the explanation for each returned hit. limit defaults to 1 in this mode to
    keep it cheap (pass a larger limit to see the plan for more matched documents), and
    skip/fields/sort/facets/highlight_fields/disable_scoring/raw are ignored — they only
    apply when explain=False.

    limit/skip/fields/sort/facets/highlight_fields/disable_scoring map to the equivalent
    Search options. raw is a passthrough dict for any other SearchOptions field not
    exposed directly (e.g. highlight_style, consistent_with).

    Returns {"index_name", "explain", "total_hits", "hits", "facets", "metadata":
    {"errors","metrics"}} on success, or {"error": ...} if the input is invalid or the
    query fails (e.g. bad query syntax, index not found). hits entries are
    {"id","score","fields","locations","fragments"} when explain=False, or
    {"id","score","explanation"} when explain=True (facets is empty in that case).
    """
    if (bucket_name is None) != (scope_name is None):
        return {
            "error": "bucket_name and scope_name must be provided together, or omitted together"
        }

    cluster = get_cluster_connection(ctx)

    try:
        if explain:
            options = SearchOptions(
                explain=True, limit=limit if limit is not None else 1
            )
        else:
            options = SearchOptions(
                limit=limit,
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
                    "locations": row.locations,
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
        return {
            "index_name": index_name,
            "explain": explain,
            "total_hits": len(hits),
            "hits": hits,
            "facets": facets_result,
            "metadata": {
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
        }
    except Exception as e:
        logger.error(
            f"Error running Search query on {index_name!r}: {e}", exc_info=True
        )
        return {"error": str(e), "index_name": index_name}
