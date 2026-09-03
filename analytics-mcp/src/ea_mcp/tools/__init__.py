"""Enterprise Analytics (EA) prototype MCP tools.

Flat tool set — no read/write split, no read-only-mode gating, unlike the
parent ``cb_mcp.tools`` package. All 8 tools are registered unconditionally.
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
from .query import explain_query, run_query_sync

TOOLS: list[Callable] = [
    get_databases_in_cluster,
    get_scopes_in_database,
    get_collections_in_scope,
    get_schema_for_collection,
    create_index,
    list_indexes,
    run_query_sync,
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
    "explain_query": ToolAnnotations(readOnlyHint=True),
}

__all__ = [
    "TOOLS",
    "TOOL_ANNOTATIONS",
    "create_index",
    "explain_query",
    "get_collections_in_scope",
    "get_databases_in_cluster",
    "get_schema_for_collection",
    "get_scopes_in_database",
    "list_indexes",
    "run_query_sync",
]
