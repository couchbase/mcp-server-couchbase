"""
Tests for the READ_ONLY_MODE functionality.

This module tests:
- Tool filtering based on READ_ONLY_MODE setting
- The get_tools() function behavior according to the truth table
- Verification that KV write tools are not loaded when READ_ONLY_MODE=True
"""

from cb_mcp.tools import (
    ALL_TOOLS,
    COLLECTION_WRITE_TOOLS,
    INDEX_WRITE_TOOLS,
    KV_WRITE_TOOLS,
    READ_ONLY_TOOLS,
    get_tools,
)
from cb_mcp.utils.constants import DEFAULT_READ_ONLY_MODE
from cb_mcp.utils.context import AppContext

# KV write tool names that should be disabled when READ_ONLY_MODE=True
KV_WRITE_TOOL_NAMES = {
    "upsert_document_by_id",
    "insert_document_by_id",
    "replace_document_by_id",
    "delete_document_by_id",
    "mutate_subdocument",
}

# Scope/collection management write tool names that should be disabled when
# READ_ONLY_MODE=True
COLLECTION_WRITE_TOOL_NAMES = {
    "create_scope",
    "create_collection",
    "delete_scope",
    "delete_collection",
}

# Index management write tool names that should be disabled when READ_ONLY_MODE=True
INDEX_WRITE_TOOL_NAMES = {
    "create_index",
    "build_index",
    "drop_index",
}

# Read-only tool names that should always be available (21 tools)
READ_ONLY_TOOL_NAMES = {
    # Server/Cluster management tools (7)
    "get_buckets_in_cluster",
    "get_server_configuration_status",
    "test_cluster_connection",
    "get_scopes_and_collections_in_bucket",
    "get_collections_in_scope",
    "get_scopes_in_bucket",
    "get_cluster_health_and_services",
    # KV read tools (2)
    "get_document_by_id",
    "lookup_subdocument",
    # Query tools (3)
    "get_schema_for_collection",
    "run_sql_plus_plus_query",
    "explain_sql_plus_plus_query",
    # Index tools (2)
    "get_index_advisor_recommendations",
    "list_indexes",
    # Query performance analysis tools (7)
    "get_queries_not_selective",
    "get_queries_not_using_covering_index",
    "get_queries_using_primary_index",
    "get_queries_with_large_result_count",
    "get_queries_with_largest_response_sizes",
    "get_longest_running_queries",
    "get_most_frequent_queries",
}


class TestToolCategories:
    """Tests for tool category definitions."""

    def test_read_only_tools_defined(self):
        """Verify READ_ONLY_TOOLS list is properly defined."""
        assert len(READ_ONLY_TOOLS) > 0
        tool_names = {tool.__name__ for tool in READ_ONLY_TOOLS}
        assert tool_names == READ_ONLY_TOOL_NAMES

    def test_kv_write_tools_defined(self):
        """Verify KV_WRITE_TOOLS list is properly defined."""
        assert len(KV_WRITE_TOOLS) == 5
        tool_names = {tool.__name__ for tool in KV_WRITE_TOOLS}
        assert tool_names == KV_WRITE_TOOL_NAMES

    def test_collection_write_tools_defined(self):
        """Verify COLLECTION_WRITE_TOOLS list is properly defined."""
        assert len(COLLECTION_WRITE_TOOLS) == 4
        tool_names = {tool.__name__ for tool in COLLECTION_WRITE_TOOLS}
        assert tool_names == COLLECTION_WRITE_TOOL_NAMES

    def test_index_write_tools_defined(self):
        """Verify INDEX_WRITE_TOOLS list is properly defined."""
        assert len(INDEX_WRITE_TOOLS) == 3
        tool_names = {tool.__name__ for tool in INDEX_WRITE_TOOLS}
        assert tool_names == INDEX_WRITE_TOOL_NAMES

    def test_all_tools_is_union(self):
        """Verify ALL_TOOLS is the union of READ_ONLY_TOOLS, KV_WRITE_TOOLS,
        COLLECTION_WRITE_TOOLS, and INDEX_WRITE_TOOLS."""
        expected_count = (
            len(READ_ONLY_TOOLS)
            + len(KV_WRITE_TOOLS)
            + len(COLLECTION_WRITE_TOOLS)
            + len(INDEX_WRITE_TOOLS)
        )
        assert len(ALL_TOOLS) == expected_count

        all_tool_names = {tool.__name__ for tool in ALL_TOOLS}
        expected_names = (
            READ_ONLY_TOOL_NAMES
            | KV_WRITE_TOOL_NAMES
            | COLLECTION_WRITE_TOOL_NAMES
            | INDEX_WRITE_TOOL_NAMES
        )
        assert all_tool_names == expected_names

    def test_no_overlap_between_categories(self):
        """Verify there's no overlap between READ_ONLY_TOOLS, KV_WRITE_TOOLS,
        COLLECTION_WRITE_TOOLS, and INDEX_WRITE_TOOLS."""
        read_only_names = {tool.__name__ for tool in READ_ONLY_TOOLS}
        kv_write_names = {tool.__name__ for tool in KV_WRITE_TOOLS}
        collection_write_names = {tool.__name__ for tool in COLLECTION_WRITE_TOOLS}
        index_write_names = {tool.__name__ for tool in INDEX_WRITE_TOOLS}
        assert read_only_names & kv_write_names == set()
        assert read_only_names & collection_write_names == set()
        assert read_only_names & index_write_names == set()
        assert kv_write_names & collection_write_names == set()
        assert kv_write_names & index_write_names == set()
        assert collection_write_names & index_write_names == set()


