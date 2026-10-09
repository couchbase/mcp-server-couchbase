"""Unit tests for the query-service admin (N1QL Admin REST API) tools.

The integration suite exercises the happy paths against a real cluster. These
unit tests cover the failure branches and the multi-node fan-out semantics
that can't reasonably be exercised against a live single-node test cluster:

- get_cluster_query_vitals / get_active_queries reject Capella connections and
  unresolvable query endpoints up front, without attempting a REST call.
- get_cluster_query_vitals merges per-node vitals keyed by node, and reports
  (rather than fails on) a node that could not be reached, as long as at
  least one other node answered.
- get_active_queries merges per-node active-request arrays into one list, and
  reports (rather than fails on) a node that could not be reached.
- Both return an error envelope, rather than raising, when every query node
  fails.
- delete_active_query tries every discovered query node in turn, stopping at
  the first one that reports success; a "not found" on a node is not a
  failure, just a reason to try the next one. It rejects Capella connections
  and an empty request_id up front, and returns a structured failure when no
  node reports having the request.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from cb_mcp.tools.operational.query_admin import (
    delete_active_query,
    get_active_queries,
    get_cluster_query_vitals,
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


def _patch_endpoints(endpoints: list[str] | None = None):
    """Patch the SDK-backed query-endpoint resolution.

    The tools ask the SDK where the query service listens rather than
    appending the default port to the connection string, so these tests stub
    that resolution instead of varying the connection string's hosts.
    """
    resolved = ["localhost:8093"] if endpoints is None else endpoints
    return patch.multiple(
        "cb_mcp.tools.operational.query_admin",
        resolve_query_endpoints=MagicMock(return_value=resolved),
        get_cluster_connection=MagicMock(return_value=MagicMock()),
    )


def _patch_httpx_client(side_effect_method: str, side_effect):
    """Patch httpx.Client so *side_effect_method* ("get"/"delete") returns *side_effect*."""
    mock_client_cm = MagicMock()
    mock_client = MagicMock()
    setattr(mock_client, side_effect_method, MagicMock(side_effect=side_effect))
    mock_client_cm.__enter__.return_value = mock_client
    mock_client_cm.__exit__.return_value = False
    return (
        patch(
            "cb_mcp.tools.operational.query_admin.httpx.Client",
            return_value=mock_client_cm,
        ),
        mock_client,
    )


def _ok_response(payload) -> MagicMock:
    """Build a mock httpx.Response that mimics .raise_for_status / .json for a 2xx."""
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = payload
    return response


def _status_response(status_code: int) -> MagicMock:
    """Build a mock httpx.Response for a non-2xx that raises on .raise_for_status()."""
    response = MagicMock()
    response.status_code = status_code
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            f"{status_code} error", request=MagicMock(), response=response
        )
    )
    return response


class TestGetClusterQueryVitals:
    """get_cluster_query_vitals: Capella rejection, per-node fan-out, merge."""

    def test_returns_error_envelope_on_missing_settings(self) -> None:
        ctx = _make_ctx_with_settings({})

        result = get_cluster_query_vitals(ctx)

        assert result["status"] == "error"
        assert "Failed to get cluster query vitals" in result["message"]

    def test_rejects_capella_connection_without_rest_call(self) -> None:
        ctx = _make_ctx_with_settings(_CAPELLA_SETTINGS)

        with patch(
            "cb_mcp.tools.operational.query_admin.httpx.Client"
        ) as mock_client_cls:
            result = get_cluster_query_vitals(ctx)

        mock_client_cls.assert_not_called()
        assert result["status"] == "error"
        assert "Capella" in result["error"]

    def test_rejects_no_resolvable_endpoints_without_rest_call(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)

        with (
            _patch_endpoints([]),
            patch(
                "cb_mcp.tools.operational.query_admin.httpx.Client"
            ) as mock_client_cls,
        ):
            result = get_cluster_query_vitals(ctx)

        mock_client_cls.assert_not_called()
        assert result["status"] == "error"
        assert "No query-service endpoints" in result["error"]

    def test_single_node_success(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        vitals_payload = {"uptime": "1h", "version": "8.0.0"}
        client_patch, mock_client = _patch_httpx_client(
            "get", [_ok_response(vitals_payload)]
        )

        with _patch_endpoints(["localhost:8093"]), client_patch:
            result = get_cluster_query_vitals(ctx)

        called_url = mock_client.get.call_args[0][0]
        assert called_url == "http://localhost:8093/admin/vitals"
        assert mock_client.get.call_args[1]["auth"] == ("admin", "password")
        assert result == {
            "status": "success",
            "vitals": {"localhost:8093": vitals_payload},
        }

    def test_multi_node_fan_out_merges_by_node(self) -> None:
        """Every discovered node is queried — not just the first that answers."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        payload_a = {"uptime": "1h"}
        payload_b = {"uptime": "2h"}
        client_patch, mock_client = _patch_httpx_client(
            "get", [_ok_response(payload_a), _ok_response(payload_b)]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_cluster_query_vitals(ctx)

        assert mock_client.get.call_count == 2
        assert result == {
            "status": "success",
            "vitals": {"node1:8093": payload_a, "node2:8093": payload_b},
        }

    def test_partial_failure_reports_unreachable_node(self) -> None:
        """One node down and one up is still a success, with the failure reported."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, _mock_client = _patch_httpx_client(
            "get", [httpx.ConnectError("refused"), _ok_response({"uptime": "2h"})]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_cluster_query_vitals(ctx)

        assert result["status"] == "success"
        assert result["vitals"] == {"node2:8093": {"uptime": "2h"}}
        assert len(result["unreachable_nodes"]) == 1
        assert result["unreachable_nodes"][0]["node"] == "node1:8093"

    def test_returns_error_envelope_when_every_node_fails(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        error = httpx.ConnectError("refused")
        client_patch, _ = _patch_httpx_client("get", [error, error])

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_cluster_query_vitals(ctx)

        assert result["status"] == "error"
        assert "node1:8093" in result["error"] and "node2:8093" in result["error"]


class TestGetActiveQueries:
    """get_active_queries: Capella rejection, per-node fan-out, list merge."""

    def test_rejects_capella_connection_without_rest_call(self) -> None:
        ctx = _make_ctx_with_settings(_CAPELLA_SETTINGS)

        with patch(
            "cb_mcp.tools.operational.query_admin.httpx.Client"
        ) as mock_client_cls:
            result = get_active_queries(ctx)

        mock_client_cls.assert_not_called()
        assert result["status"] == "error"
        assert "Capella" in result["error"]

    def test_merges_active_requests_across_nodes(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        node1_requests = [{"requestId": "r1", "node": "node1:8093"}]
        node2_requests = [
            {"requestId": "r2", "node": "node2:8093"},
            {"requestId": "r3", "node": "node2:8093"},
        ]
        client_patch, mock_client = _patch_httpx_client(
            "get", [_ok_response(node1_requests), _ok_response(node2_requests)]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_active_queries(ctx)

        assert mock_client.get.call_count == 2
        assert result["status"] == "success"
        assert result["active_requests"] == node1_requests + node2_requests
        assert "unreachable_nodes" not in result

    def test_empty_result_is_still_a_success_when_a_node_answers(self) -> None:
        """A node reporting no active requests is a valid, empty answer — not a failure."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, _ = _patch_httpx_client("get", [_ok_response([])])

        with _patch_endpoints(["localhost:8093"]), client_patch:
            result = get_active_queries(ctx)

        assert result == {"status": "success", "active_requests": []}

    def test_partial_failure_reports_unreachable_node(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, _ = _patch_httpx_client(
            "get", [_ok_response([]), httpx.ConnectError("refused")]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_active_queries(ctx)

        assert result["status"] == "success"
        assert result["active_requests"] == []
        assert result["unreachable_nodes"] == [
            {"node": "node2:8093", "error": "refused"}
        ]

    def test_returns_error_envelope_when_every_node_fails(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        error = httpx.ConnectError("refused")
        client_patch, _ = _patch_httpx_client("get", [error, error])

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_active_queries(ctx)

        assert result["status"] == "error"

    def test_non_list_response_is_treated_as_node_failure_not_success(self) -> None:
        """A 200 with an unexpected shape (e.g. a proxy/API envelope) must not
        be counted as a successful empty answer — it's reported as a failed
        node, same as an unreachable one."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, _ = _patch_httpx_client(
            "get", [_ok_response({"unexpected": "envelope"}), _ok_response([])]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_active_queries(ctx)

        assert result["status"] == "success"
        assert result["active_requests"] == []
        assert len(result["unreachable_nodes"]) == 1
        assert result["unreachable_nodes"][0]["node"] == "node1:8093"

    def test_all_non_list_responses_is_a_total_failure(self) -> None:
        """If every node returns an unexpected shape, that's a real failure,
        not a quiet 'zero active queries' success."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, _ = _patch_httpx_client(
            "get",
            [_ok_response({"unexpected": "envelope"}), _ok_response({"also": "bad"})],
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = get_active_queries(ctx)

        assert result["status"] == "error"


class TestDeleteActiveQuery:
    """delete_active_query: per-node try-in-turn, write-tool success/error envelope."""

    def test_rejects_empty_request_id_without_rest_call(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)

        with patch(
            "cb_mcp.tools.operational.query_admin.httpx.Client"
        ) as mock_client_cls:
            result = delete_active_query(ctx, request_id="   ")

        mock_client_cls.assert_not_called()
        assert result == {
            "success": False,
            "error": "request_id must be a non-empty string, got '   '",
            "request_id": "   ",
        }

    def test_rejects_capella_connection_without_rest_call(self) -> None:
        ctx = _make_ctx_with_settings(_CAPELLA_SETTINGS)

        with patch(
            "cb_mcp.tools.operational.query_admin.httpx.Client"
        ) as mock_client_cls:
            result = delete_active_query(ctx, request_id="abc-123")

        mock_client_cls.assert_not_called()
        assert result["success"] is False
        assert "Capella" in result["error"]

    def test_first_node_success(self) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, mock_client = _patch_httpx_client("delete", [_ok_response(None)])

        with _patch_endpoints(["localhost:8093"]), client_patch:
            result = delete_active_query(ctx, request_id="abc-123")

        called_url = mock_client.delete.call_args[0][0]
        assert called_url == "http://localhost:8093/admin/active_requests/abc-123"
        assert result == {
            "success": True,
            "request_id": "abc-123",
            "node": "localhost:8093",
        }

    def test_tries_next_node_after_not_found(self) -> None:
        """A 404 on one node means "not here", not "cancellation failed"."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, mock_client = _patch_httpx_client(
            "delete", [_status_response(404), _ok_response(None)]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = delete_active_query(ctx, request_id="abc-123")

        assert mock_client.delete.call_count == 2
        assert result == {
            "success": True,
            "request_id": "abc-123",
            "node": "node2:8093",
        }

    def test_returns_not_found_message_only_when_every_node_says_not_found(
        self,
    ) -> None:
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, _ = _patch_httpx_client(
            "delete", [_status_response(404), _status_response(404)]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = delete_active_query(ctx, request_id="abc-123")

        assert result["success"] is False
        assert "was not found" in result["error"]
        assert "abc-123" in result["error"]
        assert result["request_id"] == "abc-123"
        assert len(result["nodes_tried"]) == 2

    def test_does_not_report_not_found_when_every_node_is_unreachable(self) -> None:
        """A connection error is not the same as the server saying 'not
        found' — reporting it as such would read as 'safe, it's done' when
        cancellation was never attempted."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        error = httpx.ConnectError("refused")
        client_patch, _ = _patch_httpx_client("delete", [error, error])

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = delete_active_query(ctx, request_id="abc-123")

        assert result["success"] is False
        assert "was not found" not in result["error"]
        assert "Could not confirm cancellation" in result["error"]
        assert len(result["nodes_tried"]) == 2

    def test_mixed_not_found_and_real_failure_is_not_reported_as_not_found(
        self,
    ) -> None:
        """One node genuinely has no record of the request; the other failed
        for an unrelated reason (e.g. auth/5xx). The request may still be
        running on the node that failed, so this must not be reported as a
        clean not-found."""
        ctx = _make_ctx_with_settings(_VALID_SETTINGS)
        client_patch, _ = _patch_httpx_client(
            "delete", [_status_response(404), _status_response(500)]
        )

        with _patch_endpoints(["node1:8093", "node2:8093"]), client_patch:
            result = delete_active_query(ctx, request_id="abc-123")

        assert result["success"] is False
        assert "was not found" not in result["error"]
        assert "Could not confirm cancellation" in result["error"]
        assert len(result["nodes_tried"]) == 2
