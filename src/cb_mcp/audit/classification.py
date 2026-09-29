"""Tool classification — the catalogue's extension point.

The audit ID space is frozen, so adding a tool requires only a new entry in
:data:`TOOL_CLASSIFICATION`. No new event ID, no registry allocation, no
SIEM-rule change.

``operation_class`` is authoritative at the MCP layer: a tool is classified by
what it is allowed to do. That single classification drives both the event ID
(read versus write range within the package block) and ``required_scope``,
keeping the two consistent by construction.

A tool with no entry **fails closed** — recorded against a write ID, which is
never filterable — so a newly added tool can never be silently under-audited.
Such a record also carries ``unclassified: true`` so a reviewer is never misled
about the category, and :func:`unclassified_tool_names` lets startup report the
gap rather than leaving it to be discovered from an audit file months later.

Entries marked ``PENDING PRD`` are classified here from the repository's own
categorisation (the ``*_WRITE_TOOLS`` lists in :mod:`cb_mcp.tools` and the
``readOnlyHint`` annotations) because they ship today while the PRD's
example-tool tables have not yet been updated to name them. The classification
is not in doubt; the PRD text is behind. Re-verify when those rows land.
"""

from __future__ import annotations

from collections.abc import Iterable

from .catalog import ToolCallEvent, tool_call_event

#: ``tool_name -> (category, operation_class)``.
#:
#: Ordered by category to match the catalogue layout rather than
#: alphabetically, so a reviewer can check a whole block in one pass.
TOOL_CLASSIFICATION: dict[str, tuple[str, str]] = {
    # -- cluster / health -------------------------------------------------
    "get_server_configuration_status": ("cluster", "read"),
    "test_cluster_connection": ("cluster", "read"),
    "get_cluster_health_and_services": ("cluster", "read"),
    "get_cluster_diagnostics_report": ("cluster", "read"),  # PENDING PRD
    # -- schema / discovery ----------------------------------------------
    "get_buckets_in_cluster": ("schema", "read"),
    "get_scopes_in_bucket": ("schema", "read"),
    "get_collections_in_scope": ("schema", "read"),
    "get_scopes_and_collections_in_bucket": ("schema", "read"),
    "get_schema_for_collection": ("schema", "read"),
    "create_scope": ("schema", "write"),  # PENDING PRD
    "create_collection": ("schema", "write"),  # PENDING PRD
    "delete_scope": ("schema", "write"),  # PENDING PRD
    "delete_collection": ("schema", "write"),  # PENDING PRD
    # -- kv (document) ----------------------------------------------------
    "get_document_by_id": ("kv", "read"),
    "lookup_subdocument": ("kv", "read"),  # PENDING PRD
    "upsert_document_by_id": ("kv", "write"),
    "insert_document_by_id": ("kv", "write"),
    "replace_document_by_id": ("kv", "write"),
    "delete_document_by_id": ("kv", "write"),
    "mutate_subdocument": ("kv", "write"),  # PENDING PRD
    # -- query (SQL++) ----------------------------------------------------
    # run_sql_plus_plus_query is re-classified per statement at invocation; the
    # entry here is the class used when the statement was not inspected.
    "run_sql_plus_plus_query": ("query", "read"),
    "explain_sql_plus_plus_query": ("query", "read"),
    # -- index ------------------------------------------------------------
    "list_indexes": ("index", "read"),
    "get_index_advisor_recommendations": ("index", "read"),
    "create_index": ("index", "write"),  # PENDING PRD
    "build_index": ("index", "write"),  # PENDING PRD
    "drop_index": ("index", "write"),  # PENDING PRD
    # -- performance ------------------------------------------------------
    "get_longest_running_queries": ("performance", "read"),
    "get_most_frequent_queries": ("performance", "read"),
    "get_queries_with_largest_response_sizes": ("performance", "read"),
    "get_queries_with_large_result_count": ("performance", "read"),
    "get_queries_using_primary_index": ("performance", "read"),
    "get_queries_not_using_covering_index": ("performance", "read"),
    "get_queries_not_selective": ("performance", "read"),
}

#: The one tool whose read/write class depends on the statement it is given.
STATEMENT_CLASSIFIED_TOOL = "run_sql_plus_plus_query"

#: Fail-closed operation class for a tool with no entry. Write IDs are never
#: filterable, so an unknown tool is always recorded.
FAILSAFE_OPERATION_CLASS = "write"

#: Category used for the ID of an otherwise unknown tool. The record also
#: carries ``unclassified: true`` so this placeholder is never mistaken for a
#: real categorisation.
FAILSAFE_CATEGORY = "cluster"


def is_classified(tool_name: str) -> bool:
    """True when ``tool_name`` has an explicit classification entry."""
    return tool_name in TOOL_CLASSIFICATION


def classify_tool(
    tool_name: str, *, operation_class_override: str | None = None
) -> tuple[str, str]:
    """Return ``(category, operation_class)`` for ``tool_name``.

    Args:
        tool_name: The MCP tool name as registered.
        operation_class_override: Supplied for statement-classified tools once
            the statement has been inspected, so a SQL++ DML call is booked
            against the query *write* ID rather than the read ID.

    Unknown tools fail closed to a write class.
    """
    category, operation_class = TOOL_CLASSIFICATION.get(
        tool_name, (FAILSAFE_CATEGORY, FAILSAFE_OPERATION_CLASS)
    )
    if operation_class_override is not None:
        operation_class = operation_class_override
    return category, operation_class


def resolve_tool_call_event(
    tool_name: str, *, operation_class_override: str | None = None
) -> ToolCallEvent:
    """Resolve the catalogue entry for a tool invocation."""
    category, operation_class = classify_tool(
        tool_name, operation_class_override=operation_class_override
    )
    return tool_call_event(category, operation_class)


def required_scope_for(operation_class: str) -> str:
    """Map an operation class onto the scope label recorded in the audit line.

    Deliberately the short form (``read`` / ``write``) rather than the full
    ``couchbase-mcp:write`` scope string, matching the PRD's sample records.
    """
    return "write" if operation_class == "write" else "read"


def unclassified_tool_names(tool_names: Iterable[str]) -> list[str]:
    """Return the subset of ``tool_names`` with no classification entry."""
    return sorted(name for name in tool_names if name not in TOOL_CLASSIFICATION)


__all__ = [
    "FAILSAFE_CATEGORY",
    "FAILSAFE_OPERATION_CLASS",
    "STATEMENT_CLASSIFIED_TOOL",
    "TOOL_CLASSIFICATION",
    "classify_tool",
    "is_classified",
    "required_scope_for",
    "resolve_tool_call_event",
    "unclassified_tool_names",
]