class TestGetToolsTruthTable:
    """Tests for get_tools() function.

    Tool Loading Behavior:
    | READ_ONLY_MODE | KV Write Tools Loaded |
    |----------------|-----------------------|
    | True           | No                    |
    | False          | Yes                   |

    Note: SQL++ query write blocking is handled at runtime by the query tool
    itself, not at tool loading time.
    """

    def test_read_only_mode_true(self):
        """READ_ONLY_MODE=True: No write tools."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}

        # Should only have read-only tools
        assert tool_names == READ_ONLY_TOOL_NAMES

        # Write tools (KV + collection management) should NOT be present
        for write_name in KV_WRITE_TOOL_NAMES | COLLECTION_WRITE_TOOL_NAMES:
            assert write_name not in tool_names

        # Index write tools should NOT be present
        for index_write_name in INDEX_WRITE_TOOL_NAMES:
            assert index_write_name not in tool_names

    def test_read_only_mode_false(self):
        """READ_ONLY_MODE=False: All tools loaded including write tools."""
        tools = get_tools(read_only_mode=False)
        tool_names = {tool.__name__ for tool in tools}

        # Should have all tools (read-only + KV write + collection write + index write)
        expected_names = (
            READ_ONLY_TOOL_NAMES
            | KV_WRITE_TOOL_NAMES
            | COLLECTION_WRITE_TOOL_NAMES
            | INDEX_WRITE_TOOL_NAMES
        )
        assert tool_names == expected_names

        # Write tools should be present
        for write_name in KV_WRITE_TOOL_NAMES | COLLECTION_WRITE_TOOL_NAMES:
            assert write_name in tool_names

        # Index write tools should be present
        for index_write_name in INDEX_WRITE_TOOL_NAMES:
            assert index_write_name in tool_names


class TestGetToolsDefaults:
    """Tests for get_tools() default parameter values."""

    def test_default_is_read_only(self):
        """Verify default behavior is read-only (no KV or index write tools)."""
        tools = get_tools()  # Using defaults
        tool_names = {tool.__name__ for tool in tools}

        # Default should be read-only mode
        assert tool_names == READ_ONLY_TOOL_NAMES

        # Write tools should NOT be present by default
        for write_name in KV_WRITE_TOOL_NAMES | COLLECTION_WRITE_TOOL_NAMES:
            assert write_name not in tool_names

        # Index write tools should NOT be present by default
        for index_write_name in INDEX_WRITE_TOOL_NAMES:
            assert index_write_name not in tool_names

    def test_default_read_only_mode_is_true(self):
        """Verify read_only_mode defaults to True."""
        # Default should filter KV write tools
        tools = get_tools()
        tool_names = {tool.__name__ for tool in tools}

        # Should only have read-only tools (read_only_mode defaults to True)
        assert tool_names == READ_ONLY_TOOL_NAMES


class TestToolCounts:
    """Tests for verifying correct tool counts in different modes."""

    def test_read_only_mode_tool_count(self):
        """Verify correct number of tools in read-only mode."""
        tools = get_tools(read_only_mode=True)
        assert len(tools) == len(READ_ONLY_TOOLS)
        assert len(tools) == 21  # Expected count of read-only tools

    def test_all_tools_mode_tool_count(self):
        """Verify correct number of tools when all write tools are enabled."""
        tools = get_tools(read_only_mode=False)
        assert len(tools) == len(ALL_TOOLS)
        # Expected total count (21 read-only + 5 KV write + 4 collection write
        # + 3 index write)
        assert len(tools) == 33

    def test_kv_write_tools_count(self):
        """Verify exactly 5 KV write tools exist."""
        assert len(KV_WRITE_TOOLS) == 5

    def test_collection_write_tools_count(self):
        """Verify exactly 4 collection management write tools exist."""
        assert len(COLLECTION_WRITE_TOOLS) == 4

    def test_index_write_tools_count(self):
        """Verify exactly 3 index write tools exist."""
        assert len(INDEX_WRITE_TOOLS) == 3


class TestReadOnlyModeToolFiltering:
    """Tests for verifying specific tool filtering behavior."""

    def test_upsert_tool_filtered_in_read_only_mode(self):
        """Verify upsert_document_by_id is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "upsert_document_by_id" not in tool_names

    def test_insert_tool_filtered_in_read_only_mode(self):
        """Verify insert_document_by_id is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "insert_document_by_id" not in tool_names

    def test_replace_tool_filtered_in_read_only_mode(self):
        """Verify replace_document_by_id is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "replace_document_by_id" not in tool_names

    def test_delete_tool_filtered_in_read_only_mode(self):
        """Verify delete_document_by_id is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "delete_document_by_id" not in tool_names

    def test_create_scope_tool_filtered_in_read_only_mode(self):
        """Verify create_scope is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "create_scope" not in tool_names

    def test_create_collection_tool_filtered_in_read_only_mode(self):
        """Verify create_collection is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "create_collection" not in tool_names

    def test_delete_scope_tool_filtered_in_read_only_mode(self):
        """Verify delete_scope is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "delete_scope" not in tool_names

    def test_delete_collection_tool_filtered_in_read_only_mode(self):
        """Verify delete_collection is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "delete_collection" not in tool_names

    def test_create_index_tool_filtered_in_read_only_mode(self):
        """Verify create_index is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "create_index" not in tool_names

    def test_build_index_tool_filtered_in_read_only_mode(self):
        """Verify build_index is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "build_index" not in tool_names

    def test_drop_index_tool_filtered_in_read_only_mode(self):
        """Verify drop_index is filtered in read-only mode."""
        tools = get_tools(read_only_mode=True)
        tool_names = {tool.__name__ for tool in tools}
        assert "drop_index" not in tool_names

    def test_get_document_always_available(self):
        """Verify get_document_by_id is always available (read operation)."""
        # In read-only mode
        tools_read_only = get_tools(read_only_mode=True)
        tool_names_read_only = {tool.__name__ for tool in tools_read_only}
        assert "get_document_by_id" in tool_names_read_only

        # In write mode
        tools_write = get_tools(read_only_mode=False)
        tool_names_write = {tool.__name__ for tool in tools_write}
        assert "get_document_by_id" in tool_names_write

    def test_query_tool_always_available(self):
        """Verify run_sql_plus_plus_query is always available.

        Note: Query write protection is handled at runtime, not by filtering the tool.
        """
        # In read-only mode
        tools_read_only = get_tools(read_only_mode=True)
        tool_names_read_only = {tool.__name__ for tool in tools_read_only}
        assert "run_sql_plus_plus_query" in tool_names_read_only

        # In write mode
        tools_write = get_tools(read_only_mode=False)
        tool_names_write = {tool.__name__ for tool in tools_write}
        assert "run_sql_plus_plus_query" in tool_names_write

    def test_explain_query_tool_always_available(self):
        """Verify explain_sql_plus_plus_query is always available."""
        tools_read_only = get_tools(read_only_mode=True)
        tool_names_read_only = {tool.__name__ for tool in tools_read_only}
        assert "explain_sql_plus_plus_query" in tool_names_read_only

        tools_write = get_tools(read_only_mode=False)
        tool_names_write = {tool.__name__ for tool in tools_write}
        assert "explain_sql_plus_plus_query" in tool_names_write


class TestAppContext:
    """Tests for AppContext dataclass with read_only_mode field."""

    def test_app_context_has_read_only_mode_field(self):
        """Verify AppContext has read_only_mode field."""

        context = AppContext()
        assert hasattr(context, "read_only_mode")

    def test_app_context_read_only_mode_default_true(self):
        """Verify AppContext.read_only_mode defaults to True."""

        context = AppContext()
        assert context.read_only_mode is True

    def test_app_context_can_set_read_only_mode_false(self):
        """Verify AppContext.read_only_mode can be set to False."""

        context = AppContext(read_only_mode=False)
        assert context.read_only_mode is False


class TestConstantsDefault:
    """Tests for default constants."""

    def test_default_read_only_mode_constant(self):
        """Verify DEFAULT_READ_ONLY_MODE constant is True."""

        assert DEFAULT_READ_ONLY_MODE is True
