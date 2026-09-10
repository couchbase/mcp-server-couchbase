"""
Integration tests for reference.py tools.

Tests for:
- discover_tool_input_values (browse mode, search mode, chapter filters)

This tool never touches the cluster, so it is also exercised via
``create_logging_test_session`` — which strips cluster credentials — to prove it keeps working
when no cluster is configured. That is the scenario it exists for: looking up a metric name is
most useful precisely when the cluster is unreachable.
"""

from __future__ import annotations

import pytest
from conftest import (
    create_logging_test_session,
    create_mcp_session,
    extract_payload,
)

METRICS_TOOL = "get_cluster_metrics"


@pytest.mark.asyncio
async def test_browse_returns_chapters_and_a_sample_record() -> None:
    """Passing tool_name alone is a valid call and returns the table of contents."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "discover_tool_input_values", arguments={"tool_name": METRICS_TOOL}
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict response, got {type(payload)}"
        assert payload.get("success") is True
        assert payload.get("chapters"), "Browse mode must return chapters"
        assert payload.get("sample_record"), "Browse mode must return a sample record"
        assert "next_step" in payload
        assert "results" not in payload, "Browse mode must not run a search"


@pytest.mark.asyncio
async def test_search_finds_a_known_metric() -> None:
    """A concept-level keyword search surfaces the real metric name."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "discover_tool_input_values",
            arguments={
                "tool_name": METRICS_TOOL,
                "search_keywords": ["audit", "dropped"],
            },
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict response, got {type(payload)}"
        assert payload.get("success") is True
        assert payload["matches"] > 0
        names = [row["name"] for row in payload["results"]]
        assert "kv_audit_dropped_events" in names, (
            f"Expected the audit metric, got {names[:5]}"
        )

        first = payload["results"][0]
        for field in ("name", "description", "metric_type", "since_version", "score"):
            assert field in first, f"Result rows should carry {field!r}"


@pytest.mark.asyncio
async def test_chapter_filter_restricts_results_to_that_chapter() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "discover_tool_input_values",
            arguments={
                "tool_name": METRICS_TOOL,
                "search_keywords": ["memory", "usage"],
                "chapter_filters": {"category": "Query Service Metrics"},
            },
        )
        payload = extract_payload(response)

        assert payload.get("success") is True
        assert payload["matches"] > 0
        categories = {row["category"] for row in payload["results"]}
        assert categories == {"Query Service Metrics"}, f"Filter leaked: {categories}"


@pytest.mark.asyncio
async def test_unknown_tool_name_reports_what_is_registered() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "discover_tool_input_values", arguments={"tool_name": "not_a_real_tool"}
        )
        payload = extract_payload(response)

        assert payload.get("success") is False
        assert METRICS_TOOL in payload.get("available_tool_names", [])


@pytest.mark.asyncio
async def test_works_with_no_cluster_configured() -> None:
    """The reference data is bundled, so this tool must not need cluster credentials."""
    async with create_logging_test_session() as session:
        response = await session.call_tool(
            "discover_tool_input_values",
            arguments={"tool_name": METRICS_TOOL, "search_keywords": ["disk", "queue"]},
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict response, got {type(payload)}"
        assert payload.get("success") is True, (
            f"Tool should work without a cluster, got: {payload}"
        )
        assert payload["matches"] > 0
