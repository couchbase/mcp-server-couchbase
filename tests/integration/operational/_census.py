"""The operational server's tool census.

What ``operational`` is expected to register, and with which required
parameters — the fixture behind ``test_mcp_integration.py``'s
``test_all_expected_tools_registered`` / ``test_no_unexpected_tools``.

Lives beside the tests that use it rather than in
``tests/integration/conftest.py``, where it used to sit above the generic
session plumbing. That mattered once a second server existed: every
Operational Insights test imports that conftest for the session helpers, and
inherited ~170 lines describing a tool set its server does not have. The
Operational Insights server keeps its own, much smaller census in
``tests/integration/operational_insights/test_oi_mcp_integration.py``.
"""

# Tools we expect to be registered by the server
EXPECTED_TOOLS = {
    "get_buckets_in_cluster",
    "get_server_configuration_status",
    "test_cluster_connection",
    "get_scopes_and_collections_in_bucket",
    "get_collections_in_scope",
    "get_scopes_in_bucket",
    "get_document_by_id",
    "lookup_subdocument",
    "mutate_subdocument",
    "upsert_document_by_id",
    "insert_document_by_id",
    "replace_document_by_id",
    "delete_document_by_id",
    # Scope/collection management (write) tools
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
    # FTS tools
    "list_fts_indexes",
    "get_fts_index_definition",
    "run_fts_query",
    "get_cluster_health_and_services",
    "get_cluster_diagnostics_report",
    "get_cluster_metrics",
    # Performance analysis tools
    "get_longest_running_queries",
    "get_most_frequent_queries",
    "get_queries_with_largest_response_sizes",
    "get_queries_with_large_result_count",
    "get_queries_using_primary_index",
    "get_queries_not_using_covering_index",
    "get_queries_not_selective",
    # Reference data tools
    "discover_tool_input_values",
}

# Tools organized by category for validation
TOOLS_BY_CATEGORY = {
    "server": {
        "get_server_configuration_status",
        "test_cluster_connection",
        "get_buckets_in_cluster",
        "get_scopes_in_bucket",
        "get_scopes_and_collections_in_bucket",
        "get_collections_in_scope",
        "get_cluster_health_and_services",
        "get_cluster_diagnostics_report",
        "get_cluster_metrics",
    },
    "kv": {
        "get_document_by_id",
        "lookup_subdocument",
        "mutate_subdocument",
        "upsert_document_by_id",
        "insert_document_by_id",
        "replace_document_by_id",
        "delete_document_by_id",
    },
    "query": {
        "get_schema_for_collection",
        "run_sql_plus_plus_query",
        "explain_sql_plus_plus_query",
    },
    "index": {
        "list_indexes",
        "get_index_advisor_recommendations",
        "create_index",
        "build_index",
        "drop_index",
    },
    "fts": {
        "list_fts_indexes",
        "get_fts_index_definition",
        "run_fts_query",
    },
    "management": {
        "create_scope",
        "create_collection",
        "delete_scope",
        "delete_collection",
    },
    "performance": {
        "get_longest_running_queries",
        "get_most_frequent_queries",
        "get_queries_with_largest_response_sizes",
        "get_queries_with_large_result_count",
        "get_queries_using_primary_index",
        "get_queries_not_using_covering_index",
        "get_queries_not_selective",
    },
    "reference": {
        "discover_tool_input_values",
    },
}

# Expected required parameters for tools that need them
TOOL_REQUIRED_PARAMS = {
    # tool_name is the only required argument -- omitting search_keywords is browse mode.
    "discover_tool_input_values": ["tool_name"],
    "get_scopes_in_bucket": ["bucket_name"],
    "get_scopes_and_collections_in_bucket": ["bucket_name"],
    "get_collections_in_scope": ["bucket_name", "scope_name"],
    "get_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
    ],
    "upsert_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
        "document_content",
    ],
    "delete_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
    ],
    "insert_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
        "document_content",
    ],
    "replace_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
        "document_content",
    ],
    "get_schema_for_collection": ["bucket_name", "scope_name", "collection_name"],
    "run_sql_plus_plus_query": ["bucket_name", "scope_name", "query"],
    "explain_sql_plus_plus_query": ["bucket_name", "scope_name", "query"],
    "get_index_advisor_recommendations": ["bucket_name", "scope_name", "query"],
    "create_scope": ["bucket_name", "scope_name"],
    "create_collection": ["bucket_name", "scope_name", "collection_name"],
    "delete_scope": ["bucket_name", "scope_name"],
    "delete_collection": ["bucket_name", "scope_name", "collection_name"],
    "create_index": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "index_name",
        "keys",
    ],
    "build_index": ["bucket_name", "scope_name", "collection_name"],
    "drop_index": ["bucket_name", "scope_name", "collection_name", "index_name"],
    "get_fts_index_definition": ["index_name"],
    "run_fts_query": ["index_name", "query"],
}
