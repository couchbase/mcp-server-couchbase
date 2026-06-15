"""
Tools for querying the Couchbase database.

This module contains tools for getting the schema for a collection and running SQL++ queries.
"""

import logging
import re
from typing import Any

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token
from lark_sqlpp import modifies_data, modifies_structure, parse_sqlpp

from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME, SCOPE_WRITE
from ..utils.context import get_cluster_connection
from ..utils.query_utils import (
    evaluate_query_plan,
    extract_plan_from_explain_results,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.query")


def get_schema_for_collection(
    ctx: Context, bucket_name: str, scope_name: str, collection_name: str
) -> dict[str, Any]:
    """Get the schema for a collection in the specified scope.
    Returns a dictionary with the collection name and the schema returned by running INFER query on the Couchbase collection.
    """
    schema = {"collection_name": collection_name, "schema": []}
    try:
        query = f"INFER `{collection_name}`"
        result = run_sql_plus_plus_query(ctx, bucket_name, scope_name, query)
        # Result is a list of list of schemas. We convert it to a list of schemas.
        if result:
            schema["schema"] = result[0]
    except Exception as e:
        logger.error(f"Error getting schema: {e}")
        raise
    return schema


def _is_explain_statement(query: str) -> bool:
    """Check if the query is an EXPLAIN statement.

    Handles multi-line queries where EXPLAIN is followed by newline or tab,
    e.g., "EXPLAIN\nSELECT ..." or "EXPLAIN\tSELECT ...".
    """
    # Match "EXPLAIN" followed by any whitespace (space, tab, newline, etc.)
    normalized = query.lstrip().upper()
    return re.match(r"^EXPLAIN\s", normalized) is not None


def run_sql_plus_plus_query(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    query: str,
    named_parameters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run a SQL++ query on a scope and return the results as a list of JSON objects.

    The query will be run on the specified scope in the specified bucket.
    The query should use collection names directly without bucket/scope prefixes, as the scope context is automatically set.

    Use ``named_parameters`` to bind values to ``$name`` placeholders in the
    query instead of concatenating user input into the statement. This prevents
    SQL++ injection

    Example:
        query = "SELECT * FROM users WHERE age > 18"
        # Incorrect: "SELECT * FROM bucket.scope.users WHERE age > 18"
    """
    cluster = get_cluster_connection(ctx)

    bucket = connect_to_bucket(cluster, bucket_name)

    app_context = ctx.request_context.lifespan_context
    read_only_mode = app_context.read_only_mode
    read_only_query_mode = app_context.read_only_query_mode

    # SQL++ writes are blocked under any of three conditions:
    #   1. read_only_mode is True (blanket lockdown of all writes)
    #   2. read_only_query_mode is True (deprecated query-only lockdown)
    #   3. The caller's JWT carries SCOPE_READ but NOT SCOPE_WRITE.
    # Case 3 closes the gap where a token holding only couchbase-mcp:read
    # could otherwise mutate data through SQL++ because the per-tool scope
    # wrapper classifies SQL++ as a read tool. Without this check, the
    # JWT scope guarantee would be weaker than the spec promises.
    # When no token is present (stdio / OAuth disabled), `lacks_write_scope`
    # is False so the historical config-only behavior is preserved.
    token = get_access_token()
    lacks_write_scope = token is not None and SCOPE_WRITE not in (token.scopes or [])
    block_query_writes = read_only_mode or read_only_query_mode or lacks_write_scope

    try:
        scope = bucket.scope(scope_name)

        results = []
        # EXPLAIN statements are always safe to execute and should bypass write checks.
        if block_query_writes and not _is_explain_statement(query):
            parsed_query = parse_sqlpp(query)
            data_modification_query = modifies_data(parsed_query)
            structure_modification_query = modifies_structure(parsed_query)

            if data_modification_query or structure_modification_query:
                kind = "data" if data_modification_query else "structure"
                if lacks_write_scope and not (read_only_mode or read_only_query_mode):
                    # lacks_write_scope implies token is not None here.
                    held_scopes = sorted(set(token.scopes or []))
                    msg = (
                        f"SQL++ {kind} modification requires the "
                        f"'{SCOPE_WRITE}' scope; token scopes are {held_scopes}."
                    )
                    logger.warning(msg)
                    raise PermissionError(msg)
                msg = f"{kind.capitalize()} modification query is not allowed in read-only mode"
                logger.error(msg)
                raise ValueError(msg)

        # Run the query if it is not a data or structure modification query.
        # Forward named parameters only when provided so existing callers that
        # pass none keep the exact previous behaviour.
        result = (
            scope.query(query, named_parameters=named_parameters)
            if named_parameters is not None
            else scope.query(query)
        )
        for row in result:
            results.append(row)
        return results
    except Exception as e:
        logger.error(f"Error running query: {e!s}", exc_info=True)
        raise


def explain_sql_plus_plus_query(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    query: str,
) -> dict[str, Any]:
    """Generate and evaluate an EXPLAIN plan for a SQL++ query. It provides information about the execution plan for the query.

    The EXPLAIN statement is run in the specified scope in the specified bucket.
    It returns query metadata along with an extracted plan and plan evaluation.
    """
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Query cannot be empty.")

    explain_statement = (
        normalized_query
        if _is_explain_statement(normalized_query)
        else f"EXPLAIN {normalized_query}"
    )

    explain_results = run_sql_plus_plus_query(
        ctx,
        bucket_name,
        scope_name,
        explain_statement,
    )

    plan = extract_plan_from_explain_results(explain_results)
    plan_evaluation = evaluate_query_plan(plan)

    return {
        "query": query,
        "explain_statement": explain_statement,
        "query_context": {"bucket_name": bucket_name, "scope_name": scope_name},
        "plan": plan,
        "plan_evaluation": plan_evaluation,
    }


def run_cluster_query(ctx: Context, query: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Run a query on the cluster object and return the results as a list of JSON objects."""

    cluster = get_cluster_connection(ctx)
    results = []

    try:
        result = cluster.query(query, **kwargs)
        for row in result:
            results.append(row)
        return results
    except Exception as e:
        logger.error(f"Error running query: {e}")
        raise


def _run_query_tool_with_empty_message(
    ctx: Context,
    query: str,
    *,
    limit: int,
    empty_message: str,
    extra_payload: dict[str, Any] | None = None,
    **query_kwargs: Any,
) -> list[dict[str, Any]]:
    """Execute a cluster query with a consistent empty-result response."""
    results = run_cluster_query(ctx, query, limit=limit, **query_kwargs)

    if results:
        return results

    payload: dict[str, Any] = {"message": empty_message, "results": []}
    if extra_payload:
        payload.update(extra_payload)
    return [payload]


def get_longest_running_queries(ctx: Context, limit: int = 10) -> list[dict[str, Any]]:
    """Get the N longest running queries from the system:completed_requests catalog.

    Args:
        limit: Number of queries to return (default: 10)

    Returns:
        List of queries with their average service time and count
    """
    query = """
    SELECT statement,
        DURATION_TO_STR(avgServiceTime) AS avgServiceTime,
        COUNT(1) AS queries
    FROM system:completed_requests
    WHERE UPPER(statement) NOT LIKE 'INFER %'
        AND UPPER(statement) NOT LIKE 'CREATE INDEX%'
        AND UPPER(statement) NOT LIKE 'CREATE PRIMARY INDEX%'
        AND UPPER(statement) NOT LIKE '% SYSTEM:%'
    GROUP BY statement
    LETTING avgServiceTime = AVG(STR_TO_DURATION(serviceTime))
    ORDER BY avgServiceTime DESC
    LIMIT $limit
    """

    return _run_query_tool_with_empty_message(
        ctx,
        query,
        limit=limit,
        empty_message=(
            "No completed queries were available to calculate longest running queries."
        ),
    )


def get_most_frequent_queries(ctx: Context, limit: int = 10) -> list[dict[str, Any]]:
    """Get the N most frequent queries from the system:completed_requests catalog.

    Args:
        limit: Number of queries to return (default: 10)

    Returns:
        List of queries with their frequency count
    """
    query = """
    SELECT statement,
        COUNT(1) AS queries
    FROM system:completed_requests
    WHERE UPPER(statement) NOT LIKE 'INFER %'
        AND UPPER(statement) NOT LIKE 'CREATE INDEX%'
        AND UPPER(statement) NOT LIKE 'CREATE PRIMARY INDEX%'
        AND UPPER(statement) NOT LIKE 'EXPLAIN %'
        AND UPPER(statement) NOT LIKE 'ADVISE %'
        AND UPPER(statement) NOT LIKE '% SYSTEM:%'
    GROUP BY statement
    LETTING queries = COUNT(1)
    ORDER BY queries DESC
    LIMIT $limit
    """

    return _run_query_tool_with_empty_message(
        ctx,
        query,
        limit=limit,
        empty_message=(
            "No completed queries were available to calculate most frequent queries."
        ),
    )


def get_queries_with_largest_response_sizes(
    ctx: Context, limit: int = 10
) -> list[dict[str, Any]]:
    """Get queries with the largest response sizes from the system:completed_requests catalog.

    Args:
        limit: Number of queries to return (default: 10)

    Returns:
        List of queries with their average result size in bytes, KB, and MB
    """
    query = """
    SELECT statement,
        avgResultSize AS avgResultSizeBytes,
        (avgResultSize / 1000) AS avgResultSizeKB,
        (avgResultSize / 1000000) AS avgResultSizeMB,
        COUNT(1) AS queries
    FROM system:completed_requests
    WHERE UPPER(statement) NOT LIKE 'INFER %'
        AND UPPER(statement) NOT LIKE 'CREATE INDEX%'
        AND UPPER(statement) NOT LIKE 'CREATE PRIMARY INDEX%'
        AND UPPER(statement) NOT LIKE '% SYSTEM:%'
    GROUP BY statement
    LETTING avgResultSize = AVG(resultSize)
    ORDER BY avgResultSize DESC
    LIMIT $limit
    """

    return _run_query_tool_with_empty_message(
        ctx,
        query,
        limit=limit,
        empty_message=(
            "No completed queries were available to calculate response sizes."
        ),
    )


def get_queries_with_large_result_count(
    ctx: Context, limit: int = 10
) -> list[dict[str, Any]]:
    """Get queries with the largest result counts from the system:completed_requests catalog.

    Args:
        limit: Number of queries to return (default: 10)

    Returns:
        List of queries with their average result count
    """
    query = """
    SELECT statement,
        avgResultCount,
        COUNT(1) AS queries
    FROM system:completed_requests
    WHERE UPPER(statement) NOT LIKE 'INFER %' AND
        UPPER(statement) NOT LIKE 'CREATE INDEX%' AND
        UPPER(statement) NOT LIKE 'CREATE PRIMARY INDEX%' AND
        UPPER(statement) NOT LIKE '% SYSTEM:%'
    GROUP BY statement
    LETTING avgResultCount = AVG(resultCount)
    ORDER BY avgResultCount DESC
    LIMIT $limit
    """

    return _run_query_tool_with_empty_message(
        ctx,
        query,
        limit=limit,
        empty_message=(
            "No completed queries were available to calculate result counts."
        ),
    )


def get_queries_using_primary_index(
    ctx: Context, limit: int = 10
) -> list[dict[str, Any]]:
    """Get queries that use a primary index from the system:completed_requests catalog.

    Args:
        limit: Number of queries to return (default: 10)

    Returns:
        List of queries that use primary indexes, ordered by result count
    """
    query = """
    SELECT *
    FROM system:completed_requests
    WHERE phaseCounts.`primaryScan` IS NOT MISSING
        AND UPPER(statement) NOT LIKE '% SYSTEM:%'
    ORDER BY resultCount DESC
    LIMIT $limit
    """

    return _run_query_tool_with_empty_message(
        ctx,
        query,
        limit=limit,
        empty_message=(
            "No queries using the primary index were found in system:completed_requests."
        ),
    )


def get_queries_not_using_covering_index(
    ctx: Context, limit: int = 10
) -> list[dict[str, Any]]:
    """Get queries that don't use a covering index from the system:completed_requests catalog.

    Args:
        limit: Number of queries to return (default: 10)

    Returns:
        List of queries that perform index scans but also require fetches (not covering)
    """
    query = """
    SELECT *
    FROM system:completed_requests
    WHERE phaseCounts.`indexScan` IS NOT MISSING
        AND phaseCounts.`fetch` IS NOT MISSING
        AND UPPER(statement) NOT LIKE '% SYSTEM:%'
    ORDER BY resultCount DESC
    LIMIT $limit
    """

    return _run_query_tool_with_empty_message(
        ctx,
        query,
        limit=limit,
        empty_message=(
            "No queries that require fetches after index scans were found "
            "in system:completed_requests."
        ),
    )


def get_queries_not_selective(ctx: Context, limit: int = 10) -> list[dict[str, Any]]:
    """Get queries that are not very selective from the system:completed_requests catalog.

    Args:
        limit: Number of queries to return (default: 10)

    Returns:
        List of queries where index scans return significantly more documents than the final result
    """
    query = """
    SELECT statement,
       AVG(phaseCounts.`indexScan` - resultCount) AS diff
    FROM system:completed_requests
    WHERE phaseCounts.`indexScan` > resultCount
    GROUP BY statement
    ORDER BY diff DESC
    LIMIT $limit
    """

    return _run_query_tool_with_empty_message(
        ctx,
        query,
        limit=limit,
        empty_message=(
            "No non-selective queries were found in system:completed_requests."
        ),
    )
