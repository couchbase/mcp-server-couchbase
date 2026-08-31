"""Unit tests for server tool error / branch paths.

The integration suite exercises the happy paths against a real cluster.
These unit tests cover the failure branches that can't reasonably be
reached against a live cluster:

- test_cluster_connection returns an error envelope on connect failure.
- get_scopes_and_collections_in_bucket re-raises SDK errors.
- get_scopes_in_bucket re-raises SDK errors.
- get_cluster_health_and_services returns an error envelope on ping failure.
- get_cluster_health_and_services translates service_types into PingOptions and
  rejects unrecognized service type strings via the same error envelope.
- get_cluster_metrics / get_nodes_in_cluster return error envelopes on REST
  failures and success envelopes wrapping the raw REST response otherwise, and
  reject Capella connections up front without attempting the REST call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from couchbase.diagnostics import ServiceType

from cb_mcp.tools.server import (
    get_cluster_health_and_services,
    get_cluster_metrics,
    get_nodes_in_cluster,
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


def _make_ctx_with_settings(settings: dict) -> SimpleNamespace:
    """Build a fake Context exposing *settings* via get_settings(ctx)."""
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(settings=settings)
        )
    )


_VALID_SETTINGS = {
    "connection_string": "couchbase://localhost",
    "username": "admin",
    "password": "password",
}

_CAPELLA_SETTINGS = {
    "connection_string": "couchbases://cb.abc123.cloud.couchbase.com",
    "username": "admin",
    "password": "password",
}


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

    def test_service_types_filter_passed_to_cluster_ping(self) -> None:
        """service_types must be translated into a PingOptions and passed through."""
        cluster = MagicMock()
        ping_result = MagicMock()
        ping_result.as_json.return_value = '{"services": {"query": []}}'
        cluster.ping.return_value = ping_result
        ctx = _make_ctx(cluster=cluster)

        with patch(
            "cb_mcp.tools.server.get_cluster_connection",
            return_value=cluster,
        ):
            result = get_cluster_health_and_services(ctx, service_types=["query"])

        cluster.ping.assert_called_once()
        (ping_opts,) = cluster.ping.call_args.args
        assert list(ping_opts["service_types"]) == [ServiceType.Query]
        assert result["status"] == "success"
        assert result["data"] == {"services": {"query": []}}

    def test_service_types_filter_passed_to_bucket_ping(self) -> None:
        """service_types must also be passed through on the bucket-scoped path."""
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
            result = get_cluster_health_and_services(
                ctx, bucket_name="b", service_types=["key_value"]
            )

        bucket.ping.assert_called_once()
        (ping_opts,) = bucket.ping.call_args.args
        assert list(ping_opts["service_types"]) == [ServiceType.KeyValue]
        assert result["status"] == "success"

    def test_invalid_service_type_returns_error_envelope(self) -> None:
        """An unrecognized service type string must not raise past the tool boundary."""
        cluster = MagicMock()
        ctx = _make_ctx(cluster=cluster)

        with patch(
            "cb_mcp.tools.server.get_cluster_connection",
            return_value=cluster,
        ):
            result = get_cluster_health_and_services(ctx, service_types=["bogus"])

        cluster.ping.assert_not_called()
        assert result["status"] == "error"
        assert "Failed to get cluster health" in result["message"]


class TestGetClusterMetrics:
    """get_cluster_metrics: Capella rejection, REST call, and error/success envelopes."""

    @staticmethod
    def _patch_httpx_client(side_effect_method: str, side_effect):
        """Patch httpx.Client so *side_effect_method* ("get"/"post") returns *side_effect*."""
        mock_client_cm = MagicMock()
        mock_client = MagicMock()
        setattr(mock_client, side_effect_method, MagicMock(side_effect=side_effect))
        mock_client_cm.__enter__.return_value = mock_client
        mock_client_cm.__exit__.return_value = False
        return (
            patch("cb_mcp.tools.server.httpx.Client", return_value=mock_client_cm),
            mock_client,
        )

    @staticmethod
    def _ok_response(payload: list | None = None) -> MagicMock:
        """Build a mock httpx.Response that mimics .raise_for_status / .json."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = payload if payload is not None else []
        return response

    def test_returns_error_envelope_on_missing_settings(self) -> None:
        """Missing connection settings must not raise past the tool boundary."""
        ctx = _make_ctx_with_settings({})

        result = get_cluster_metrics(ctx, metrics=[{"metric": []}])

        assert result["status"] == "error"
        assert "Failed to get cluster metrics" in result["message"]

    def test_rejects_capella_connection_without_rest_call(self) -> None:
        """A Capella connection must be rejected up front, with no REST attempt."""
        ctx = _make_ctx_with_settings(_CAPELLA_SETTINGS)

        with patch("cb_mcp.tools.server.httpx.Client") as mock_client_cls:
            result = get_cluster_metrics(ctx, metrics=[{"metric": []}])

        mock_client_cls.assert_not_called()
        assert result["status"] == "error"
        assert "Capella" in result["error"]
        assert "Failed to get cluster metrics" in result["message"]

    def test_returns_success_envelope_with_rest_response(self) -> None:
        """Happy path wraps the raw stats-range response, per-spec errors and
        all, under {"status": "success", "data": ...}."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        rest_response = [
            {"data": [{"metric": {"name": "kv_ops"}, "values": []}], "errors": []},
            {"data": [], "errors": ["unrecognized metric"]},
        ]
        metrics = [{"metric": [{"label": "name", "value": "kv_ops"}]}]
        client_patch, mock_client = self._patch_httpx_client(
            "post", [self._ok_response(rest_response)]
        )

        with client_patch:
            result = get_cluster_metrics(ctx, metrics=metrics)

        called_url = mock_client.post.call_args[0][0]
        assert called_url == "http://localhost:8091/pools/default/stats/range"
        assert mock_client.post.call_args[1]["json"] == metrics
        assert mock_client.post.call_args[1]["auth"] == ("admin", "password")
        assert result == {"status": "success", "data": rest_response}

    def test_multi_host_failover(self) -> None:
        """If the first host fails, the second one should be tried."""
        settings = {**_VALID_SETTINGS, "connection_string": "couchbase://host1,host2"}
        ctx = _make_ctx_with_settings(settings)
        client_patch, mock_client = self._patch_httpx_client(
            "post",
            [httpx.ConnectError("refused"), self._ok_response([{"data": []}])],
        )

        with client_patch:
            result = get_cluster_metrics(ctx, metrics=[])

        assert result == {"status": "success", "data": [{"data": []}]}
        assert mock_client.post.call_count == 2

    def test_returns_error_envelope_when_all_hosts_fail(self) -> None:
        """When every host fails, the tool returns a structured error response."""
        settings = {**_VALID_SETTINGS, "connection_string": "couchbase://host1,host2"}
        ctx = _make_ctx_with_settings(settings)
        error = httpx.ConnectError("refused")
        client_patch, _ = self._patch_httpx_client("post", [error, error])

        with client_patch:
            result = get_cluster_metrics(ctx, metrics=[])

        assert result["status"] == "error"
        assert "host1" in result["error"] and "host2" in result["error"]
        assert "Failed to get cluster metrics" in result["message"]


class TestGetNodesInCluster:
    """get_nodes_in_cluster: Capella rejection, REST call, and error/success envelopes."""

    @staticmethod
    def _patch_httpx_client(side_effect_method: str, side_effect):
        """Patch httpx.Client so *side_effect_method* ("get"/"post") returns *side_effect*."""
        mock_client_cm = MagicMock()
        mock_client = MagicMock()
        setattr(mock_client, side_effect_method, MagicMock(side_effect=side_effect))
        mock_client_cm.__enter__.return_value = mock_client
        mock_client_cm.__exit__.return_value = False
        return (
            patch("cb_mcp.tools.server.httpx.Client", return_value=mock_client_cm),
            mock_client,
        )

    @staticmethod
    def _ok_response(payload: list | None = None) -> MagicMock:
        """Build a mock httpx.Response that mimics .raise_for_status / .json."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = payload if payload is not None else []
        return response

    def test_returns_error_envelope_on_missing_settings(self) -> None:
        """Missing connection settings must not raise past the tool boundary."""
        ctx = _make_ctx_with_settings({})

        result = get_nodes_in_cluster(ctx)

        assert result["status"] == "error"
        assert "Failed to get cluster nodes" in result["message"]

    def test_rejects_capella_connection_without_rest_call(self) -> None:
        """A Capella connection must be rejected up front, with no REST attempt."""
        ctx = _make_ctx_with_settings(_CAPELLA_SETTINGS)

        with patch("cb_mcp.tools.server.httpx.Client") as mock_client_cls:
            result = get_nodes_in_cluster(ctx)

        mock_client_cls.assert_not_called()
        assert result["status"] == "error"
        assert "Capella" in result["error"]
        assert "Failed to get cluster nodes" in result["message"]

    def test_returns_success_envelope_with_deduped_targets(self) -> None:
        """Happy path flattens+dedupes targets across response elements."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        rest_response = [
            {"targets": ["node1:18091", "node2:18091"]},
            {"targets": ["node2:18091"]},
        ]
        client_patch, mock_client = self._patch_httpx_client(
            "get", [self._ok_response(rest_response)]
        )

        with client_patch:
            result = get_nodes_in_cluster(
                ctx, use_secure_ports=False, network="external"
            )

        called_url = mock_client.get.call_args[0][0]
        assert called_url == "http://localhost:8091/prometheus_sd_config"
        assert mock_client.get.call_args[1]["params"] == {
            "type": "json",
            "port": "insecure",
            "network": "external",
        }
        assert result == {"status": "success", "data": ["node1:18091", "node2:18091"]}

    def test_multi_host_failover(self) -> None:
        """If the first host fails, the second one should be tried."""
        settings = {**_VALID_SETTINGS, "connection_string": "couchbase://host1,host2"}
        ctx = _make_ctx_with_settings(settings)
        client_patch, mock_client = self._patch_httpx_client(
            "get",
            [
                httpx.ConnectError("refused"),
                self._ok_response([{"targets": ["n:8091"]}]),
            ],
        )

        with client_patch:
            result = get_nodes_in_cluster(ctx)

        assert result == {"status": "success", "data": ["n:8091"]}
        assert mock_client.get.call_count == 2

    def test_returns_error_envelope_when_all_hosts_fail(self) -> None:
        """When every host fails, the tool returns a structured error response."""
        settings = {**_VALID_SETTINGS, "connection_string": "couchbase://host1,host2"}
        ctx = _make_ctx_with_settings(settings)
        error = httpx.ConnectError("refused")
        client_patch, _ = self._patch_httpx_client("get", [error, error])

        with client_patch:
            result = get_nodes_in_cluster(ctx)

        assert result["status"] == "error"
        assert "host1" in result["error"] and "host2" in result["error"]
        assert "Failed to get cluster nodes" in result["message"]
