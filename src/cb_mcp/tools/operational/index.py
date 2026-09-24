"""
Tools for index operations.

This module contains tools for listing and managing indexes in the Couchbase cluster and getting index recommendations using the Couchbase Index Advisor.
"""

import logging
from typing import Any

from couchbase.management.options import CreateQueryIndexOptions, DropQueryIndexOptions
from fastmcp import Context

from ...servers.operational.constants import (
    OPERATIONAL_LOGGER_NAMESPACE,
    QUERY_SERVICE_LIST_INDEXES_MIN_MAJOR_VERSION,
)
from ...utils.config import get_settings
from ...utils.context import get_cluster_connection
from ...utils.operational.connection import connect_to_bucket, format_keyspace
from ...utils.operational.connection_string import validate_connection_settings
from ...utils.operational.index_utils import (
    fetch_index_stats_from_rest_api,
    fetch_indexes_from_rest_api,
    parse_index_stats_key,
    process_index_data_from_query,
    process_index_data_from_rest_api,
    resolve_cluster_major_version,
    validate_filter_params,
)
from ...utils.responses import tool_error, tool_success
from .query import run_cluster_query, run_sql_plus_plus_query

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.index")


def get_index_advisor_recommendations(
    ctx: Context, bucket_name: str, scope_name: str, query: str
) -> dict[str, Any]:
    """Get index recommendations from Couchbase Index Advisor for a given SQL++ query.

    The Index Advisor analyzes the query and provides recommendations for optimal indexes.
    This tool works with SELECT, UPDATE, DELETE, or MERGE queries.
    The queries will be run on the specified scope in the specified bucket.

    Returns a dictionary with:
    - current_used_indexes: Array of currently used indexes (if any)
    - recommended_indexes: Array of recommended secondary indexes (if any)
    - recommended_covering_indexes: Array of recommended covering indexes (if any)

    Each index object contains:
    - index: The CREATE INDEX SQL++ command
    - statements: Array of statement objects with the query and run count
    """
    try:
        # Build the ADVISOR query using a named parameter.
        advisor_query = "SELECT ADVISOR($advise_statement) AS advisor_result"

        logger.info("Running Index Advisor for the provided query")

        # Execute in scope context so the advised query can use bare collection
        # names. ADVISOR is a read-only SELECT, so the read-only-mode write guard
        # in run_sql_plus_plus_query is a no-op here.
        advisor_results = run_sql_plus_plus_query(
            ctx,
            bucket_name,
            scope_name,
            advisor_query,
            named_parameters={"advise_statement": query},
        )

        if not advisor_results:
            return {
                "message": "No recommendations available",
                "current_used_indexes": [],
                "recommended_indexes": [],
                "recommended_covering_indexes": [],
            }

        # The result is wrapped in advisor_result key
        advisor_data = advisor_results[0].get("advisor_result", {})

        # Extract the relevant fields with defaults
        response = {
            "current_used_indexes": advisor_data.get("current_used_indexes", []),
            "recommended_indexes": advisor_data.get("recommended_indexes", []),
            "recommended_covering_indexes": advisor_data.get(
                "recommended_covering_indexes", []
            ),
        }

        # Add summary information for better user experience
        response["summary"] = {
            "current_indexes_count": len(response["current_used_indexes"]),
            "recommended_indexes_count": len(response["recommended_indexes"]),
            "recommended_covering_indexes_count": len(
                response["recommended_covering_indexes"]
            ),
            "has_recommendations": bool(
                response["recommended_indexes"]
                or response["recommended_covering_indexes"]
            ),
        }

        logger.info(
            f"Index Advisor completed. Found {response['summary']['recommended_indexes_count']} recommended indexes"
        )

        return response

    except Exception as e:
        logger.error(f"Error running Index Advisor: {e!s}", exc_info=True)
        raise


