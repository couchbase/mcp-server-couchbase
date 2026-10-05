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
- get_cluster_health_snapshot
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

from cb_mcp.servers.operational.constants import FASTMCP_SERVER_NAME


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
        assert payload.get("server_name") == FASTMCP_SERVER_NAME

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
        if payload.get("status") == "error":
            # Only the documented Capella rejection is an acceptable error here —
            # anything else (bad nodes, bounds rejection, connectivity) is a real
            # failure this test should catch, not silently pass through.
            assert "Capella" in payload.get("error", ""), (
                f"Expected only a Capella-rejection error, got: {payload}"
            )
            return

        assert payload.get("status") == "success", (
            f"Expected a status envelope: {payload}"
        )
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
                    {
                        "metric": [{"label": "name", "value": "not_a_real_metric_xyz"}],
                    }
                ]
            },
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        if payload.get("status") == "error":
            # Only the documented Capella rejection is an acceptable error here —
            # anything else (bad nodes, bounds rejection, connectivity) is a real
            # failure this test should catch, not silently pass through.
            assert "Capella" in payload.get("error", ""), (
                f"Expected only a Capella-rejection error, got: {payload}"
            )
            return

        data = payload.get("data")
        assert isinstance(data, list) and len(data) == 1
        # The server reports the unrecognized metric via a per-spec error rather
        # than failing the whole request.
        assert data[0].get("errors") or data[0].get("data") == []


