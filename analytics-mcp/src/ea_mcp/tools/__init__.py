"""Enterprise Analytics (EA) prototype MCP tools.

Flat tool set — no read/write split, no read-only-mode gating, unlike the
parent ``cb_mcp.tools`` package. All 10 tools are registered unconditionally.
"""

from collections.abc import Callable

from mcp.types import ToolAnnotations

from .index import create_index, list_indexes
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
from .query import explain_query, run_query_sync

TOOLS: list[Callable] = [
    get_databases_in_cluster,
    get_scopes_in_database,
    get_collections_in_scope,
    get_schema_for_collection,
    create_index,
    list_indexes,
    run_query_sync,
    # Server Async Request API (EA 2.2+).
    run_query_async,
    get_async_query_results,
    discard_async_query_results,
    cancel_async_query,
    explain_query,
]

TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_databases_in_cluster": ToolAnnotations(readOnlyHint=True),
    "get_scopes_in_database": ToolAnnotations(readOnlyHint=True),
    "get_collections_in_scope": ToolAnnotations(readOnlyHint=True),
    "get_schema_for_collection": ToolAnnotations(readOnlyHint=True),
    "list_indexes": ToolAnnotations(readOnlyHint=True),
    # create_index issues DDL, so it is not read-only (matches create_index in
    # the parent server). Per the EA tool spec it is unavailable in read-only
    # mode, which this prototype does not yet implement.
    "create_index": ToolAnnotations(),
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
    "explain_query": ToolAnnotations(readOnlyHint=True),
}

__all__ = [
    "TOOLS",
    "TOOL_ANNOTATIONS",
    "cancel_async_query",
    "discard_async_query_results",
    "get_async_query_results",
    "create_index",
    "explain_query",
    "get_collections_in_scope",
    "get_databases_in_cluster",
    "get_schema_for_collection",
    "get_scopes_in_database",
    "run_query_async",
    "list_indexes",
    "run_query_sync",
]
