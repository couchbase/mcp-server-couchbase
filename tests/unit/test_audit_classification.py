"""Tests for tool classification.

The classification map is what keeps every shipped tool auditable, so the
central test asserts it covers the registered tool set exactly — no gaps and no
stale entries for tools that no longer exist.

Coverage map:
- every registered tool is classified, and nothing extra is
- the repository's own write-tool lists agree with the classification
- read-only tools are classified read
- SQL++ statement override moves the event to the query write id
- unknown tools fail closed to a write id
- required_scope is derived from the operation class
- unclassified_tool_names reports gaps
"""

from __future__ import annotations

import pytest

from cb_mcp.audit.catalog import (
    CATEGORY_SLOTS,
    FAILSAFE_SLOT_REQUIRED,
    SERVICE_PACKAGE_BLOCKS,
    categories_for,
)
from cb_mcp.audit.classification import (
    FAILSAFE_CATEGORY,
    TOOL_CLASSIFICATION,
    classify_tool,
    is_classified,
    is_statement_classified,
    required_scope_for,
    resolve_tool_call_event,
    unclassified_tool_names,
)
from cb_mcp.tools.operational import ALL_TOOLS, READ_ONLY_TOOLS, WRITE_TOOLS
from cb_mcp.tools.operational_insights import ALL_TOOLS as OI_ALL_TOOLS
from cb_mcp.tools.operational_insights import WRITE_TOOLS as OI_WRITE_TOOLS
from cb_mcp.tools.operational_insights.query import _audit_statement_class

#: The operational package. Operational Insights has its own block and table
#: its own block and table; constants for it are defined alongside these.
PACKAGE = "operational"

#: Main collapsed the four per-area tool groups into read-only and write when
#: the second server landed (#250), so the parity assertions below read the two
#: that remain. The contract they test is unchanged: the audit class must agree
#: with the server's own categorisation of every registered tool.
REGISTERED = {tool.__name__ for tool in ALL_TOOLS}
WRITE_TOOL_NAMES = {tool.__name__ for tool in WRITE_TOOLS}
READ_ONLY_NAMES = {tool.__name__ for tool in READ_ONLY_TOOLS}

#: The Operational Insights server, audited under its own block.
OI_REGISTERED = {tool.__name__ for tool in OI_ALL_TOOLS}
OI_WRITE_NAMES = {tool.__name__ for tool in OI_WRITE_TOOLS}


def test_every_registered_tool_is_classified():
    assert unclassified_tool_names(REGISTERED, PACKAGE) == []


def test_no_stale_entries_for_tools_that_do_not_exist():
    assert sorted(set(TOOL_CLASSIFICATION[PACKAGE]) - REGISTERED) == []


def test_classification_covers_the_registered_set_exactly():
    assert set(TOOL_CLASSIFICATION[PACKAGE]) == REGISTERED


def test_every_classified_package_has_a_block_and_vice_versa():
    """A half-registered package is the failure this guards.

    A table with no block raises at runtime the first time a tool is called; a
    block with no table sends every tool down the fail-closed path, recording
    real calls against a placeholder category. Both halves land together or
    this fails.
    """
    assert set(TOOL_CLASSIFICATION) == set(SERVICE_PACKAGE_BLOCKS)
    assert set(TOOL_CLASSIFICATION) == {"operational", "operational_insights"}


def test_the_two_servers_resolve_their_shared_tool_names_differently():
    """The reason the table is keyed by package at all.

    Both servers register get_server_configuration_status. Booking one against
    the other's block would file its records under ids that mean a different
    server, and nothing in the record would reveal it.
    """
    operational = resolve_tool_call_event(
        "get_server_configuration_status", package="operational"
    )
    insights = resolve_tool_call_event(
        "get_server_configuration_status", package="operational_insights"
    )
    assert operational.id == 61495
    assert insights.id == 65591
    # Same category and class, so the difference is purely the block.
    assert operational.category == insights.category == "config"
    assert operational.operation_class == insights.operation_class == "read"


def test_every_registered_oi_tool_is_classified():
    assert unclassified_tool_names(OI_REGISTERED, "operational_insights") == []


def test_no_stale_oi_entries():
    assert (
        sorted(set(TOOL_CLASSIFICATION["operational_insights"]) - OI_REGISTERED) == []
    )


