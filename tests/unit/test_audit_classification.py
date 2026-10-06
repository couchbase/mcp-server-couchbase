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

from cb_mcp.audit.catalog import CATEGORIES
from cb_mcp.audit.classification import (
    TOOL_CLASSIFICATION,
    classify_tool,
    is_classified,
    required_scope_for,
    resolve_tool_call_event,
    unclassified_tool_names,
)
from cb_mcp.tools.operational import ALL_TOOLS, READ_ONLY_TOOLS, WRITE_TOOLS

#: The audited package. Only ``operational`` has a Tier-2 block and a
#: classification table; the Operational Insights server declares no
#: ``audit_package`` and is deliberately not audited.
PACKAGE = "operational"

#: Main collapsed the four per-area tool groups into read-only and write when
#: the second server landed (#250), so the parity assertions below read the two
#: that remain. The contract they test is unchanged: the audit class must agree
#: with the server's own categorisation of every registered tool.
REGISTERED = {tool.__name__ for tool in ALL_TOOLS}
WRITE_TOOL_NAMES = {tool.__name__ for tool in WRITE_TOOLS}
READ_ONLY_NAMES = {tool.__name__ for tool in READ_ONLY_TOOLS}


def test_every_registered_tool_is_classified():
    assert unclassified_tool_names(REGISTERED, PACKAGE) == []


def test_no_stale_entries_for_tools_that_do_not_exist():
    assert sorted(set(TOOL_CLASSIFICATION[PACKAGE]) - REGISTERED) == []


def test_classification_covers_the_registered_set_exactly():
    assert set(TOOL_CLASSIFICATION[PACKAGE]) == REGISTERED


def test_only_the_operational_package_is_classified():
    """Operational Insights is deliberately unaudited.

    Its tools collide with operational's on five names, so classifying it
    without its own Tier-2 block would book its records against operational
    ids. Adding it is a table here plus a block in the catalogue — this test
    is what will fail, informatively, on the day someone adds one without
    the other.
    """
    assert set(TOOL_CLASSIFICATION) == {"operational"}


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOL_NAMES))
def test_repository_write_tools_are_classified_write(tool_name):
    """The audit class must agree with the server's own categorisation."""
    _, operation_class = classify_tool(tool_name)
    assert operation_class == "write", tool_name
    assert resolve_tool_call_event(tool_name).filterable is False


@pytest.mark.parametrize("tool_name", sorted(READ_ONLY_NAMES))
def test_read_only_tools_are_classified_read(tool_name):
    _, operation_class = classify_tool(tool_name)
    assert operation_class == "read", tool_name


@pytest.mark.parametrize(
    ("tool_name", "expected_id"),
    [
        ("get_document_by_id", 61490),
        ("lookup_subdocument", 61490),
        ("upsert_document_by_id", 61522),
        ("mutate_subdocument", 61522),
        ("delete_document_by_id", 61522),
        ("get_cluster_diagnostics_report", 61488),
        ("test_cluster_connection", 61488),
        ("get_cluster_tasks", 61488),
        ("get_cluster_health_snapshot", 61488),
        ("get_buckets_in_cluster", 61489),
        ("get_schema_for_collection", 61489),
        ("create_scope", 61521),
        ("delete_collection", 61521),
        ("explain_sql_plus_plus_query", 61491),
        ("run_sql_plus_plus_query", 61491),
        ("list_indexes", 61492),
        ("create_index", 61524),
        ("drop_index", 61524),
        ("get_longest_running_queries", 61493),
        ("get_queries_not_selective", 61493),
        # Search keeps its own category rather than folding into index: a
        # reviewer filtering on `index read` must not get FTS traffic, and
        # `run_fts_query` is a query, not an index operation.
        ("list_fts_indexes", 61496),
        ("run_fts_query", 61496),
        ("upsert_fts_index", 61528),
        ("drop_fts_index", 61528),
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
    assert resolve_tool_call_event(tool_name).id == expected_id


def test_sqlpp_write_statement_moves_to_the_query_write_id():
    read_event = resolve_tool_call_event("run_sql_plus_plus_query")
    write_event = resolve_tool_call_event(
        "run_sql_plus_plus_query", operation_class_override="write"
    )
    assert read_event.id == 61491
    assert write_event.id == 61523
    assert read_event.filterable is True
    # A successful mutation must not be filed under a filterable read id.
    assert write_event.filterable is False


def test_unknown_tool_fails_closed_to_a_non_filterable_write():
    assert is_classified("some_future_tool") is False
    event = resolve_tool_call_event("some_future_tool")
    assert event.operation_class == "write"
    assert event.filterable is False


def test_unclassified_tool_names_reports_only_the_gaps():
    names = ["get_document_by_id", "brand_new_a", "brand_new_b"]
    assert unclassified_tool_names(names) == ["brand_new_a", "brand_new_b"]


@pytest.mark.parametrize(
    ("operation_class", "expected"),
    [("read", "read"), ("write", "write")],
)
def test_required_scope_is_derived_from_the_operation_class(operation_class, expected):
    assert required_scope_for(operation_class) == expected


def test_every_classification_uses_a_known_category_and_class():
    for tool_name, (category, operation_class) in TOOL_CLASSIFICATION[PACKAGE].items():
        assert category in CATEGORIES, tool_name
        assert operation_class in ("read", "write"), tool_name
