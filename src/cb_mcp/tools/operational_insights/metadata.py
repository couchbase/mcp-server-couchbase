"""System-metadata introspection tools for Operational Insights.

Databases/scopes/collections are queried via SQL++ against the
``System.Metadata`` catalog. Read tools: raise on error, return the raw list
of rows (no ``{"success": ...}`` envelope) — matching the operational
server's read-tool convention.
"""

import logging
from typing import Any

from couchbase_operational_insights.options import QueryOptions
from fastmcp import Context

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from ...utils.operational_insights.context import get_oi_cluster
from ...utils.operational_insights.sqlpp import keyspace

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.tools.metadata")

MAX_SCHEMA_SAMPLE_SIZE = 10_000


def get_databases_in_cluster(ctx: Context) -> list[dict[str, Any]]:
    """List all databases in the Operational Insights cluster.

    Returns a list of rows, each with a DatabaseName field.
    """
    query = (
        "SELECT DISTINCT d.DatabaseName "
        "FROM System.Metadata.`Dataverse` d "
        'WHERE d.DataverseName <> "Metadata";'
    )
    try:
        logger.debug("Listing databases in cluster")
        cluster = get_oi_cluster(ctx)
        result = cluster.execute_query(query)
        rows = result.get_all_rows()
        logger.info(f"Found {len(rows)} database(s)")
        return rows
    except Exception as e:
        logger.error(f"Error listing databases: {e}", exc_info=True)
        raise


def get_scopes_in_database(ctx: Context, database_name: str) -> list[dict[str, Any]]:
    """List all scopes in a database.

    Returns a list of rows, each with DatabaseName and ScopeName fields.
    """
    query = (
        "SELECT d.DatabaseName, d.DataverseName AS ScopeName "
        "FROM System.Metadata.`Dataverse` d "
        'WHERE d.DataverseName <> "Metadata" AND d.DatabaseName = $database_name;'
    )
    try:
        logger.debug(f"Listing scopes in database {database_name!r}")
        cluster = get_oi_cluster(ctx)
        result = cluster.execute_query(
            query, QueryOptions(named_parameters={"database_name": database_name})
        )
        rows = result.get_all_rows()
        logger.info(f"Found {len(rows)} scope(s) in database {database_name!r}")
        return rows
    except Exception as e:
        logger.error(
            f"Error listing scopes in database {database_name!r}: {e}", exc_info=True
        )
        raise


def get_collections_in_scope(
    ctx: Context, database_name: str, scope_name: str
) -> list[dict[str, Any]]:
    """List all collections (datasets) in a scope.

    Returns a list of rows, each with DatabaseName, ScopeName,
    CollectionName, and Type fields. Type is the collection's DatasetType
    (INTERNAL, EXTERNAL, or VIEW) — callers that only want stored/linked
    collections should filter out Type == "VIEW" themselves.

    Note: this server and the operational server both expose a tool named
    ``get_collections_in_scope``. They run as separate processes/servers, so
    this is only a concern for a client that registers both simultaneously —
    see CONTRIBUTING.md's tool-naming section.
    """
    query = (
        "SELECT d.DatabaseName, d.DataverseName AS ScopeName, "
        "d.DatasetName AS CollectionName, d.DatasetType AS `Type` "
        "FROM System.Metadata.`Dataset` d "
        'WHERE d.DataverseName <> "Metadata" '
        "AND d.DatabaseName = $database_name AND d.DataverseName = $scope_name;"
    )
    try:
        logger.debug(f"Listing collections in {database_name!r}.{scope_name!r}")
        cluster = get_oi_cluster(ctx)
        result = cluster.execute_query(
            query,
            QueryOptions(
                named_parameters={
                    "database_name": database_name,
                    "scope_name": scope_name,
                }
            ),
        )
        rows = result.get_all_rows()
        logger.info(
            f"Found {len(rows)} collection(s) in {database_name!r}.{scope_name!r}"
        )
        return rows
    except Exception as e:
        logger.error(
            f"Error listing collections in {database_name!r}.{scope_name!r}: {e}",
            exc_info=True,
        )
        raise


def get_schema_for_collection(
    ctx: Context,
    database_name: str,
    scope_name: str,
    collection_name: str,
    sample_size: int = 1000,
    num_sample_values: int = 0,
) -> list[dict[str, Any]]:
    """Infer the JSON schema of a collection using Analytics' built-in
    ARRAY_INFER_SCHEMA function, sampling up to sample_size documents.

    ARRAY_INFER_SCHEMA detects distinct structural "flavors" across the
    sample and returns one JSON-Schema-shaped object per flavor (with
    per-property type/percentage stats) — this is the same function the
    Capella UI uses for schema inference. sample_size must be positive and
    is capped at 10_000.

    num_sample_values caps how many example values ARRAY_INFER_SCHEMA
    includes per property. It defaults to 0 here so
    this tool reports structure only, without pulling actual document
    content into results — pass a higher value to get samples. Must be
    non-negative.

    Returns a list of JSON-Schema-shaped objects, one per detected flavor.

    Note: this server and the operational server both expose a tool named
    ``get_schema_for_collection``. They run as separate processes/servers, so
    this is only a concern for a client that registers both simultaneously —
    see CONTRIBUTING.md's tool-naming section.
    """
    if sample_size <= 0:
        raise ValueError(f"sample_size must be positive, got {sample_size}")
    sample_size = min(sample_size, MAX_SCHEMA_SAMPLE_SIZE)

    if num_sample_values < 0:
        raise ValueError(
            f"num_sample_values must be non-negative, got {num_sample_values}"
        )

    ks = keyspace(database_name, scope_name, collection_name)
    # ks is built entirely from safe_ident()-quoted (backtick-escaped)
    # identifiers, and sample_size/infer_params are bound $-parameters
    # below, not interpolated — not an injection vector despite the f-string.
    query = f"SELECT VALUE ARRAY_INFER_SCHEMA((SELECT VALUE d FROM {ks} AS d LIMIT $sample_size), $infer_params);"  # noqa: S608
    try:
        logger.debug(f"Inferring schema for {ks}")
        cluster = get_oi_cluster(ctx)
        result = cluster.execute_query(
            query,
            QueryOptions(
                named_parameters={
                    "sample_size": sample_size,
                    "infer_params": {"num_sample_values": num_sample_values},
                }
            ),
        )
        # SELECT VALUE over a bare array_infer_schema() call returns exactly
        # one row whose value is the array of flavor objects itself — unwrap
        # it so this tool returns a flat list of flavor objects like its
        # other list-returning siblings.
        rows = result.get_all_rows()
        flavors = rows[0] if rows else []
        logger.info(f"Inferred schema for {ks} from {sample_size} sampled document(s)")
        return flavors
    except Exception as e:
        logger.error(f"Error inferring schema for {ks}: {e}", exc_info=True)
        raise


__all__ = [
    "MAX_SCHEMA_SAMPLE_SIZE",
    "get_collections_in_scope",
    "get_databases_in_cluster",
    "get_schema_for_collection",
    "get_scopes_in_database",
]
