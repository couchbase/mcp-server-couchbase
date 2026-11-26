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

import asyncio
import contextlib
import uuid
from datetime import timedelta

import pytest
from conftest import (
    create_mcp_session,
    direct_cluster,
    extract_payload,
    is_error_response,
)
from couchbase.options import QueryOptions


def _is_capella_rejection(response) -> bool:
    """True for the Capella error, or the tool being withheld on Capella."""
    text = str(extract_payload(response))
    return "Capella" in text or "unknown tool" in text.lower()


@pytest.mark.asyncio
async def test_get_cluster_query_vitals() -> None:
    """Verify get_cluster_query_vitals returns per-node vitals, keyed by node."""
    async with create_mcp_session() as session:
        response = await session.call_tool("get_cluster_query_vitals")
        if is_error_response(response):
            # Withheld entirely for a Capella deployment — see
            # test_deployment_gating.py for the dedicated assertions on this —
            # or rejected with a Capella error.
            assert _is_capella_rejection(response), (
                f"Expected only a Capella rejection, got: {extract_payload(response)}"
            )
            return

        payload = extract_payload(response)
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
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
            assert _is_capella_rejection(response), (
                f"Expected only a Capella rejection, got: {extract_payload(response)}"
            )
            return

        payload = extract_payload(response)
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
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
            assert isinstance(entry.get("node"), str) and ":" in entry["node"]


@pytest.mark.asyncio
async def test_get_active_queries_tags_running_request_with_node() -> None:
    """A genuinely running statement must come back tagged with its query node.

    The Query service's /admin/active_requests does not return a "node" field
    on the versions this has been checked against, so get_active_queries adds
    it. The statement is run over a direct SDK connection with a unique
    clientContextID so the entry can be picked out of whatever else is running,
    then cancelled through delete_active_query once it has been found.
    """
    context_id = f"mcp-it-active-queries-{uuid.uuid4()}"
    cluster = direct_cluster()

    def run_long_query() -> None:
        list(
            cluster.query(
                "SELECT COUNT(*) FROM ARRAY_RANGE(0, 50000) AS a "
                "UNNEST ARRAY_RANGE(0, 50000) AS b",
                QueryOptions(
                    client_context_id=context_id,
                    timeout=timedelta(seconds=60),
                ),
            ).rows()
        )

    query_task = asyncio.create_task(asyncio.to_thread(run_long_query))
    try:
        async with create_mcp_session() as session:
            entry = None
            for _ in range(30):
                response = await session.call_tool("get_active_queries")
                if is_error_response(response):
                    return
                payload = extract_payload(response)
                if payload.get("status") == "error":
                    assert _is_capella_rejection(payload), (
                        f"Expected only a Capella-rejection error, got: {payload}"
                    )
                    return
                entry = next(
                    (
                        e
                        for e in payload["active_requests"]
                        if e.get("clientContextID") == context_id
                    ),
                    None,
                )
                if entry is not None:
                    break
                await asyncio.sleep(0.5)

            assert entry is not None, (
                f"Running query {context_id!r} never appeared in get_active_queries"
            )
            request_id = entry["requestId"]
            assert isinstance(entry.get("node"), str) and ":" in entry["node"], (
                f"Expected a host:port 'node' on the running request: {entry}"
            )

            await session.call_tool(
                "delete_active_query", arguments={"request_id": request_id}
            )
    finally:
        # The cancelled statement ends in an SDK error; that is expected here.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(query_task, timeout=70)


@pytest.mark.asyncio
async def test_delete_active_query_with_bogus_id_reports_delivery() -> None:
    """A request ID that doesn't exist must not raise, and must not be
    reported as a failure either.

    delete_active_query reports delivery, not outcome: Couchbase Server's
    DELETE /admin/active_requests/{id} does not reliably distinguish a
    cancelled request from one that never existed (both can come back as the
    same error status on some builds), so "success" here only means the
    request reached a query node — confirmed via the per-node diagnostic
    data in "nodes", not inferred from any status code.

    This is the only branch of delete_active_query safe to exercise without
    coordinating a genuinely long-running query from a second session: every
    query node is reached and none can have a request matching a UUID that
    was never issued.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "delete_active_query",
            arguments={"request_id": "00000000-0000-0000-0000-000000000000"},
        )
        if is_error_response(response):
            assert _is_capella_rejection(response), (
                f"Expected only a Capella rejection, got: {extract_payload(response)}"
            )
            return

        payload = extract_payload(response)
        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("success") is True, (
            f"Expected the request to be delivered successfully: {payload}"
        )
        assert payload.get("request_id") == "00000000-0000-0000-0000-000000000000"
        nodes = payload.get("nodes")
        assert isinstance(nodes, list) and len(nodes) > 0
        for entry in nodes:
            assert "node" in entry
            assert "http_status" in entry
