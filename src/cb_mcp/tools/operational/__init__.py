"""
Couchbase MCP Tools

This module contains all the MCP tools for Couchbase operations.

Tool Categories:
- READ_ONLY_TOOLS: Tools that only read data (always available)
- WRITE_TOOLS: Tools that modify data (disabled when READ_ONLY_MODE=True)
"""

from collections.abc import Callable

from mcp.types import ToolAnnotations

from ...core.spec import ToolSet
from ...utils.constants import SCOPE_READ, SCOPE_WRITE

# Shared with every other server, and registered by them too — the same
# function object, not a per-server reimplementation. See cb_mcp/tools/status.py.
from ..status import get_server_configuration_status

# Scope/collection management tools
from .collection_management import (
    create_collection,
    create_scope,
    delete_collection,
    delete_scope,
)

# FTS tools
from .fts import (
    get_fts_index_definition,
    list_fts_indexes,
    run_fts_query,
)

# Index tools
from .index import (
    build_index,
    create_index,
    drop_index,
    get_index_advisor_recommendations,
    list_indexes,
)

# Key-Value tools
from .kv import (
    delete_document_by_id,
    get_document_by_id,
    insert_document_by_id,
    lookup_subdocument,
    mutate_subdocument,
    replace_document_by_id,
    upsert_document_by_id,
)

# Query tools
from .query import (
    explain_sql_plus_plus_query,
    get_longest_running_queries,
    get_most_frequent_queries,
    get_queries_not_selective,
    get_queries_not_using_covering_index,
    get_queries_using_primary_index,
    get_queries_with_large_result_count,
    get_queries_with_largest_response_sizes,
    get_schema_for_collection,
    run_sql_plus_plus_query,
)

# Reference data tools
from .reference import (
    discover_tool_input_values,
)

# Server tools
from .server import (
    get_buckets_in_cluster,
    get_cluster_diagnostics_report,
    get_cluster_health_and_services,
    get_cluster_metrics,
    get_collections_in_scope,
    get_scopes_and_collections_in_bucket,
    get_scopes_in_bucket,
    test_cluster_connection,
)

# The operational server's tool inventory, and the single source of truth for
# it. The module-level lists below are derived from this; prefer TOOL_SET in
# new code, and see cb_mcp.core.spec for why the inventory is declared as data.
TOOL_SET = ToolSet(
    # Read-only tools - always available regardless of mode settings
    read_only=(
        # Server/Cluster management tools
        get_buckets_in_cluster,
        get_server_configuration_status,
        test_cluster_connection,
        get_scopes_and_collections_in_bucket,
        get_collections_in_scope,
        get_scopes_in_bucket,
        get_cluster_health_and_services,
        get_cluster_diagnostics_report,
        get_cluster_metrics,
        # KV read tools
        get_document_by_id,
        lookup_subdocument,
        # Query tools (read operations)
        get_schema_for_collection,
        run_sql_plus_plus_query,  # Write protection handled at runtime via read_only_mode
        explain_sql_plus_plus_query,
        # Index tools
        get_index_advisor_recommendations,
        list_indexes,
        # FTS tools
        list_fts_indexes,
        get_fts_index_definition,
        run_fts_query,
        # Query performance analysis tools
        get_queries_not_selective,
        get_queries_not_using_covering_index,
        get_queries_using_primary_index,
        get_queries_with_large_result_count,
        get_queries_with_largest_response_sizes,
        get_longest_running_queries,
        get_most_frequent_queries,
        # Reference data tools
        discover_tool_input_values,
    ),
    # Write tools - disabled when READ_ONLY_MODE is True
    write=(
        # KV write tools
        upsert_document_by_id,
        insert_document_by_id,
        replace_document_by_id,
        delete_document_by_id,
        mutate_subdocument,
        # Scope/collection management write tools
        create_scope,
        create_collection,
        delete_scope,
        delete_collection,
        # Index write tools
        create_index,
        build_index,
        drop_index,
    ),
)

# Derived views, kept for backward compatibility with existing importers.
READ_ONLY_TOOLS = list(TOOL_SET.read_only)
WRITE_TOOLS = list(TOOL_SET.write)
ALL_TOOLS = TOOL_SET.all_tools

