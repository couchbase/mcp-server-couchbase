"""System-metadata introspection tools for Enterprise Analytics.

Databases/scopes/collections are queried via SQL++ against the
``System.Metadata`` catalog, per the EA tool spec. Read tools: raise on
error, return the raw list of rows (no {"success": ...} envelope) — matching
the parent ``mcp-server-couchbase`` read-tool convention.
"""

import logging
from typing import Any

from couchbase_analytics.options import QueryOptions
from fastmcp import Context

from ..connection import get_cluster_connection

logger = logging.getLogger("ea-mcp-server.tools.metadata")


def get_databases_in_cluster(ctx: Context) -> list[dict[str, Any]]:
    """List all databases in the Enterprise Analytics cluster.

    Returns a list of rows, each with a DatabaseName field.
    """
    query = (
        "SELECT DISTINCT d.DatabaseName "
        "FROM System.Metadata.`Dataverse` d "
        'WHERE d.DataverseName <> "Metadata";'
    )
    try:
        logger.debug("Listing databases in cluster")
        cluster = get_cluster_connection(ctx)
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
        cluster = get_cluster_connection(ctx)
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

    Returns a list of rows, each with DatabaseName, ScopeName, and
    CollectionName fields.
    """
    query = (
        "SELECT d.DatabaseName, d.DataverseName AS ScopeName, "
        "d.DatasetName AS CollectionName "
        "FROM System.Metadata.`Dataset` d "
        'WHERE d.DataverseName <> "Metadata" '
        "AND d.DatabaseName = $database_name AND d.DataverseName = $scope_name;"
    )
    try:
        logger.debug(f"Listing collections in {database_name!r}.{scope_name!r}")
        cluster = get_cluster_connection(ctx)
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
) -> list[dict[str, Any]]:
    """Infer the JSON schema of a collection by sampling documents.

    Samples up to sample_size documents from the collection and reports each
    field's inferred type and how many sampled documents had it.

    Returns a list of rows, each with field, data_type, and occurrences.
    """
    keyspace = f"`{database_name}`.`{scope_name}`.`{collection_name}`"
    query = (
        f"SELECT p.name AS field, t AS data_type, COUNT(*) AS occurrences "
        f"FROM (SELECT VALUE d FROM {keyspace} AS d LIMIT $sample_size) AS d "
        f"UNNEST OBJECT_PAIRS(d) AS p "
        f"LET t = CASE "
        f'WHEN is_number(p.`value`) THEN "number" '
        f'WHEN is_string(p.`value`) THEN "string" '
        f'WHEN is_boolean(p.`value`) THEN "boolean" '
        f'WHEN is_array(p.`value`) THEN "array" '
        f'WHEN is_object(p.`value`) THEN "object" '
        f'ELSE "null/unknown" END '
        f"GROUP BY p.name, t "
        f"ORDER BY field, occurrences DESC;"
    )
    try:
        logger.debug(f"Inferring schema for {keyspace}")
        cluster = get_cluster_connection(ctx)
        result = cluster.execute_query(
            query, QueryOptions(named_parameters={"sample_size": sample_size})
        )
        rows = result.get_all_rows()
        logger.info(
            f"Inferred schema for {keyspace} from {sample_size} sampled document(s)"
        )
        return rows
    except Exception as e:
        logger.error(f"Error inferring schema for {keyspace}: {e}", exc_info=True)
        raise