@pytest.mark.asyncio
async def test_get_cluster_tasks() -> None:
    """Verify get_cluster_tasks returns the raw task array from the cluster.

    Self-managed Couchbase Server 7.6+ only; Capella is rejected by the tool.
    Unlike the enveloped tools, this returns the endpoint's array unchanged, so
    the assertions are about shape rather than a status envelope.

    An idle cluster still reports a rebalance task with status "notRunning", so
    this asserts on per-task "status" rather than on the array being empty.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool("get_cluster_tasks")
        payload = extract_payload(response)

        if is_error_response(response):
            # Only the documented Capella rejection is an acceptable error here —
            # anything else (connectivity, RBAC, an unsupported server) is a real
            # failure this test should catch, not silently pass through.
            assert "Capella" in str(payload), (
                f"Expected only a Capella rejection, got: {payload}"
            )
            return

        payload = ensure_list(payload)
        assert isinstance(payload, list), f"Expected a list, got {type(payload)}"
        for task in payload:
            assert isinstance(task, dict), f"Expected task objects, got: {task}"
            # "type" and "status" are the only fields common to every task type;
            # everything else varies by type and is passed through untouched.
            assert "type" in task, f"Task missing 'type': {task}"
            assert "status" in task, f"Task missing 'status': {task}"


@pytest.mark.asyncio
async def test_get_cluster_health_snapshot() -> None:
    """Verify get_cluster_health_snapshot merges the three topology endpoints.

    Self-managed Couchbase Server 7.6+ only; Capella is rejected by the tool.
    Like get_cluster_tasks this returns its payload unenveloped, so the
    assertions are about shape rather than a status envelope.

    A healthy test cluster exercises only the healthy path, so this asserts on
    the join (every node carries its status, services and ports) and on the
    orchestrator being identified — the merge's failure mode is a quiet one,
    where nothing raises but no node is ever flagged.
    """
    async with create_mcp_session() as session:
        response = await session.call_tool("get_cluster_health_snapshot")
        payload = extract_payload(response)

        if is_error_response(response):
            # Only the documented Capella rejection is an acceptable error here.
            assert "Capella" in str(payload), (
                f"Expected only a Capella rejection, got: {payload}"
            )
            return

        assert isinstance(payload, dict), f"Expected a dict, got {type(payload)}"
        assert set(payload) == {"cluster", "nodes"}, f"Unexpected keys: {list(payload)}"

        cluster = payload["cluster"]
        nodes = payload["nodes"]
        assert isinstance(nodes, list) and nodes, "Expected at least one node"
        assert cluster["nodes_total"] == len(nodes)

        for node in nodes:
            # status/services come from /pools/default, service_ports from
            # nodeServices — all three present means the join worked.
            assert node["hostname"], f"Node missing hostname: {node}"
            assert node["status"], f"Node missing status: {node}"
            assert isinstance(node["services"], list), f"Bad services: {node}"
            assert isinstance(node["service_ports"], dict), f"Bad ports: {node}"
            assert isinstance(node["is_orchestrator"], bool)
            assert node["safe_to_act_on"] is not node["is_orchestrator"]
            # Per-node sample metrics are deliberately not carried through.
            assert "systemStats" not in node
            assert "interestingStats" not in node

        # terseClusterInfo names the orchestrator by otpNode; a hostname
        # comparison would leave every node unflagged without raising.
        if cluster["orchestrator_known"]:
            orchestrators = [n for n in nodes if n["is_orchestrator"]]
            assert len(orchestrators) == 1, (
                f"Expected exactly one orchestrator, got {len(orchestrators)}; "
                f"orchestrator={cluster['orchestrator']!r}"
            )

        # The rollups must agree with the per-node rows they summarise.
        assert cluster["unhealthy_nodes"] == [
            n["hostname"] for n in nodes if n["status"] != "healthy"
        ]
        assert cluster["inactive_nodes"] == [
            n["hostname"] for n in nodes if n["clusterMembership"] != "active"
        ]
        assert sum(cluster["nodes_by_status"].values()) == len(nodes)

        # Pre-signed failover/eject URLs must never reach the caller.
        assert "controllers" not in cluster
        assert "failOver" not in str(payload)


@pytest.mark.asyncio
async def test_get_cluster_health_snapshot_is_internally_consistent() -> None:
    """Verify the snapshot describes one coherent view of the cluster.

    The tool reads /pools/default, nodeServices and terseClusterInfo from a
    single node and abandons a host if any one of the three fails, rather than
    merging payloads fetched from different nodes — two nodes can disagree
    about membership mid-rebalance. Unit tests cover that with mocks; this
    checks the guarantee holds end to end, where a regression that spread the
    reads across hosts would show up as the three payloads disagreeing.

    Called twice because the failure is per-call: a snapshot stitched from two
    nodes can look self-consistent once and name a different node set or
    orchestrator on the next call against an unchanged cluster.
    """
    async with create_mcp_session() as session:
        responses = [
            await session.call_tool("get_cluster_health_snapshot") for _ in range(2)
        ]
        if any(is_error_response(r) for r in responses):
            # Only the documented Capella rejection is acceptable here.
            assert "Capella" in str(extract_payload(responses[0]))
            return
        first, second = (extract_payload(r) for r in responses)

        for payload in (first, second):
            nodes = payload["nodes"]
            cluster = payload["cluster"]

            # Every node in /pools/default must have been joined to its
            # nodeServices entry: an active node always serves management, so
            # empty ports here mean the two payloads named different nodes.
            for node in nodes:
                if node["clusterMembership"] == "active":
                    assert node["service_ports"], (
                        f"Active node {node['hostname']} has no service ports — "
                        f"the nodeServices join missed it"
                    )
                    assert node["reachable_address"], (
                        f"Active node {node['hostname']} has no reachable address"
                    )

            # terseClusterInfo's orchestrator must name a node that
            # /pools/default also reported, by otpNode.
            if cluster["orchestrator_known"]:
                assert cluster["orchestrator"] in {n["otpNode"] for n in nodes}, (
                    f"Orchestrator {cluster['orchestrator']!r} is not among the "
                    f"nodes reported: {[n['otpNode'] for n in nodes]}"
                )

        # An unchanged cluster must describe the same topology twice.
        assert [n["otpNode"] for n in first["nodes"]] == [
            n["otpNode"] for n in second["nodes"]
        ]
        assert first["cluster"]["orchestrator"] == second["cluster"]["orchestrator"]
