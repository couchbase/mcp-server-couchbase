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

# SQL++ has no bind-parameter support for identifiers (only for values), so
# database/scope/collection names must be safely quoted before being
# interpolated into a keyspace string. Backtick-quoting alone isn't enough —
# a name containing a backtick could otherwise break out of the identifier
# and inject arbitrary SQL++ (matches the parent mcp-server-couchbase's
# safe_ident() convention in src/cb_mcp/tools/query.py).
MAX_SCHEMA_SAMPLE_SIZE = 10_000


def safe_ident(name: str) -> str:
    """Backtick-quote a SQL++ identifier, doubling embedded backticks."""
    return "`" + name.replace("`", "``") + "`"


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

    Returns a list of rows, each with DatabaseName, ScopeName,
    CollectionName, and Type fields. Type is the collection's DatasetType
    (INTERNAL, EXTERNAL, or VIEW) — callers that only want stored/linked
    collections should filter out Type == "VIEW" themselves.
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
    """Infer the JSON schema of a collection using Analytics' built-in
    ARRAY_INFER_SCHEMA function, sampling up to sample_size documents.

    ARRAY_INFER_SCHEMA detects distinct structural "flavors" across the
    sample and returns one JSON-Schema-shaped object per flavor (with
    per-property type/percentage/sample-value stats) — this is the same
    function the Capella UI uses for schema inference. sample_size must be
    positive and is capped at 10_000.

    Returns a list of JSON-Schema-shaped objects, one per detected flavor.
    """
    if sample_size <= 0:
        raise ValueError(f"sample_size must be positive, got {sample_size}")
    sample_size = min(sample_size, MAX_SCHEMA_SAMPLE_SIZE)

    keyspace = (
        f"{safe_ident(database_name)}.{safe_ident(scope_name)}."
        f"{safe_ident(collection_name)}"
    )
    query = (
        "SELECT VALUE ARRAY_INFER_SCHEMA("
        f"(SELECT VALUE d FROM {keyspace} AS d LIMIT $sample_size)"
        ");"
    )
    try:
        logger.debug(f"Inferring schema for {keyspace}")
        cluster = get_cluster_connection(ctx)
        result = cluster.execute_query(
            query, QueryOptions(named_parameters={"sample_size": sample_size})
        )
        # SELECT VALUE over a bare array_infer_schema() call returns exactly
        # one row whose value is the array of flavor objects itself — unwrap
        # it so this tool returns a flat list of flavor objects like its
        # other list-returning siblings.
        rows = result.get_all_rows()
        flavors = rows[0] if rows else []
        logger.info(
            f"Inferred schema for {keyspace} from {sample_size} sampled document(s)"
        )
        return flavors
    except Exception as e:
        logger.error(f"Error inferring schema for {keyspace}: {e}", exc_info=True)
        raise