def fetch_indexes_via_query_service(
    ctx: Context,
    bucket_name: str | None,
    scope_name: str | None,
    collection_name: str | None,
    index_name: str | None,
    return_raw_index_stats: bool = False,
) -> list[dict[str, Any]]:
    """Fetch indexes from ``system:indexes`` via the query service.

    Uses a LET clause to normalize legacy and modern index shapes so filters
    apply symmetrically. When ``return_raw_index_stats`` is True, returns raw
    rows with no injected bucket/scope/collection fields.

    Returns:
        List of dict rows from ``system:indexes``.
    """
    # Always present — guards future Couchbase pool/namespace additions and
    # restricts to GSI indexes.
    clauses: list[str] = ["s.namespace_id = 'default'", "s.`using` = 'gsi'"]
    params: dict[str, Any] = {}

    if bucket_name:
        clauses.append("bid = $bucket_id")
        params["bucket_id"] = bucket_name
    if scope_name:
        clauses.append("sid = $scope_id")
        params["scope_id"] = scope_name
    if collection_name:
        clauses.append("kid = $keyspace_id")
        params["keyspace_id"] = collection_name
    if index_name:
        clauses.append("s.name = $index_name")
        params["index_name"] = index_name

    let_clause = (
        "LET bid = IFMISSING(s.bucket_id, s.keyspace_id), "
        "sid = IFMISSING(s.scope_id, '_default'), "
        "kid = NVL2(s.bucket_id, s.keyspace_id, '_default')"
    )
    if return_raw_index_stats:
        select_clause = "SELECT RAW s"
    else:
        select_clause = (
            "SELECT s.*, bid AS `bucket`, sid AS `scope`, kid AS `collection`"
        )

    query = (
        f"{select_clause} FROM system:indexes AS s {let_clause} "
        f"WHERE {' AND '.join(clauses)}"
    )
    logger.debug("Running list_indexes query")

    rows = run_cluster_query(ctx, query, named_parameters=params)
    return [row for row in rows if isinstance(row, dict)]


def list_indexes(
    ctx: Context,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
    index_name: str | None = None,
    return_raw_index_stats: bool = False,
) -> list[dict[str, Any]]:
    """List indexes in the cluster with optional filtering by bucket, scope, collection, and index name.

    Filters must be provided hierarchically: scope requires bucket, collection requires both, index requires all three.
    Set ``return_raw_index_stats=True`` to get the unprocessed source row for each index.

    Each result contains: name, definition (CREATE INDEX statement), status, isPrimary, bucket, scope, collection, lastScanTime.
    If a required field is missing, the entry contains warning and raw_index_stats instead.

    Source depends on cluster version: v8+ queries ``system:indexes`` via the
    query service (RBAC-scoped — the connected user sees only indexes on
    keyspaces they can access); older clusters fall back to the admin-level
    Index Service REST API ``/getIndexStatus``.
    """
    try:
        # Validate parameters
        validate_filter_params(bucket_name, scope_name, collection_name, index_name)

        # Get connection settings
        settings = get_settings(ctx)

        # Decide which path to use based on cluster version (via SDK).
        cluster = get_cluster_connection(ctx)
        major_version = resolve_cluster_major_version(cluster)

        if major_version >= QUERY_SERVICE_LIST_INDEXES_MIN_MAJOR_VERSION:
            logger.info(
                f"Fetching indexes via query service (system:indexes) for "
                f"bucket={bucket_name}, scope={scope_name}, "
                f"collection={collection_name}, index={index_name}"
            )
            raw_indexes = fetch_indexes_via_query_service(
                ctx,
                bucket_name=bucket_name,
                scope_name=scope_name,
                collection_name=collection_name,
                index_name=index_name,
                return_raw_index_stats=return_raw_index_stats,
            )
            if return_raw_index_stats:
                return raw_indexes
            indexes = [process_index_data_from_query(idx) for idx in raw_indexes]
            logger.info(f"Found {len(indexes)} indexes via query service")
            return indexes

        # Fallback / pre-8.x path: Index Service REST API.
        # This path authenticates directly against the REST endpoint, so
        # username/password (and connection_string) must be present.
        validate_connection_settings(settings)

        logger.info(
            f"Fetching indexes from Index Service REST API for "
            f"bucket={bucket_name}, scope={scope_name}, "
            f"collection={collection_name}, index={index_name}"
        )
        raw_indexes = fetch_indexes_from_rest_api(
            settings["connection_string"],
            settings["username"],
            settings["password"],
            bucket_name=bucket_name,
            scope_name=scope_name,
            collection_name=collection_name,
            index_name=index_name,
            ca_cert_path=settings.get("ca_cert_path"),
        )

        # Process and format the results
        if return_raw_index_stats:
            return raw_indexes
        indexes = [process_index_data_from_rest_api(idx) for idx in raw_indexes]

        logger.info(f"Found {len(indexes)} indexes from REST API")
        return indexes

    except Exception as e:
        logger.error(f"Error listing indexes: {e}", exc_info=True)
        raise


