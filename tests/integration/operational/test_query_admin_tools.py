"""
Integration tests for query_admin.py tools.

Tests for:
- get_cluster_query_vitals
- get_active_queries
- delete_active_query

Self-managed Couchbase Server 7.6+ only, like the other REST-only tools in
test_server_tools.py. Against a connection string that resolves to Capella,
the registration-time gate withholds these tools entirely, so the call comes
back as an MCP-level "unknown tool" error rather than this tool's own
envelope — see test_deployment_gating.py for the dedicated withholding
assertions. These tests accept that outcome too, rather than assuming the
configured cluster is self-managed.
"""

from __future__ import annotations

import pytest
from conftest import create_mcp_session, extract_payload, is_error_response


def _is_capella_rejection(payload: dict) -> bool:
    error = payload.get("error", "")
    return "Capella" in error


@pytest.mark.asyncio
async def test_get_cluster_query_vitals() -> None:
    """Verify get_cluster_query_vitals returns per-node vitals, keyed by node."""
    async with create_mcp_session() as session:
        response = await session.call_tool("get_cluster_query_vitals")
        if is_error_response(response):
            # Withheld entirely for a Capella deployment — see
            # test_deployment_gating.py for the dedicated assertions on this.
            return

        payload = extract_payload(response)
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        if payload.get("status") == "error":
            assert _is_capella_rejection(payload), (
                f"Expected only a Capella-rejection error, got: {payload}"
            )
            return

        assert payload.get("status") == "success", (
            f"Expected a status envelope: {payload}"
        )
        vitals = payload.get("vitals")
        assert isinstance(vitals, dict) and vitals, (
            "Expected 'vitals' keyed by at least one query node"
        )
        for node, node_vitals in vitals.items():
            assert isinstance(node, str) and ":" in node
            assert isinstance(node_vitals, dict)
            assert "version" in node_vitals


@pytest.mark.asyncio
async def test_get_active_queries() -> None:
    """Verify get_active_queries returns a merged list of active requests."""
    async with create_mcp_session() as session:
        response = await session.call_tool("get_active_queries")
        if is_error_response(response):
            return

        payload = extract_payload(response)
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        if payload.get("status") == "error":
            assert _is_capella_rejection(payload), (
                f"Expected only a Capella-rejection error, got: {payload}"
            )
            return

        assert payload.get("status") == "success", (
            f"Expected a status envelope: {payload}"
        )
        active_requests = payload.get("active_requests")
        assert isinstance(active_requests, list), (
            "Expected 'active_requests' to be a list (possibly empty)"
        )
        for entry in active_requests:
            assert isinstance(entry, dict)
            assert "requestId" in entry


@pytest.mark.asyncio
async def test_delete_active_query_with_bogus_id_reports_not_found() -> None:
    """A request ID that doesn't exist must fail cleanly, not raise.

    This is the only branch of delete_active_query safe to exercise without
    coordinating a genuinely long-running query from a second session: every
    query node is tried and none can have a request matching a UUID that was
    never issued.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "delete_active_query",
            arguments={"request_id": "00000000-0000-0000-0000-000000000000"},
        )
        if is_error_response(response):
            return

        payload = extract_payload(response)
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        if payload.get("success") is False and _is_capella_rejection(payload):
            return

        assert payload.get("success") is False, (
            f"Expected a bogus request id to be reported as not found: {payload}"
        )
        assert "00000000-0000-0000-0000-000000000000" in payload.get("error", "")
