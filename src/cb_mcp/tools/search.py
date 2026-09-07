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
    bucket/scope are None for cluster-level (legacy) entries. This is a summary view —
    call get_search_index_definition with the same bucket_name/scope_name pairing to get
    the full index definition (mappings, analyzers, plan params) for a specific index.
    """
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


def get_search_index_definition(
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

    Returns the index's name, source_type, idx_type, source_name, uuid, params (mapping/
    analyzer configuration), source_uuid, source_params, and plan_params (num replicas/
    partitions), plus the bucket/scope it was looked up in. If no index with this name
    exists at the given location, returns {"error": ...} — confirm the exact name and
    location first with list_search_indexes.
    """
    if (bucket_name is None) != (scope_name is None):
        return {
            "error": "bucket_name and scope_name must be provided together, or omitted together"
        }

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
        return {
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
    except Exception as e:
        logger.error(f"Error fetching Search index {index_name!r}: {e}", exc_info=True)
        return {"error": str(e), "index_name": index_name}


def run_fts_query(
    ctx: Context,
    index_name: str,
    query: dict[str, Any],
    bucket_name: str | None = None,
    scope_name: str | None = None,
    limit: int | None = None,
    skip: int | None = None,
    fields: list[str] | None = None,
    sort: list[Any] | None = None,
    facets: dict[str, Any] | None = None,
    highlight_fields: list[str] | None = None,
    disable_scoring: bool = False,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an FTS (Search) query against a Search index.

    query is the raw FTS query JSON body, passed through unvalidated to the Search
    service — this supports every non-vector FTS query type, e.g.:
    - {"match": "ale", "field": "type"}
    - {"conjuncts": [{"match": "ale"}, {"term": "beer", "field": "type"}]}
    - {"query": "type:beer +name:ale"}  (query_string)
    Vector search is NOT supported by this tool (separate feature).

    Pass both bucket_name and scope_name together to query a scope-level (scoped) index,
    or omit both to query a cluster-level ("legacy") index. Passing only one is invalid.
    Confirm the index's exact name/location first with list_search_indexes.

    limit/skip/fields/sort/facets/highlight_fields/disable_scoring map to the equivalent
    Search options. raw is a passthrough dict for any other SearchOptions field not
    exposed directly (e.g. highlight_style, consistent_with).

    Returns {"index_name", "total_hits", "hits": [{"id","score","fields","locations",
    "fragments"}], "facets", "metadata": {"errors","metrics"}} on success, or
    {"error": ...} if the input is invalid or the query fails (e.g. bad query syntax,
    index not found).
    """
    if (bucket_name is None) != (scope_name is None):
        return {
            "error": "bucket_name and scope_name must be provided together, or omitted together"
        }

    cluster = get_cluster_connection(ctx)

    try:
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
        metadata = result.metadata()
        metrics = metadata.metrics()
        logger.info(f"run_fts_query on {index_name!r} returned {len(hits)} hit(s)")
        return {
            "index_name": index_name,
            "total_hits": len(hits),
            "hits": hits,
            "facets": result.facets(),
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


def explain_fts_query(
    ctx: Context,
    index_name: str,
    query: dict[str, Any],
    bucket_name: str | None = None,
    scope_name: str | None = None,
    limit: int = 1,
) -> dict[str, Any]:
    """Fetch the execution plan for an FTS (Search) query.

    The Search service exposes the query plan per matched document, not as a separate
    plan-only/dry-run call — so this tool actually executes the query (with explain
    enabled) and returns the explanation for each returned hit. limit defaults to 1 to
    keep this cheap; pass a larger limit to see the plan for more matched documents.

    query, bucket_name, and scope_name follow the same rules as run_fts_query: query is
    the raw FTS query JSON body (non-vector), and bucket_name/scope_name must be provided
    together (scope-level index) or both omitted (cluster-level/legacy index).

    Returns {"index_name", "query_explained": True, "limit", "explanations": [{"id",
    "score", "explanation"}], "metadata": {"errors","metrics"}} on success, or
    {"error": ...} if the input is invalid or the query fails.
    """
    if (bucket_name is None) != (scope_name is None):
        return {
            "error": "bucket_name and scope_name must be provided together, or omitted together"
        }

    cluster = get_cluster_connection(ctx)

    try:
        options = SearchOptions(explain=True, limit=limit)
        request = SearchRequest.create(RawQuery(query))
        if bucket_name and scope_name:
            bucket = connect_to_bucket(cluster, bucket_name)
            result = bucket.scope(scope_name).search(index_name, request, options)
        else:
            result = cluster.search(index_name, request, options)

        explanations = [
            {"id": row.id, "score": row.score, "explanation": row.explanation}
            for row in result.rows()
        ]
        metadata = result.metadata()
        metrics = metadata.metrics()
        logger.info(
            f"explain_fts_query on {index_name!r} explained {len(explanations)} hit(s)"
        )
        return {
            "index_name": index_name,
            "query_explained": True,
            "limit": limit,
            "explanations": explanations,
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
            f"Error explaining Search query on {index_name!r}: {e}", exc_info=True
        )
        return {"error": str(e), "index_name": index_name}