# Tool annotations for MCP clients (readOnlyHint, destructiveHint, etc.)
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    # Server/Cluster management tools (read-only)
    "get_server_configuration_status": ToolAnnotations(readOnlyHint=True),
    "test_cluster_connection": ToolAnnotations(readOnlyHint=True),
    "get_buckets_in_cluster": ToolAnnotations(readOnlyHint=True),
    "get_scopes_and_collections_in_bucket": ToolAnnotations(readOnlyHint=True),
    "get_collections_in_scope": ToolAnnotations(readOnlyHint=True),
    "get_scopes_in_bucket": ToolAnnotations(readOnlyHint=True),
    "get_cluster_health_and_services": ToolAnnotations(readOnlyHint=True),
    "get_cluster_diagnostics_report": ToolAnnotations(readOnlyHint=True),
    "get_cluster_metrics": ToolAnnotations(readOnlyHint=True),
    # KV read tools
    "get_document_by_id": ToolAnnotations(readOnlyHint=True),
    "lookup_subdocument": ToolAnnotations(readOnlyHint=True),
    # Query tools
    "get_schema_for_collection": ToolAnnotations(readOnlyHint=True),
    "run_sql_plus_plus_query": ToolAnnotations(),
    "explain_sql_plus_plus_query": ToolAnnotations(readOnlyHint=True),
    # Index tools (read-only)
    "get_index_advisor_recommendations": ToolAnnotations(readOnlyHint=True),
    "list_indexes": ToolAnnotations(readOnlyHint=True),
    # FTS tools (read-only)
    "list_fts_indexes": ToolAnnotations(readOnlyHint=True),
    "get_fts_index_definition": ToolAnnotations(readOnlyHint=True),
    "run_fts_query": ToolAnnotations(readOnlyHint=True),
    # Query performance analysis tools (read-only)
    "get_longest_running_queries": ToolAnnotations(readOnlyHint=True),
    "get_most_frequent_queries": ToolAnnotations(readOnlyHint=True),
    "get_queries_with_largest_response_sizes": ToolAnnotations(readOnlyHint=True),
    "get_queries_with_large_result_count": ToolAnnotations(readOnlyHint=True),
    "get_queries_using_primary_index": ToolAnnotations(readOnlyHint=True),
    "get_queries_not_using_covering_index": ToolAnnotations(readOnlyHint=True),
    "get_queries_not_selective": ToolAnnotations(readOnlyHint=True),
    # Reference data tools (read-only, no cluster access at all)
    "discover_tool_input_values": ToolAnnotations(readOnlyHint=True),
    # KV write tools
    "upsert_document_by_id": ToolAnnotations(idempotentHint=True),
    "insert_document_by_id": ToolAnnotations(idempotentHint=True),
    "replace_document_by_id": ToolAnnotations(idempotentHint=True),
    "delete_document_by_id": ToolAnnotations(destructiveHint=True, idempotentHint=True),
    "mutate_subdocument": ToolAnnotations(destructiveHint=True),
    # Scope/collection management write tools
    "create_scope": ToolAnnotations(),
    "create_collection": ToolAnnotations(),
    "delete_scope": ToolAnnotations(destructiveHint=True),
    "delete_collection": ToolAnnotations(destructiveHint=True),
    # Index write tools
    "create_index": ToolAnnotations(),
    "build_index": ToolAnnotations(idempotentHint=True),
    "drop_index": ToolAnnotations(destructiveHint=True),
}

# Per-tool explanations appended to a scope-denial error, reaching the
# enforcement layer via ``ServerSpec.scope_hints``. Use these where the
# literal "missing X" line under-explains *why* a tool needs the scope it
# does; tools not listed here get the generic message.
#
# Kept beside the tools rather than in ``cb_mcp.utils.scope_enforcement``,
# where they used to live: that module is shared by every server, and a
# hard-coded ``run_sql_plus_plus_query`` there made it quietly specific to
# this one. The Operational Insights server already declares its hints this
# way.
TOOL_SCOPE_HINTS: dict[str, str] = {
    "run_sql_plus_plus_query": (
        f"A '{SCOPE_WRITE}'-only token cannot invoke SQL++; '{SCOPE_READ}' is required."
    ),
}


def get_tools(read_only_mode: bool = True) -> list[Callable]:
    """Get the list of tools based on the mode settings.

    This function determines which tools should be loaded based on the
    READ_ONLY_MODE setting. When read_only_mode is True, write tools are excluded.
    """
    return TOOL_SET.tools_for(read_only_mode=read_only_mode)


__all__ = [
    # Individual tools
    "get_server_configuration_status",
    "test_cluster_connection",
    "get_scopes_and_collections_in_bucket",
    "get_collections_in_scope",
    "get_scopes_in_bucket",
    "get_buckets_in_cluster",
    "get_document_by_id",
    "lookup_subdocument",
    "mutate_subdocument",
    "upsert_document_by_id",
    "insert_document_by_id",
    "replace_document_by_id",
    "delete_document_by_id",
    "create_scope",
    "create_collection",
    "delete_scope",
    "delete_collection",
    "get_schema_for_collection",
    "run_sql_plus_plus_query",
    "explain_sql_plus_plus_query",
    "get_index_advisor_recommendations",
    "list_indexes",
    "create_index",
    "build_index",
    "drop_index",
    "list_fts_indexes",
    "get_fts_index_definition",
    "run_fts_query",
    "get_cluster_health_and_services",
    "get_cluster_diagnostics_report",
    "get_cluster_metrics",
    "get_queries_not_selective",
    "get_queries_not_using_covering_index",
    "get_queries_using_primary_index",
    "get_queries_with_large_result_count",
    "get_queries_with_largest_response_sizes",
    "get_longest_running_queries",
    "get_most_frequent_queries",
    "discover_tool_input_values",
    # Tool inventory
    "TOOL_SET",
    # Tool categories
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
    # Tool annotations
    "TOOL_ANNOTATIONS",
    "TOOL_SCOPE_HINTS",
    # Convenience
    "ALL_TOOLS",
    "get_tools",
]