@pytest.mark.parametrize(
    ("tool_name", "expected_id"),
    [
        ("get_server_configuration_status", 65591),  # config read
        ("operational_insights_get_databases_in_cluster", 65585),  # schema read
        ("operational_insights_get_schema_for_collection", 65585),
        # Statement-classified: 65619 is the non-filterable fallback used
        # when the statement reported no class.
        ("operational_insights_run_query_sync", 65619),
        ("operational_insights_cancel_async_query", 65587),
        ("operational_insights_list_indexes", 65588),  # index read
        ("operational_insights_create_index", 65620),  # index write
    ],
)
def test_oi_tool_maps_to_the_expected_event_id(tool_name, expected_id):
    """Every id is in the OI block, never the operational one."""
    event = resolve_tool_call_event(tool_name, package="operational_insights")
    assert event.id == expected_id
    assert 65536 <= event.id < 65536 + 4096


@pytest.mark.parametrize("tool_name", sorted(OI_WRITE_NAMES))
def test_oi_write_tools_are_classified_write(tool_name):
    _, operation_class = classify_tool(tool_name, package="operational_insights")
    assert operation_class == "write", tool_name


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOL_NAMES))
def test_repository_write_tools_are_classified_write(tool_name):
    """The audit class must agree with the server's own categorisation."""
    _, operation_class = classify_tool(tool_name, package=PACKAGE)
    assert operation_class == "write", tool_name
    assert resolve_tool_call_event(tool_name, package=PACKAGE).filterable is False


@pytest.mark.parametrize("tool_name", sorted(READ_ONLY_NAMES))
def test_read_only_tools_are_classified_read(tool_name):
    _, operation_class = classify_tool(tool_name, package=PACKAGE)
    assert operation_class == "read", tool_name


@pytest.mark.parametrize(
    ("tool_name", "expected_id"),
    [
        ("get_document_by_id", 61490),
        ("lookup_subdocument", 61490),
        ("upsert_document_by_id", 61522),
        ("mutate_subdocument", 61522),
        ("delete_document_by_id", 61522),
        ("get_client_connectivity_report", 61488),
        ("test_cluster_connection", 61488),
        ("get_cluster_tasks", 61488),
        ("get_cluster_health_snapshot", 61488),
        ("get_cluster_system_events", 61488),
        # Cluster Setup & Health: both describe how a service is holding up
        # rather than the query workload or the index inventory.
        ("get_cluster_query_vitals", 61488),
        ("get_index_stats", 61488),
        ("get_bucket_config", 61488),
        # The server's own surface, split out of cluster on the same date.
        ("get_server_configuration_status", 61495),
        ("discover_tool_input_values", 61495),
        ("get_buckets_in_cluster", 61489),
        ("get_schema_for_collection", 61489),
        ("create_scope", 61521),
        ("delete_collection", 61521),
        ("explain_sql_plus_plus_query", 61491),
        ("run_sql_plus_plus_query", 61491),
        ("list_indexes", 61492),
        ("create_index", 61524),
        # Superseding create_index must not move the id a SIEM rule watches.
        ("create_query_index", 61524),
        ("drop_index", 61524),
        ("get_longest_running_queries", 61493),
        ("get_queries_not_selective", 61493),
        ("get_active_queries", 61493),
        # The first tool to take the performance write id, and the only one in
        # the server that cancels another caller's in-flight work.
        ("delete_active_query", 61525),
        # Search keeps its own category rather than folding into index: a
        # reviewer filtering on `index read` must not get FTS traffic, and
        # `run_search` is a query, not an index operation. Moved into the
        # slot 6.
        ("list_search_indexes", 61494),
        ("run_search", 61494),
        ("upsert_search_index", 61526),
        ("drop_search_index", 61526),
    ],
)
def test_tool_maps_to_the_expected_event_id(tool_name, expected_id):
    """Pins the *category*, which the read/write tests above cannot.

    ``test_repository_write_tools_are_classified_write`` checks the operation
    class against the server's own read/write split, so a tool filed under the
    wrong category — an FTS write booked as an index write — satisfies it
    while writing the wrong id into every record for that tool. The id is the
    wire contract a SIEM rule is written against; only an explicit expectation
    holds it still.
    """
    assert resolve_tool_call_event(tool_name, package=PACKAGE).id == expected_id


def test_sqlpp_write_statement_moves_to_the_query_write_id():
    read_event = resolve_tool_call_event("run_sql_plus_plus_query", package=PACKAGE)
    write_event = resolve_tool_call_event(
        "run_sql_plus_plus_query", package=PACKAGE, operation_class_override="write"
    )
    assert read_event.id == 61491
    assert write_event.id == 61523
    assert read_event.filterable is True
    # A successful mutation must not be filed under a filterable read id.
    assert write_event.filterable is False


