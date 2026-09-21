"""
Operational Insights MCP Tools

This module contains all the MCP tools for the Operational Insights server.

Tool Categories:
- READ_ONLY_TOOLS: Tools that only read data (always available)
- WRITE_TOOLS: Tools that modify data (disabled when read_only_mode is True)

Three of these tool names — get_collections_in_scope, get_schema_for_collection
and create_index — also exist on the operational server. Both servers run as
independent processes, so this only matters to a client that registers both
simultaneously; see CONTRIBUTING.md's tool-naming section for why these were
kept as-is rather than renamed.

Import order below matters and is deliberately alphabetical (".index" before
".metadata" before ".query"): .index imports
cb_mcp.utils.operational_insights.context, which imports the
cb_mcp.utils.operational_insights package and so runs its __init__.py (which
snapshots the stdlib root logger's handlers) before .metadata's own
"from couchbase_operational_insights.options import ..." line ever executes.
See utils/operational_insights/sdk_logging.py.
"""

from collections.abc import Callable

from mcp.types import ToolAnnotations

from ...core.spec import ToolSet
from ...utils.constants import SCOPE_READ, SCOPE_WRITE
from .index import create_index
from .metadata import (
    get_collections_in_scope,
    get_databases_in_cluster,
    get_schema_for_collection,
    get_scopes_in_database,
)
from .query import explain_query, run_query_sync

# The Operational Insights server's tool inventory, and the single source of
# truth for it.
TOOL_SET = ToolSet(
    read_only=(
        get_databases_in_cluster,
        get_scopes_in_database,
        get_collections_in_scope,
        get_schema_for_collection,
        explain_query,
        # run_query_sync can carry DDL/DML, so — like run_sql_plus_plus_query
        # on the operational server — write protection is enforced at
        # runtime (QueryOptions(readonly=True)), not by omitting it here.
        run_query_sync,
    ),
    write=(create_index,),
)

# Derived views, kept for parity with the operational tools package.
READ_ONLY_TOOLS = list(TOOL_SET.read_only)
WRITE_TOOLS = list(TOOL_SET.write)
ALL_TOOLS = TOOL_SET.all_tools

# Tool annotations for MCP clients (readOnlyHint, destructiveHint, etc.)
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_databases_in_cluster": ToolAnnotations(readOnlyHint=True),
    "get_scopes_in_database": ToolAnnotations(readOnlyHint=True),
    "get_collections_in_scope": ToolAnnotations(readOnlyHint=True),
    "get_schema_for_collection": ToolAnnotations(readOnlyHint=True),
    "explain_query": ToolAnnotations(readOnlyHint=True),
    # run_query_sync can carry DDL/DML, so it gets no readOnlyHint (matches
    # run_sql_plus_plus_query on the operational server).
    "run_query_sync": ToolAnnotations(),
    # create_index issues DDL (matches create_index on the operational
    # server).
    "create_index": ToolAnnotations(),
}

# Per-tool scope-denial hints. Kept in this package rather than the shared
# cb_mcp.utils.scope_enforcement module (where the operational server's hints
# live) so this server needs no shared-file changes.
TOOL_SCOPE_HINTS: dict[str, str] = {
    "run_query_sync": (
        f"A '{SCOPE_WRITE}'-only token cannot invoke SQL++; '{SCOPE_READ}' is required."
    ),
}


def get_tools(read_only_mode: bool = True) -> list[Callable]:
    """Get the list of tools based on the mode settings.

    This function determines which tools should be loaded based on the
    read_only_mode setting. When read_only_mode is True, write tools are
    excluded.
    """
    return TOOL_SET.tools_for(read_only_mode=read_only_mode)


__all__ = [
    "ALL_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_ANNOTATIONS",
    "TOOL_SCOPE_HINTS",
    "TOOL_SET",
    "WRITE_TOOLS",
    "create_index",
    "explain_query",
    "get_collections_in_scope",
    "get_databases_in_cluster",
    "get_schema_for_collection",
    "get_scopes_in_database",
    "get_tools",
    "run_query_sync",
]
