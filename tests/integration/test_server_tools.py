"""
Integration tests for server.py tools.

Tests for:
- get_server_configuration_status
- get_buckets_in_cluster
- get_scopes_in_bucket
- get_scopes_and_collections_in_bucket
- get_collections_in_scope
- get_cluster_health_and_services (including service_types filtering)
- get_cluster_diagnostics_report
- test_cluster_connection
- get_cluster_metrics
- get_nodes_in_cluster
"""

from __future__ import annotations

import pytest
from conftest import (
    create_mcp_session,
    ensure_list,
    extract_payload,
    get_test_scope,
    is_error_response,
    require_test_bucket,
)


@pytest.mark.asyncio
async def test_get_server_configuration_status() -> None:
    """Verify get_server_configuration_status returns server config without secrets."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_server_configuration_status", arguments={}
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), "Expected dict response"
        assert payload.get("status") == "running"
        assert payload.get("server_name") == "couchbase"

        # Configuration should be present but not expose the password
        config = payload.get("configuration", {})
        assert "connection_string" in config
        assert "username" in config
        assert "disabled_tools" in config
        assert "confirmation_required_tools" in config
        assert isinstance(config["disabled_tools"], list)
        assert isinstance(config["confirmation_required_tools"], list)
        assert "password_configured" in config
        assert "password" not in config  # password should NOT be exposed


@pytest.mark.asyncio
async def test_get_scopes_in_bucket() -> None:
    """Verify get_scopes_in_bucket returns scopes for a given bucket."""
    bucket = require_test_bucket()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_scopes_in_bucket", arguments={"bucket_name": bucket}
        )
        payload = extract_payload(response)

        assert isinstance(payload, list), (
            f"Expected list of scopes, got {type(payload)}"
        )
        # Every bucket has at least _default scope
        assert "_default" in payload, "Expected _default scope in bucket"


@pytest.mark.asyncio
async def test_get_scopes_and_collections_in_bucket() -> None:
    """Verify get_scopes_and_collections_in_bucket returns scope->collections map."""
    bucket = require_test_bucket()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_scopes_and_collections_in_bucket", arguments={"bucket_name": bucket}
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        # Every bucket has at least _default scope with _default collection
        assert "_default" in payload, "Expected _default scope"
        assert isinstance(payload["_default"], list), (
            "Scope should map to list of collections"
        )
        assert "_default" in payload["_default"], (
            "Expected _default collection in _default scope"
        )


@pytest.mark.asyncio
async def test_get_collections_in_scope() -> None:
    """Verify get_collections_in_scope returns collections for a given scope."""
    bucket = require_test_bucket()
    scope = get_test_scope()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_collections_in_scope",
            arguments={"bucket_name": bucket, "scope_name": scope},
        )
        payload = ensure_list(extract_payload(response))

        assert isinstance(payload, list), (
            f"Expected list of collections, got {type(payload)}"
        )
        # _default scope always has _default collection
        if scope == "_default":
            assert "_default" in payload, (
                "Expected _default collection in _default scope"
            )


@pytest.mark.asyncio
async def test_get_cluster_health_and_services() -> None:
    """Verify get_cluster_health_and_services returns health info."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_cluster_health_and_services", arguments={}
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") == "success", f"Expected success status: {payload}"
        assert "data" in payload, "Expected 'data' key with health info"


@pytest.mark.asyncio
async def test_get_cluster_health_and_services_with_bucket() -> None:
    """Verify get_cluster_health_and_services works with a specific bucket."""
    bucket = require_test_bucket()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_cluster_health_and_services", arguments={"bucket_name": bucket}
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") == "success", f"Expected success status: {payload}"
        assert "data" in payload, "Expected 'data' key with health info"


@pytest.mark.asyncio
async def test_get_cluster_health_and_services_with_service_types() -> None:
    """Verify get_cluster_health_and_services filters by service_types."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_cluster_health_and_services",
            arguments={"service_types": ["query"]},
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") == "success", f"Expected success status: {payload}"
        assert "data" in payload, "Expected 'data' key with health info"


@pytest.mark.asyncio
async def test_get_cluster_health_and_services_invalid_service_type() -> None:
    """An unrecognized service_types entry must return an error envelope."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_cluster_health_and_services",
            arguments={"service_types": ["not_a_real_service"]},
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") == "error", f"Expected error status: {payload}"


@pytest.mark.asyncio
async def test_get_scopes_in_nonexistent_bucket_returns_error() -> None:
    """A bucket that doesn't exist must surface a clean error response."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_scopes_in_bucket",
            arguments={"bucket_name": "definitely-does-not-exist-xyz123"},
        )

        assert is_error_response(response), (
            "Non-existent bucket must produce an error response, "
            f"got payload: {extract_payload(response)}"
        )


@pytest.mark.asyncio
async def test_get_collections_in_nonexistent_scope_returns_empty() -> None:
    """A scope that doesn't exist returns an empty list, NOT an error."""
    bucket = require_test_bucket()

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_collections_in_scope",
            arguments={
                "bucket_name": bucket,
                "scope_name": "no-such-scope-xyz123",
            },
        )
        payload = ensure_list(extract_payload(response))

        assert payload == [], (
            f"Expected empty list for non-existent scope, got: {payload}"
        )


@pytest.mark.asyncio
async def test_get_cluster_metrics() -> None:
    """Verify get_cluster_metrics returns a stats-range response envelope.

    Self-managed Couchbase Server 7.6+ only. Against Capella, expect a clean
    {"status": "error", ...} envelope rather than an unhandled exception.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_cluster_metrics",
            arguments={
                "metrics": [
                    {
                        "metric": [
                            {"label": "name", "value": "sysproc_cpu_utilization"}
                        ],
                        "applyFunctions": ["avg"],
                        "step": 10,
                        "start": -60,
                    }
                ]
            },
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") in ("success", "error"), (
            f"Expected a status envelope: {payload}"
        )
        if payload.get("status") == "success":
            assert isinstance(payload.get("data"), list), (
                "Expected 'data' to be a list of per-metric-spec results"
            )


@pytest.mark.asyncio
async def test_get_cluster_metrics_invalid_metric_reports_per_spec_error() -> None:
    """An unrecognized metric name should surface inline, not fail the whole call."""
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_cluster_metrics",
            arguments={
                "metrics": [
                    {"metric": [{"label": "name", "value": "not_a_real_metric_xyz"}]}
                ]
            },
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        if payload.get("status") == "success":
            data = payload.get("data")
            assert isinstance(data, list) and len(data) == 1
            # The server reports the unrecognized metric via a per-spec error rather
            # than failing the whole request.
            assert data[0].get("errors") or data[0].get("data") == []


@pytest.mark.asyncio
async def test_get_nodes_in_cluster() -> None:
    """Verify get_nodes_in_cluster returns cluster node targets.

    Self-managed Couchbase Server only. Against Capella, expect a clean
    {"status": "error", ...} envelope rather than an unhandled exception.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool("get_nodes_in_cluster", arguments={})
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("status") in ("success", "error"), (
            f"Expected a status envelope: {payload}"
        )
        if payload.get("status") == "success":
            data = payload.get("data")
            assert isinstance(data, list) and len(data) > 0, (
                "Expected at least one node target"
            )
            assert all(":" in target for target in data), (
                f"Expected 'host:port' targets, got: {data}"
            )