@pytest.mark.parametrize("package", sorted(SERVICE_PACKAGE_BLOCKS))
def test_fail_closed_resolves_in_every_package(package):
    """The fail-closed path must produce a record, not an exception.

    An unclassified tool resolves to the failsafe category. If a package does
    not allocate that category, the resolution raises -- and the audit
    middleware swallows exceptions on purpose, so the call succeeds and
    *nothing is recorded*. That is silence in the one case fail-closed exists
    to make loud, and no counter moves: the operator sees dropped: 0.

    """
    event = resolve_tool_call_event("a_tool_nobody_classified", package=package)
    assert event.operation_class == "write"
    assert event.filterable is False
    base = SERVICE_PACKAGE_BLOCKS[package]
    assert base <= event.id < base + 4096
    assert is_classified("a_tool_nobody_classified", package) is False


def test_the_failsafe_category_is_allocated_everywhere():
    """Pins the invariant the import-time guard enforces."""
    assert FAILSAFE_CATEGORY == FAILSAFE_SLOT_REQUIRED
    for package, slots in CATEGORY_SLOTS.items():
        assert FAILSAFE_CATEGORY in slots, package


def test_unknown_tool_fails_closed_to_a_non_filterable_write():
    assert is_classified("some_future_tool", PACKAGE) is False
    event = resolve_tool_call_event("some_future_tool", package=PACKAGE)
    assert event.operation_class == "write"
    assert event.filterable is False


def test_unclassified_tool_names_reports_only_the_gaps():
    names = ["get_document_by_id", "brand_new_a", "brand_new_b"]
    assert unclassified_tool_names(names, PACKAGE) == ["brand_new_a", "brand_new_b"]


@pytest.mark.parametrize(
    ("operation_class", "expected"),
    [("read", "read"), ("write", "write")],
)
def test_required_scope_is_derived_from_the_operation_class(operation_class, expected):
    assert required_scope_for(operation_class) == expected


def test_every_classification_uses_a_known_category_and_class():
    for tool_name, (category, operation_class) in TOOL_CLASSIFICATION[PACKAGE].items():
        assert category in categories_for(PACKAGE), tool_name
        assert operation_class in ("read", "write"), tool_name


class TestStatementClassifiedTools:
    """Tools that accept SELECT, DML and DDL through one entry point.

    Their class cannot come from the tool name. Read ids are filterable, so a
    mutation recorded as a read could be configured out of the audit log --
    which is why the static fallback for each is write.
    """

    def test_the_operational_insights_query_tools_are_statement_classified(self):
        for tool_name in (
            "operational_insights_run_query_sync",
            "operational_insights_run_query_async",
        ):
            assert is_statement_classified(tool_name, "operational_insights"), tool_name

    def test_sqlpp_is_statement_classified_on_the_operational_server(self):
        assert is_statement_classified("run_sql_plus_plus_query", PACKAGE)

    def test_a_statement_classified_tool_is_not_shared_across_packages(self):
        """The name only means something inside its own package."""
        assert not is_statement_classified(
            "run_sql_plus_plus_query", "operational_insights"
        )
        assert not is_statement_classified(
            "operational_insights_run_query_sync", PACKAGE
        )

    @pytest.mark.parametrize(
        "tool_name",
        [
            "operational_insights_run_query_sync",
            "operational_insights_run_query_async",
        ],
    )
    def test_the_fallback_class_is_the_non_filterable_one(self, tool_name):
        """If no class was recorded, the record must not be filterable.

        The failure mode of the state channel is silence, so the static answer
        has to be the one that cannot hide a mutation.
        """
        event = resolve_tool_call_event(tool_name, package="operational_insights")
        assert event.operation_class == "write"
        assert event.filterable is False

    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            ("SELECT 1", "read"),
            ("  -- a comment\nSELECT 1", "read"),
            ("EXPLAIN SELECT 1", "read"),
            ("INSERT INTO x VALUES (1)", "write"),
            ("DELETE FROM x WHERE a = 1", "write"),
            ("CREATE INDEX i ON d(a)", "write"),
            ("COPY d TO link AT 'p'", "write"),
            # Not provably a read, so it is recorded as a write: noise in the
            # log rather than a mutation that could be filtered out of it.
            ("WITH a AS (SELECT 1) SELECT * FROM a", "write"),
        ],
    )
    def test_statements_that_are_not_provably_reads_are_writes(
        self, statement, expected
    ):
        assert _audit_statement_class(statement) == expected
