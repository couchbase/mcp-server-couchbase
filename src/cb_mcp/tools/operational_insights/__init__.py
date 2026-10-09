"""
Operational Insights MCP Tools

This module contains all the MCP tools for the Operational Insights server.

Tool Categories:
- READ_ONLY_TOOLS: Tools that only read data (always available)
- WRITE_TOOLS: Tools that modify data (disabled when read_only_mode is True)

Every tool name here is prefixed with ``oi_`` precisely so it cannot collide
with the operational server's tool names even though a client might register
both servers at once — see CONTRIBUTING.md's tool-naming section.

get_server_configuration_status also appears on both servers, but is a
different case: it is a single shared function (cb_mcp.tools.status) that
every server registers, so a client sees one tool with one behaviour.

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
from .index import oi_create_index, oi_list_indexes
from .metadata import (
    oi_get_collections_in_scope,
    oi_get_databases_in_cluster,
    oi_get_schema_for_collection,
    oi_get_scopes_in_database,
)
from .query import (
    oi_cancel_async_query,
    oi_discard_async_query_results,
    oi_explain_query,
    oi_get_async_query_results,
    oi_run_query_async,
    oi_run_query_sync,
)

# The Operational Insights server's tool inventory, and the single source of
# truth for it.
TOOL_SET = ToolSet(
    read_only=(
        # Shared across every server — same function object as the
        # operational server registers. See cb_mcp/tools/status.py.
        get_server_configuration_status,
        oi_get_databases_in_cluster,
        oi_get_scopes_in_database,
        oi_get_collections_in_scope,
        oi_get_schema_for_collection,
        oi_list_indexes,
        oi_explain_query,
        # oi_run_query_sync/oi_run_query_async can carry DDL/DML, so — like
        # run_sql_plus_plus_query on the operational server — write
        # protection is enforced at runtime (QueryOptions(readonly=True)),
        # not by omitting them here.
        oi_run_query_sync,
        oi_run_query_async,
        # Pure reads/cleanup of an already-gated handle — no runtime
        # read-only logic needed.
        oi_get_async_query_results,
        oi_discard_async_query_results,
        # Cancelling mutates no data: it releases server resources the caller
        # itself allocated with oi_run_query_async. Classified read-only for
        # the same reason oi_discard_async_query_results is — both end a query
        # the caller started, and neither touches stored data.
        #
        # It was previously a write tool on the grounds that interrupting an
        # in-flight query is "more consequential" than discarding a finished
        # one. That reasoning measures the wrong axis: read-only mode exists
        # to prevent *mutation*, and consequence is already communicated to
        # clients by destructiveHint=True in TOOL_ANNOTATIONS. The practical
        # cost of the old classification was a dead end — under
        # --read-only-mode (the default) oi_run_query_async was registered but
        # oi_cancel_async_query was not, so a caller could start a long query
        # and have no way to stop it, while oi_discard_async_query_results
        # answered an in-flight handle by recommending a tool that was not
        # loaded.
        oi_cancel_async_query,
    ),
    write=(oi_create_index,),
)

# Derived views, kept for parity with the operational tools package.
READ_ONLY_TOOLS = list(TOOL_SET.read_only)
WRITE_TOOLS = list(TOOL_SET.write)
ALL_TOOLS = TOOL_SET.all_tools

# Tool annotations for MCP clients (readOnlyHint, destructiveHint, etc.)
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_server_configuration_status": ToolAnnotations(readOnlyHint=True),
    "oi_get_databases_in_cluster": ToolAnnotations(readOnlyHint=True),
    "oi_get_scopes_in_database": ToolAnnotations(readOnlyHint=True),
    "oi_get_collections_in_scope": ToolAnnotations(readOnlyHint=True),
    "oi_get_schema_for_collection": ToolAnnotations(readOnlyHint=True),
    "oi_list_indexes": ToolAnnotations(readOnlyHint=True),
    "oi_explain_query": ToolAnnotations(readOnlyHint=True),
    # oi_run_query_sync/oi_run_query_async can carry DDL/DML, so they get no
    # readOnlyHint (matches run_sql_plus_plus_query on the operational
    # server). Their copy_to_* arguments write to external storage, which is
    # the same classification for the same reason — no change needed here.
    "oi_run_query_sync": ToolAnnotations(),
    "oi_run_query_async": ToolAnnotations(),
    "oi_get_async_query_results": ToolAnnotations(readOnlyHint=True),
    # The annotation is about destructive *effect*, independent of which
    # registration bucket each tool ends up in: both free/interrupt
    # server-side state even though only one is write-gated.
    "oi_discard_async_query_results": ToolAnnotations(destructiveHint=True),
    "oi_cancel_async_query": ToolAnnotations(destructiveHint=True),
    # oi_create_index issues DDL (matches create_index on the operational
    # server).
    "oi_create_index": ToolAnnotations(),
}

# Per-tool scope-denial hints. Kept in this package rather than the shared
# cb_mcp.utils.scope_enforcement module (where the operational server's hints
# live) so this server needs no shared-file changes.
TOOL_SCOPE_HINTS: dict[str, str] = {
    "oi_run_query_sync": (
        f"A '{SCOPE_WRITE}'-only token cannot invoke SQL++; '{SCOPE_READ}' is required."
    ),
    "oi_run_query_async": (
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
    "get_server_configuration_status",
    "get_tools",
    "oi_cancel_async_query",
    "oi_create_index",
    "oi_discard_async_query_results",
    "oi_explain_query",
    "oi_get_async_query_results",
    "oi_get_collections_in_scope",
    "oi_get_databases_in_cluster",
    "oi_get_schema_for_collection",
    "oi_get_scopes_in_database",
    "oi_list_indexes",
    "oi_run_query_async",
    "oi_run_query_sync",
]