def get_index_stats(
    ctx: Context,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
    index_name: str | None = None,
    skip_empty: bool = False,
) -> dict[str, Any]:
    """Get per-index statistics (size, fragmentation, scan traffic) from the Index Service.

    Names which index is responsible for disk or memory pressure — use it after
    get_cluster_metrics shows a node's index disk climbing.

    Filters are hierarchical: scope requires bucket, collection requires both,
    index requires all three. No filters returns every index in the cluster.

    Returns ``nodes`` (keyed by node, each with an ``indexer`` block and an
    ``indexes`` list of bucket/scope/collection/name + ``stats``),
    ``nodes_without_index`` (nodes that answered but do not hold the requested
    index — expected, since an index lives only where it was placed) and
    ``nodes_failed`` (nodes that could not be reached, so a partial answer is
    visible as one). ``indexer`` is None for filtered calls.

    Numbers are per node. A partitioned index appears under several nodes, each
    reporting only its own share: sum ``items_count``/``data_size``/``disk_size``
    across nodes, but not ``num_requests``, which every hosting node counts in
    full.

    Reading the stats: ``disk_size`` far above ``data_size`` (see
    ``frag_percent``) means space reclaimable by compaction; ``num_requests``
    with ``last_known_scan_time`` (Unix ns, 0 = never) identifies unused
    indexes; ``num_docs_pending`` is indexing lag. Counters reset when the
    indexer restarts, so a low count alone does not prove an index is unused.

    ``skip_empty=True`` drops zero-valued fields (~two thirds on an idle
    cluster) — leave it False when hunting unused indexes, since it also removes
    ``num_requests: 0``.
    """
    try:
        validate_filter_params(bucket_name, scope_name, collection_name, index_name)

        settings = get_settings(ctx)
        validate_connection_settings(settings)

        logger.info(
            f"Fetching index stats for bucket={bucket_name}, scope={scope_name}, "
            f"collection={collection_name}, index={index_name}"
        )
        per_node, not_hosted, failures = fetch_index_stats_from_rest_api(
            get_cluster_connection(ctx),
            settings["connection_string"],
            settings["username"],
            settings["password"],
            bucket_name=bucket_name,
            scope_name=scope_name,
            collection_name=collection_name,
            index_name=index_name,
            skip_empty=skip_empty,
            ca_cert_path=settings.get("ca_cert_path"),
        )

        nodes = {
            node: _shape_node_stats(raw_stats) for node, raw_stats in per_node.items()
        }
        total = sum(len(n["indexes"]) for n in nodes.values())
        logger.info(
            f"Found {total} index entry(s) across {len(nodes)} node(s); "
            f"{len(not_hosted)} node(s) without it, {len(failures)} unreachable"
        )
        return {
            "nodes": nodes,
            "nodes_without_index": not_hosted,
            "nodes_failed": failures,
        }

    except Exception as e:
        logger.error(f"Error getting index stats: {e}", exc_info=True)
        raise


def _shape_node_stats(raw_stats: dict[str, Any]) -> dict[str, Any]:
    """Split one node's raw ``/api/v1/stats`` body into indexer and index parts.

    Kept separate from :func:`get_index_stats` so the response layout can change
    without touching how the statistics are collected.
    """
    # "indexer" holds the node-level aggregates and sits alongside the per-index
    # entries, so it has to come out before the rest can be treated as indexes.
    # It is only sent for the unfiltered endpoint, so its absence is normal.
    stats = dict(raw_stats)
    indexer = stats.pop("indexer", None)

    return {
        "indexer": indexer,
        "indexes": [
            {**parse_index_stats_key(key), "stats": index_stats}
            for key, index_stats in stats.items()
        ],
    }


