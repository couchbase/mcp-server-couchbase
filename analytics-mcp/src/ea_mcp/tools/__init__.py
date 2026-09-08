"""Enterprise Analytics (EA) prototype MCP tools.

Flat tool set — no read/write split, no read-only-mode gating, unlike the
parent ``cb_mcp.tools`` package. All 10 tools are registered unconditionally.
"""

from collections.abc import Callable

from mcp.types import ToolAnnotations

from .metadata import (
    get_collections_in_scope,
    get_databases_in_cluster,
    get_schema_for_collection,
    get_scopes_in_database,
)
from .query import (
    cancel_async_query,
    discard_async_query_results,
    get_async_query_results,
    run_query_async,
    run_query_sync,
)
from .query_poc import (
    get_async_query_results_poc_resource,
    get_query_poc_async_results,
    release_query_poc_result,
    run_query_poc_async,
    run_query_poc_resource,
)

TOOLS: list[Callable] = [
    get_databases_in_cluster,
    get_scopes_in_database,
    get_collections_in_scope,
    get_schema_for_collection,
    run_query_sync,
    # Server Async Request API (EA 2.2+).
    run_query_async,
    get_async_query_results,
    discard_async_query_results,
    cancel_async_query,
    # POC: large-result handling via MCP resources. A query returns a small
    # preview plus a resource_uri; the rows are read back through the resource
    # templates (whole result, or a page). See tools/query_poc.py.
    run_query_poc_resource,
    # Async: reuse run_query_async, truncate at fetch time.
    get_async_query_results_poc_resource,
    # Async: one result_id spanning running and ready.
    run_query_poc_async,
    get_query_poc_async_results,
    # Cleanup for every POC above.
    release_query_poc_result,
]

TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_databases_in_cluster": ToolAnnotations(readOnlyHint=True),
    "get_scopes_in_database": ToolAnnotations(readOnlyHint=True),
    "get_collections_in_scope": ToolAnnotations(readOnlyHint=True),
    "get_schema_for_collection": ToolAnnotations(readOnlyHint=True),
    # run_query_sync can carry DDL/DML per the tool spec, so it gets no
    # readOnlyHint (matches run_sql_plus_plus_query in the parent server).
    "run_query_sync": ToolAnnotations(),
    # run_query_async can likewise carry DDL/DML — no readOnlyHint.
    "run_query_async": ToolAnnotations(),
    # Fetching does not free EA's buffers or evict the token, so it is
    # genuinely repeatable and side-effect free.
    "get_async_query_results": ToolAnnotations(readOnlyHint=True),
    "discard_async_query_results": ToolAnnotations(destructiveHint=True),
    "cancel_async_query": ToolAnnotations(destructiveHint=True),
    # The POC runners wrap the same execute_query as run_query_sync, so they
    # can carry DDL/DML too — no readOnlyHint.
    "run_query_poc_resource": ToolAnnotations(),
    "run_query_poc_async": ToolAnnotations(),
    # Fetching buffers rows server-side but does not consume EA's, so these
    # stay repeatable and side-effect free (same reasoning as
    # get_async_query_results).
    "get_async_query_results_poc_resource": ToolAnnotations(readOnlyHint=True),
    "get_query_poc_async_results": ToolAnnotations(readOnlyHint=True),
    # Frees the buffer and cancels the query if it is still running.
    "release_query_poc_result": ToolAnnotations(destructiveHint=True),
}

__all__ = [
    "TOOLS",
    "TOOL_ANNOTATIONS",
    "cancel_async_query",
    "discard_async_query_results",
    "get_async_query_results",
    "get_async_query_results_poc_resource",
    "get_collections_in_scope",
    "get_databases_in_cluster",
    "get_query_poc_async_results",
    "get_schema_for_collection",
    "get_scopes_in_database",
    "release_query_poc_result",
    "run_query_async",
    "run_query_poc_async",
    "run_query_poc_resource",
    "run_query_sync",
]
