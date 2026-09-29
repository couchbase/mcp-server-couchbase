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

from .catalog import DEFAULT_SERVICE_PACKAGE, ToolCallEvent, tool_call_event

#: ``service package -> {tool_name -> (category, operation_class)}``.
#:
#: Keyed by service package, not by bare tool name. The repository ships two
#: servers and they genuinely collide on five names — ``create_index``,
#: ``list_indexes``, ``get_collections_in_scope``, ``get_schema_for_collection``
#: and ``get_server_configuration_status``. A flat table cannot represent both,
#: and booking one server's tool against another's block would put records in a
#: block whose ids mean something else.
#:
#: Only ``operational`` is populated today; the Operational Insights server is
#: not audited (see :data:`cb_mcp.audit.catalog.DEFAULT_SERVICE_PACKAGE`).
#: Adding it later means a table here and a block in
#: :data:`~cb_mcp.audit.catalog.SERVICE_PACKAGE_BLOCKS`, and renumbers nothing.
#:
#: Within a package, ordered by category to match the catalogue layout rather
#: than alphabetically, so a reviewer can check a whole block in one pass.
TOOL_CLASSIFICATION: dict[str, dict[str, tuple[str, str]]] = {
    "operational": {
        # -- cluster / health ---------------------------------------------
        "get_server_configuration_status": ("cluster", "read"),
        "test_cluster_connection": ("cluster", "read"),
        "get_cluster_health_and_services": ("cluster", "read"),
        "get_cluster_diagnostics_report": ("cluster", "read"),  # PENDING PRD
        "get_cluster_metrics": ("cluster", "read"),  # PENDING PRD
        # Reads bundled reference data rather than the cluster, so it touches
        # no keyspace and has no service of its own. Booked as a cluster read:
        # it is a server-level informational call, which is what that category
        # already covers.
        "discover_tool_input_values": ("cluster", "read"),  # PENDING PRD
        # -- schema / discovery --------------------------------------------
        "get_buckets_in_cluster": ("schema", "read"),
        "get_scopes_in_bucket": ("schema", "read"),
        "get_collections_in_scope": ("schema", "read"),
        "get_scopes_and_collections_in_bucket": ("schema", "read"),
        "get_schema_for_collection": ("schema", "read"),
        "create_scope": ("schema", "write"),  # PENDING PRD
        "create_collection": ("schema", "write"),  # PENDING PRD
        "delete_scope": ("schema", "write"),  # PENDING PRD
        "delete_collection": ("schema", "write"),  # PENDING PRD
        # -- kv (document) --------------------------------------------------
        "get_document_by_id": ("kv", "read"),
        "lookup_subdocument": ("kv", "read"),  # PENDING PRD
        "upsert_document_by_id": ("kv", "write"),
        "insert_document_by_id": ("kv", "write"),
        "replace_document_by_id": ("kv", "write"),
        "delete_document_by_id": ("kv", "write"),
        "mutate_subdocument": ("kv", "write"),  # PENDING PRD
        # -- query (SQL++) ---------------------------------------------------
        # run_sql_plus_plus_query is re-classified per statement at invocation;
        # the entry here is the class used when the statement was not inspected.
        "run_sql_plus_plus_query": ("query", "read"),
        "explain_sql_plus_plus_query": ("query", "read"),
        # -- index -----------------------------------------------------------
        "list_indexes": ("index", "read"),
        "get_index_advisor_recommendations": ("index", "read"),
        "get_index_stats": ("index", "read"),  # PENDING PRD
        "create_index": ("index", "write"),  # PENDING PRD
        "build_index": ("index", "write"),  # PENDING PRD
        "drop_index": ("index", "write"),  # PENDING PRD
        # -- performance -----------------------------------------------------
        "get_longest_running_queries": ("performance", "read"),
        "get_most_frequent_queries": ("performance", "read"),
        "get_queries_with_largest_response_sizes": ("performance", "read"),
        "get_queries_with_large_result_count": ("performance", "read"),
        "get_queries_using_primary_index": ("performance", "read"),
        "get_queries_not_using_covering_index": ("performance", "read"),
        "get_queries_not_selective": ("performance", "read"),
        # -- search (FTS) ------------------------------------------------------
        # Its own category rather than folded into index: run_fts_query is a
        # query, so a reviewer filtering on `index read` should not get search
        # traffic mixed in. All three tools ship read-only; the search *write*
        # id (61528) exists in the catalogue for when FTS index management
        # lands, so adding it will need no new id.
        "list_fts_indexes": ("search", "read"),  # PENDING PRD
        "get_fts_index_definition": ("search", "read"),  # PENDING PRD
        "run_fts_query": ("search", "read"),  # PENDING PRD
    },
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


def classification_for(
    package: str = DEFAULT_SERVICE_PACKAGE,
) -> dict[str, tuple[str, str]]:
    """Return ``package``'s classification table, or an empty one if unaudited."""
    return TOOL_CLASSIFICATION.get(package, {})


def is_classified(tool_name: str, package: str = DEFAULT_SERVICE_PACKAGE) -> bool:
    """True when ``tool_name`` has an explicit entry in ``package``'s table."""
    return tool_name in classification_for(package)


def classify_tool(
    tool_name: str,
    *,
    package: str = DEFAULT_SERVICE_PACKAGE,
    operation_class_override: str | None = None,
) -> tuple[str, str]:
    """Return ``(category, operation_class)`` for ``tool_name``.

    Args:
        tool_name: The MCP tool name as registered.
        operation_class_override: Supplied for statement-classified tools once
            the statement has been inspected, so a SQL++ DML call is booked
            against the query *write* ID rather than the read ID.

    Unknown tools fail closed to a write class.
    """
    category, operation_class = classification_for(package).get(
        tool_name, (FAILSAFE_CATEGORY, FAILSAFE_OPERATION_CLASS)
    )
    if operation_class_override is not None:
        operation_class = operation_class_override
    return category, operation_class


def resolve_tool_call_event(
    tool_name: str,
    *,
    package: str = DEFAULT_SERVICE_PACKAGE,
    operation_class_override: str | None = None,
) -> ToolCallEvent:
    """Resolve the catalogue entry for a tool invocation in ``package``."""
    category, operation_class = classify_tool(
        tool_name,
        package=package,
        operation_class_override=operation_class_override,
    )
    return tool_call_event(category, operation_class, package)


def required_scope_for(operation_class: str) -> str:
    """Map an operation class onto the scope label recorded in the audit line.

    Deliberately the short form (``read`` / ``write``) rather than the full
    ``couchbase-mcp:write`` scope string, matching the PRD's sample records.
    """
    return "write" if operation_class == "write" else "read"


def unclassified_tool_names(
    tool_names: Iterable[str], package: str = DEFAULT_SERVICE_PACKAGE
) -> list[str]:
    """Return the subset of ``tool_names`` with no entry in ``package``'s table."""
    table = classification_for(package)
    return sorted(name for name in tool_names if name not in table)


__all__ = [
    "FAILSAFE_CATEGORY",
    "classification_for",
    "FAILSAFE_OPERATION_CLASS",
    "STATEMENT_CLASSIFIED_TOOL",
    "TOOL_CLASSIFICATION",
    "classify_tool",
    "is_classified",
    "required_scope_for",
    "resolve_tool_call_event",
    "unclassified_tool_names",
]
