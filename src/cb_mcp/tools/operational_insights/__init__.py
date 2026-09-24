"""
Operational Insights MCP Tools

This module contains all the MCP tools for the Operational Insights server.

Tool Categories:
- READ_ONLY_TOOLS: Tools that only read data (always available)
- WRITE_TOOLS: Tools that modify data (disabled when read_only_mode is True)

Four of these tool names — get_collections_in_scope, get_schema_for_collection,
create_index and list_indexes — also exist on the operational server, with a
different implementation behind each. Both servers run as independent
processes, so this only matters to a client that registers both
simultaneously; see CONTRIBUTING.md's tool-naming section for why these were
kept as-is rather than renamed.

get_server_configuration_status also appears on both, but is not one of those:
it is a single shared function (cb_mcp.tools.status) that every server
registers, so a client sees one tool with one behaviour.

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
from ..status import get_server_configuration_status
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
    explain_query,
    get_async_query_results,
    run_query_async,
    run_query_sync,
)

# The Operational Insights server's tool inventory, and the single source of
# truth for it.
TOOL_SET = ToolSet(
    read_only=(
        # Shared across every server — same function object as the
        # operational server registers. See cb_mcp/tools/status.py.
        get_server_configuration_status,
        get_databases_in_cluster,
        get_scopes_in_database,
        get_collections_in_scope,
        get_schema_for_collection,
        list_indexes,
        explain_query,
        # run_query_sync/run_query_async can carry DDL/DML, so — like
        # run_sql_plus_plus_query on the operational server — write
        # protection is enforced at runtime (QueryOptions(readonly=True)),
        # not by omitting them here.
        run_query_sync,
        run_query_async,
        # Pure reads/cleanup of an already-gated handle — no runtime
        # read-only logic needed.
        get_async_query_results,
        discard_async_query_results,
    ),
    write=(
        create_index,
        # Interrupts an in-flight query, a more consequential action than
        # discarding an already-finished one — disabled entirely under
        # --read-only-mode rather than runtime-gated.
        cancel_async_query,
    ),
)

# Derived views, kept for parity with the operational tools package.
READ_ONLY_TOOLS = list(TOOL_SET.read_only)
WRITE_TOOLS = list(TOOL_SET.write)
ALL_TOOLS = TOOL_SET.all_tools

# Tool annotations for MCP clients (readOnlyHint, destructiveHint, etc.)
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_server_configuration_status": ToolAnnotations(readOnlyHint=True),
    "get_databases_in_cluster": ToolAnnotations(readOnlyHint=True),
    "get_scopes_in_database": ToolAnnotations(readOnlyHint=True),
    "get_collections_in_scope": ToolAnnotations(readOnlyHint=True),
    "get_schema_for_collection": ToolAnnotations(readOnlyHint=True),
    "list_indexes": ToolAnnotations(readOnlyHint=True),
    "explain_query": ToolAnnotations(readOnlyHint=True),
    # run_query_sync/run_query_async can carry DDL/DML, so they get no
    # readOnlyHint (matches run_sql_plus_plus_query on the operational
    # server).
    "run_query_sync": ToolAnnotations(),
    "run_query_async": ToolAnnotations(),
    "get_async_query_results": ToolAnnotations(readOnlyHint=True),
    # The annotation is about destructive *effect*, independent of which
    # registration bucket each tool ends up in: both free/interrupt
    # server-side state even though only one is write-gated.
    "discard_async_query_results": ToolAnnotations(destructiveHint=True),
    "cancel_async_query": ToolAnnotations(destructiveHint=True),
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
    "run_query_async": (
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
    "cancel_async_query",
    "create_index",
    "discard_async_query_results",
    "explain_query",
    "get_async_query_results",
    "get_collections_in_scope",
    "get_databases_in_cluster",
    "get_server_configuration_status",
    "get_schema_for_collection",
    "get_scopes_in_database",
    "get_tools",
    "list_indexes",
    "run_query_async",
    "run_query_sync",
]