def create_index(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    index_name: str,
    keys: list[str],
    deferred: bool = True,
    condition: str | None = None,
    num_replicas: int | None = None,
    ignore_if_exists: bool = False,
) -> dict[str, Any]:
    """Create a non-vector (scalar) GSI secondary index on a collection.
    This is the preferred way to create a scalar index — use it instead of a raw CREATE
    INDEX statement via run_sql_plus_plus_query. It only creates scalar GSI indexes; it
    cannot create vector indexes.

    By default the index is created deferred (not built). The recommended next step is to
    call build_index to trigger the build, then list_indexes to confirm it reaches the
    'online' state.

    keys is the field(s)/expression(s) to index, e.g. ["email"] or ["type", "created_at DESC"].
    condition is an optional WHERE clause for a partial index, e.g. type = 'user'.
    num_replicas optionally sets the number of index replicas for availability and scaling.
    Pass ignore_if_exists=True to avoid an error when an index with this name already exists.

    Returns {"success": True, "index_name": ..., "deferred": ..., "keyspace": ...} on
    success; when deferred is True the result also includes a "next_step" hint noting the
    index must be built (via build_index) before it is usable. On failure returns
    {"success": False, "error": ...} — e.g. the index already exists (without
    ignore_if_exists=True) or the keys/condition are invalid.
    """
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Creating index {index_name!r} on {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        index_manager = collection.query_indexes()
        index_manager.create_index(
            index_name,
            keys,
            CreateQueryIndexOptions(
                deferred=deferred,
                condition=condition,
                num_replicas=num_replicas,
                ignore_if_exists=ignore_if_exists,
            ),
        )
        logger.info(f"Created index {index_name!r} on {keyspace} (deferred={deferred})")
        result = {"index_name": index_name, "deferred": deferred, "keyspace": keyspace}
        if deferred:
            result["next_step"] = (
                f"Index '{index_name}' was created deferred and is NOT yet usable. "
                "Call build_index to build it, then list_indexes to confirm it reaches 'online'."
            )
        return tool_success(**result)
    except Exception as e:
        logger.error(
            f"Error creating index {index_name!r} on {keyspace}: {e}", exc_info=True
        )
        return tool_error(e, index_name=index_name, keyspace=keyspace)


def build_index(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
) -> dict[str, Any]:
    """Trigger the build of all deferred indexes on a collection.

    This builds every index in the collection currently in the 'deferred' state — you
    cannot target a single index by name, and this includes vector indexes if any are
    deferred (only create_index is restricted to scalar indexes; build is not). If there
    are no deferred indexes, this is a harmless no-op.

    The build runs asynchronously: success means the build was triggered, not that it has
    finished. Use list_indexes to check when the index(es) reach the 'online' state.

    Returns {"success": True, "keyspace": ...} on success, or
    {"success": False, "error": ...} on failure.
    """
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Building deferred indexes on {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        index_manager = collection.query_indexes()
        index_manager.build_deferred_indexes()
        logger.info(f"Triggered build of deferred indexes on {keyspace}")
        return tool_success(keyspace=keyspace)
    except Exception as e:
        logger.error(
            f"Error building deferred indexes on {keyspace}: {e}", exc_info=True
        )
        return tool_error(e, keyspace=keyspace)


def drop_index(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    index_name: str,
    ignore_if_not_exists: bool = False,
) -> dict[str, Any]:
    """Drop an existing GSI index (scalar or vector) from a collection.

    This permanently removes the index and cannot be undone. Queries that relied on it may
    become slower or fall back to another index, and the index would have to be recreated
    (and rebuilt) to restore it. Prefer confirming the index name with list_indexes first.

    Pass ignore_if_not_exists=True to avoid an error when the named index doesn't exist.

    Returns {"success": True, "index_name": ..., "keyspace": ...} on success, or
    {"success": False, "error": ...} on failure — e.g. the index does not exist
    (without ignore_if_not_exists=True).
    """
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Dropping index {index_name!r} on {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        index_manager = collection.query_indexes()
        index_manager.drop_index(
            index_name,
            DropQueryIndexOptions(ignore_if_not_exists=ignore_if_not_exists),
        )
        logger.info(f"Dropped index {index_name!r} on {keyspace}")
        return tool_success(index_name=index_name, keyspace=keyspace)
    except Exception as e:
        logger.error(
            f"Error dropping index {index_name!r} on {keyspace}: {e}", exc_info=True
        )
        return tool_error(e, index_name=index_name, keyspace=keyspace)
