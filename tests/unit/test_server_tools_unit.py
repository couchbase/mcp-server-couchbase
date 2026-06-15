"""Unit tests for server tool error / branch paths.

The integration suite exercises the happy paths against a real cluster.
These unit tests cover the failure branches that can't reasonably be
reached against a live cluster:

- test_cluster_connection returns an error envelope on connect failure.
- get_scopes_and_collections_in_bucket re-raises SDK errors.
- get_scopes_in_bucket re-raises SDK errors.
- get_cluster_health_and_services returns an error envelope on ping failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cb_mcp.tools.server import (
    get_cluster_health_and_services,
    get_scopes_and_collections_in_bucket,
    get_scopes_in_bucket,
)
from cb_mcp.tools.server import (
    # Aliased so pytest doesn't collect the tool function itself as a test.
    test_cluster_connection as cluster_connection_tool,
)


def _make_ctx(cluster: MagicMock | None = None) -> SimpleNamespace:
    """Build a fake Context with a cluster_provider that returns *cluster*."""
    provider = SimpleNamespace(get_cluster=lambda c: cluster)
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster_provider=provider,
            )
        )
    )


class TestTestClusterConnection:
    """test_cluster_connection error envelope."""

    def test_returns_error_envelope_on_failure(self) -> None:
        """When get_cluster_connection raises, the tool must return a
        structured error response rather than propagating the exception."""
        ctx = _make_ctx(cluster=None)

        # Force get_cluster_connection to raise by patching it at the module
        # path the tool imports from.
        with patch(
            "cb_mcp.tools.server.get_cluster_connection",
            side_effect=Exception("auth failed"),
        ):
            result = cluster_connection_tool(ctx)

        assert result == {
            "status": "error",
            "cluster_connected": False,
            "bucket_connected": False,
            "bucket_name": None,
            "error": "auth failed",
            "message": "Failed to connect to Couchbase cluster",
        }

    def test_returns_success_envelope_on_connect(self) -> None:
        """Happy path returns success with bucket_connected=False when no
        bucket_name is supplied."""
        cluster = MagicMock()
        ctx = _make_ctx(cluster=cluster)

        with patch(
            "cb_mcp.tools.server.get_cluster_connection",
            return_value=cluster,
        ):
            result = cluster_connection_tool(ctx)

        assert result["status"] == "success"
        assert result["cluster_connected"] is True
        assert result["bucket_connected"] is False
        assert result["bucket_name"] is None

    def test_bucket_connection_attempted_when_name_provided(self) -> None:
        """A bucket_name argument should drive a connect_to_bucket call and
        set bucket_connected=True on success."""
        cluster = MagicMock()
        ctx = _make_ctx(cluster=cluster)

        with (
            patch(
                "cb_mcp.tools.server.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.server.connect_to_bucket",
                return_value=MagicMock(),
            ) as mock_connect_bucket,
        ):
            result = cluster_connection_tool(ctx, bucket_name="travel-sample")

        mock_connect_bucket.assert_called_once_with(cluster, "travel-sample")
        assert result["status"] == "success"
        assert result["bucket_connected"] is True
        assert result["bucket_name"] == "travel-sample"


class TestGetScopesAndCollectionsInBucket:
    """get_scopes_and_collections_in_bucket: error and happy path."""

    def test_propagates_collection_manager_failure(self) -> None:
        """SDK failures must be re-raised — callers need to see why a bucket
        introspection failed rather than getting an empty result."""
        cluster = MagicMock()
        bucket = MagicMock()
        bucket.collections.side_effect = Exception("collections RPC failed")
        ctx = _make_ctx(cluster=cluster)

        with (
            patch(
                "cb_mcp.tools.server.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.server.connect_to_bucket",
                return_value=bucket,
            ),
        ):
            try:
                get_scopes_and_collections_in_bucket(ctx, "b")
            except Exception as e:
                assert "collections RPC failed" in str(e)
                return
            raise AssertionError("expected exception")

    def test_returns_scope_to_collection_map(self) -> None:
        """Happy path: produces a {scope: [collection, ...]} mapping."""
        cluster = MagicMock()
        bucket = MagicMock()
        # Two scopes, each with two collections.
        scope_a = SimpleNamespace(
            name="_default",
            collections=[
                SimpleNamespace(name="_default"),
                SimpleNamespace(name="users"),
            ],
        )
        scope_b = SimpleNamespace(
            name="analytics",
            collections=[SimpleNamespace(name="events")],
        )
        bucket.collections.return_value.get_all_scopes.return_value = [
            scope_a,
            scope_b,
        ]
        ctx = _make_ctx(cluster=cluster)

        with (
            patch(
                "cb_mcp.tools.server.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.server.connect_to_bucket",
                return_value=bucket,
            ),
        ):
            result = get_scopes_and_collections_in_bucket(ctx, "b")

        assert result == {
            "_default": ["_default", "users"],
            "analytics": ["events"],
        }


class TestGetScopesInBucket:
    """get_scopes_in_bucket: error and happy path."""

    def test_propagates_failure(self) -> None:
        """SDK failure must propagate so callers see the actual root cause."""
        cluster = MagicMock()
        bucket = MagicMock()
        bucket.collections.side_effect = Exception("scopes RPC failed")
        ctx = _make_ctx(cluster=cluster)

        with (
            patch(
                "cb_mcp.tools.server.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.server.connect_to_bucket",
                return_value=bucket,
            ),
        ):
            try:
                get_scopes_in_bucket(ctx, "b")
            except Exception as e:
                assert "scopes RPC failed" in str(e)
                return
            raise AssertionError("expected exception")

    def test_returns_scope_names(self) -> None:
        """Happy path returns just the list of scope names."""
        cluster = MagicMock()
        bucket = MagicMock()
        bucket.collections.return_value.get_all_scopes.return_value = [
            SimpleNamespace(name="_default"),
            SimpleNamespace(name="analytics"),
        ]
        ctx = _make_ctx(cluster=cluster)

        with (
            patch(
                "cb_mcp.tools.server.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.server.connect_to_bucket",
                return_value=bucket,
            ),
        ):
            result = get_scopes_in_bucket(ctx, "b")

        assert result == ["_default", "analytics"]


class TestGetClusterHealthAndServices:
    """get_cluster_health_and_services: error envelope and bucket-scoped path."""

    def test_returns_error_envelope_on_failure(self) -> None:
        """A ping failure must be reported as a structured error response."""
        cluster = MagicMock()
        cluster.ping.side_effect = Exception("ping timeout")
        ctx = _make_ctx(cluster=cluster)

        with patch(
            "cb_mcp.tools.server.get_cluster_connection",
            return_value=cluster,
        ):
            result = get_cluster_health_and_services(ctx)

        assert result["status"] == "error"
        assert "ping timeout" in result["error"]
        assert "Failed to get cluster health" in result["message"]

    def test_cluster_level_ping_when_no_bucket(self) -> None:
        """No bucket_name means we ping at the cluster level."""
        cluster = MagicMock()
        ping_result = MagicMock()
        ping_result.as_json.return_value = '{"services": {}}'
        cluster.ping.return_value = ping_result
        ctx = _make_ctx(cluster=cluster)

        with patch(
            "cb_mcp.tools.server.get_cluster_connection",
            return_value=cluster,
        ):
            result = get_cluster_health_and_services(ctx)

        cluster.ping.assert_called_once()
        assert result["status"] == "success"
        assert result["data"] == {"services": {}}

    def test_bucket_level_ping_when_bucket_supplied(self) -> None:
        """A bucket_name must route the ping through bucket.ping()."""
        cluster = MagicMock()
        bucket = MagicMock()
        ping_result = MagicMock()
        ping_result.as_json.return_value = '{"services": {"kv": []}}'
        bucket.ping.return_value = ping_result
        ctx = _make_ctx(cluster=cluster)

        with (
            patch(
                "cb_mcp.tools.server.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.server.connect_to_bucket",
                return_value=bucket,
            ),
        ):
            result = get_cluster_health_and_services(ctx, bucket_name="b")

        bucket.ping.assert_called_once()
        cluster.ping.assert_not_called()
        assert result["status"] == "success"
        assert result["data"] == {"services": {"kv": []}}
