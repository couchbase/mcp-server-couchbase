"""Tests for tool classification.

The classification map is what keeps every shipped tool auditable, so the
central test asserts it covers the registered tool set exactly — no gaps and no
stale entries for tools that no longer exist.

Coverage map:
- every registered tool is classified, and nothing extra is
- the repository's own write-tool lists agree with the classification
- read-only tools are classified read
- the query category is unsplit: one event, whatever the statement does
- unknown tools fail closed to a write id
- required_scope is derived from the operation class
- unclassified_tool_names reports gaps
"""

from __future__ import annotations

import pytest

from cb_mcp.audit.catalog import (
    ALL_IDS,
    CATEGORY_SLOTS,
    FAILSAFE_SLOT_REQUIRED,
    FILTERABLE_IDS,
    SERVICE_PACKAGE_BLOCKS,
    categories_for,
    is_unsplit,
)
from cb_mcp.audit.classification import (
    FAILSAFE_CATEGORY,
    TOOL_CLASSIFICATION,
    classify_tool,
    is_classified,
    required_scope_for,
    resolve_tool_call_event,
    unclassified_tool_names,
)
from cb_mcp.tools.operational import ALL_TOOLS, READ_ONLY_TOOLS, WRITE_TOOLS
from cb_mcp.tools.operational_insights import ALL_TOOLS as OI_ALL_TOOLS
from cb_mcp.tools.operational_insights import WRITE_TOOLS as OI_WRITE_TOOLS

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
        # `query` is unsplit: one id for every query call.
        ("operational_insights_run_query_sync", 65603),
        ("operational_insights_run_query_async", 65603),
        ("operational_insights_cancel_async_query", 65603),
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
    """The audit class must agree with the server's own categorisation.

    Except in an unsplit category, which has no write class to agree with: a
    `search` write and a `search` read are one event, and that event is
    filterable. `drop_search_index` is therefore a write the audit log does not
    mark as one -- the accepted cost of collapsing the category.
    """
    category, operation_class = classify_tool(tool_name, package=PACKAGE)
    if is_unsplit(category):
        # No write class to agree with, but the guarantee that matters -- a
        # write is never filterable -- still holds.
        assert resolve_tool_call_event(tool_name, package=PACKAGE).filterable is False
        return
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
        ("explain_sql_plus_plus_query", 61507),
        ("run_sql_plus_plus_query", 61507),
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
        # `search` is unsplit: index lifecycle and search queries share one id.
        ("list_search_indexes", 61510),
        ("run_search", 61510),
        ("run_vector_search", 61510),
        ("upsert_search_index", 61510),
        ("drop_search_index", 61510),
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


def test_sqlpp_resolves_to_the_single_query_event_whatever_the_class():
    """`query` is unsplit, so an override cannot move the id.

    Callers may still pass an operation class -- the fail-closed path asks for
    a write -- and every one of them has to land on the same id, or a record
    could claim a `query write` id that the catalogue no longer publishes.
    """
    event = resolve_tool_call_event("run_sql_plus_plus_query", package=PACKAGE)
    overridden = resolve_tool_call_event(
        "run_sql_plus_plus_query", package=PACKAGE, operation_class_override="write"
    )
    assert event.id == overridden.id == 61507
    assert event.event_name == "query"
    assert event.filterable is False


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


class TestUnsplitQueryCategory:
    """`query` emits one event per call rather than a read/write pair.

    Deciding read-vs-write meant inspecting the statement, which was neither
    reliable (a SELECT behind a CTE booked as a write) nor cheap (a full parse
    per call). The category now records that a query happened and leaves what
    it did to the statement itself.
    """

    @pytest.mark.parametrize(
        ("package", "tool_name", "expected_id"),
        [
            ("operational", "run_sql_plus_plus_query", 61507),
            ("operational", "explain_sql_plus_plus_query", 61507),
            ("operational", "run_vector_query", 61507),
            ("operational_insights", "operational_insights_run_query_sync", 65603),
            ("operational_insights", "operational_insights_run_query_async", 65603),
            ("operational_insights", "operational_insights_explain_query", 65603),
        ],
    )
    def test_every_query_tool_resolves_to_its_package_query_event(
        self, package, tool_name, expected_id
    ):
        event = resolve_tool_call_event(tool_name, package=package)
        assert event.id == expected_id
        assert event.event_name == "query"

    @pytest.mark.parametrize("package", sorted(SERVICE_PACKAGE_BLOCKS))
    def test_an_unsplit_category_leaves_two_offsets_unused(self, package):
        """One event means the read and write offsets both stay empty.

        Pinned so the descriptor can never advertise an id no build emits -- a
        SIEM author would write a rule against it that never fires.
        """
        block = SERVICE_PACKAGE_BLOCKS[package]
        for category in ("query", "search"):
            if category not in CATEGORY_SLOTS[package]:
                continue
            read_offset = block + 48 + CATEGORY_SLOTS[package][category]
            assert read_offset not in ALL_IDS, (package, category, "read")
            assert read_offset + 32 not in ALL_IDS, (package, category, "write")

    @pytest.mark.parametrize(
        "event_id",
        [
            61507,  # operational query
            61510,  # operational search
            65603,  # operational_insights query
        ],
    )
    def test_an_unsplit_event_is_never_filterable(self, event_id):
        """One event covers the category's writes, so it cannot be switched off.

        Were it filterable, an operator turning down read noise would lose the
        record of a SQL++ mutation or an FTS index deletion with it -- exactly
        what the read/write split protected. The cost of this choice is that
        the category's noise cannot be reduced at all.
        """
        assert event_id not in FILTERABLE_IDS
