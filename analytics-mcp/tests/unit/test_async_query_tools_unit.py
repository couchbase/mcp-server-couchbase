"""Unit tests for the Server Async Request API tools.

Mocks both the cluster and the handle registry so the whole handle lifecycle
(start -> poll -> fetch / discard / cancel) can be covered without a live EA
cluster, including the error branches -- which are returned as
{"success": False, "error": ...}, never raised.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ea_mcp.handle_registry import HandleRegistry, UnknownHandleError
from ea_mcp.tools.query import (
    cancel_async_query,
    discard_async_query_results,
    get_async_query_results,
    run_query_async,
)


def _make_ctx() -> tuple[SimpleNamespace, MagicMock, HandleRegistry]:
    """Build a ctx carrying a mock cluster and a real (isolated) registry.

    The registry is real rather than mocked so the tests exercise the actual
    token minting/eviction the tools depend on.
    """
    cluster = MagicMock()
    registry = HandleRegistry()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(cluster=cluster, handle_registry=registry)
        )
    )
    return ctx, cluster, registry


def _make_result(rows: list, result_count: int = 1, result_size: int = 42) -> MagicMock:
    """A mock query result with the metadata chain the tools read.

    Timing accessors return ``timedelta`` like the real SDK does, so the
    conversion to JSON-safe milliseconds is actually exercised.
    """
    result = MagicMock()
    result.get_all_rows.return_value = rows
    meta = result.metadata.return_value
    meta.request_id.return_value = "req-1"
    meta.warnings.return_value = []
    metrics = meta.metrics.return_value
    metrics.elapsed_time.return_value = timedelta(milliseconds=12.5)
    metrics.execution_time.return_value = timedelta(milliseconds=3)
    metrics.result_count.return_value = result_count
    metrics.result_size.return_value = result_size
    metrics.processed_objects.return_value = 7
    return result


def _start(ctx, cluster, statement: str = "SELECT 1 AS one") -> tuple[str, MagicMock]:
    """Run run_query_async and return (token, the mock handle it registered)."""
    handle = MagicMock()
    handle._request_id = "req-1"
    cluster.start_query.return_value = handle
    result = run_query_async(ctx, statement)
    assert result["success"] is True
    return result["query_handle"], handle


class TestRunQueryAsync:
    def test_returns_handle_token(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)

        cluster.start_query.assert_called_once_with("SELECT 1 AS one")
        assert registry.get(token).handle is handle
        assert registry.count() == 1

    def test_includes_request_id(self) -> None:
        ctx, cluster, _ = _make_ctx()
        handle = MagicMock()
        handle._request_id = "req-abc"
        cluster.start_query.return_value = handle

        result = run_query_async(ctx, "SELECT 1")

        assert result["request_id"] == "req-abc"

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster, registry = _make_ctx()
        cluster.start_query.side_effect = Exception("syntax error")

        result = run_query_async(ctx, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }
        assert registry.count() == 0


class TestGetAsyncQueryResults:
    """This tool does double duty: it reports readiness AND returns rows, so
    there is no separate status tool to check first."""

    def test_fetches_rows_and_metadata_and_keeps_token(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.return_value = _make_result(
            [{"one": 1}]
        )

        result = get_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["rows"] == [{"one": 1}]
        assert result["row_count"] == 1
        assert result["metadata"] == {
            "request_id": "req-1",
            "warnings": [],
            "metrics": {
                "elapsed_time_ms": 12.5,
                "execution_time_ms": 3.0,
                "result_count": 1,
                "result_size": 42,
                "processed_objects": 7,
            },
        }
        # EA does not free buffers on fetch, so the token stays valid for a
        # re-fetch or an explicit discard.
        assert registry.count() == 1

    def test_derives_result_handle_when_status_not_polled(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.return_value = _make_result(
            [{"two": 2}]
        )

        # No prior readiness check exists -- this tool derives it itself.
        result = get_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["rows"] == [{"two": 2}]

    def test_reports_not_ready_without_caching_a_result_handle(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = get_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["ready"] is False
        assert "rows" not in result
        # Nothing to cache yet, and the query stays tracked.
        assert registry.get(token).result_handle is None

    def test_unknown_token_returns_error_envelope(self) -> None:
        ctx, _, _ = _make_ctx()

        result = get_async_query_results(ctx, "nope")

        assert result["success"] is False
        assert "nope" in result["error"]
        assert result["query_handle"] == "nope"

    def test_can_be_fetched_twice(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.return_value = _make_result(
            [{"one": 1}]
        )

        first = get_async_query_results(ctx, token)
        second = get_async_query_results(ctx, token)

        assert first["rows"] == [{"one": 1}]
        assert second["rows"] == [{"one": 1}]
        assert registry.count() == 1

    def test_caches_derived_result_handle(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value
        result_handle.fetch_results.return_value = _make_result([{"one": 1}])

        # Fetch without a prior status poll -- the handle is derived here.
        get_async_query_results(ctx, token)

        assert registry.get(token).result_handle is result_handle

    def test_discard_after_fetch_releases_buffers(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value
        result_handle.fetch_results.return_value = _make_result([{"one": 1}])

        get_async_query_results(ctx, token)
        result = discard_async_query_results(ctx, token)

        assert result["discarded"] is True
        result_handle.discard_results.assert_called_once()
        assert registry.count() == 0

    def test_not_ready_returns_ready_false_without_evicting(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = get_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["ready"] is False
        assert "rows" not in result
        # Still tracked, so the caller can poll and fetch later.
        assert registry.count() == 1

    def test_partial_metadata_does_not_fail_the_fetch(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_obj = _make_result([{"one": 1}])
        result_obj.metadata.side_effect = Exception("no metadata")
        status.result_handle.return_value.fetch_results.return_value = result_obj

        result = get_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["rows"] == [{"one": 1}]
        assert result["metadata"] == {}

    def test_one_bad_metrics_field_does_not_lose_the_others(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_obj = _make_result([{"one": 1}])
        result_obj.metadata.return_value.metrics.return_value.result_size.side_effect = Exception(
            "not reported"
        )
        status.result_handle.return_value.fetch_results.return_value = result_obj

        result = get_async_query_results(ctx, token)

        assert result["success"] is True
        metrics = result["metadata"]["metrics"]
        assert metrics["result_size"] is None
        # Every other field survives the one failed read.
        assert metrics["result_count"] == 1
        assert metrics["elapsed_time_ms"] == 12.5
        assert result["metadata"]["request_id"] == "req-1"

    def test_returns_error_envelope_on_fetch_error(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.side_effect = Exception("boom")

        result = get_async_query_results(ctx, token)

        assert result == {
            "success": False,
            "error": "boom",
            "query_handle": token,
        }


class TestDiscardAsyncQueryResults:
    def test_discards_ready_results_and_evicts(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value

        result = discard_async_query_results(ctx, token)

        assert result == {"success": True, "query_handle": token, "discarded": True}
        result_handle.discard_results.assert_called_once()
        assert registry.count() == 0

    def test_not_ready_is_a_no_op_that_keeps_the_handle(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = discard_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["discarded"] is False
        # Reports readiness with the same field name the fetch tool uses, so a
        # caller can tell "still running" from other discard failures.
        assert result["ready"] is False
        assert registry.count() == 1

    def test_unknown_token_returns_error_envelope(self) -> None:
        ctx, _, _ = _make_ctx()

        result = discard_async_query_results(ctx, "nope")

        assert result["success"] is False
        assert "nope" in result["error"]


class TestCancelAsyncQuery:
    def test_cancels_running_query_and_evicts(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = cancel_async_query(ctx, token)

        assert result == {"success": True, "query_handle": token, "cancelled": True}
        handle.cancel.assert_called_once()
        assert registry.count() == 0

    def test_completed_query_is_not_cancelled_and_token_survives(self) -> None:
        """EA 404s a cancel for a finished query and the SDK hides it, so the
        tool checks status first rather than reporting a false success."""
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value

        result = cancel_async_query(ctx, token)

        assert result["success"] is True
        assert result["cancelled"] is False
        assert "discard_async_query_results" in result["message"]
        # cancel() must NOT have been attempted.
        handle.cancel.assert_not_called()
        # The entry survives, with the result handle cached, so the discard the
        # message recommends actually works.
        assert registry.count() == 1
        assert registry.get(token).result_handle is result_handle

    def test_discard_works_after_a_refused_cancel(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value

        cancel_async_query(ctx, token)
        discarded = discard_async_query_results(ctx, token)

        assert discarded["discarded"] is True
        result_handle.discard_results.assert_called_once()
        assert registry.count() == 0

    def test_returns_error_envelope_on_status_error(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.side_effect = Exception("status unavailable")

        result = cancel_async_query(ctx, token)

        assert result == {
            "success": False,
            "error": "status unavailable",
            "query_handle": token,
        }
        assert registry.count() == 1

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False
        handle.cancel.side_effect = Exception("already finished")

        result = cancel_async_query(ctx, token)

        assert result == {
            "success": False,
            "error": "already finished",
            "query_handle": token,
        }
        # Cancel failed, so the handle is deliberately still tracked.
        assert registry.count() == 1

    def test_unknown_token_returns_error_envelope(self) -> None:
        ctx, _, _ = _make_ctx()

        result = cancel_async_query(ctx, "nope")

        assert result["success"] is False
        assert "nope" in result["error"]


class TestHandleRegistry:
    def test_tokens_are_unique_per_registration(self) -> None:
        registry = HandleRegistry()

        first = registry.register(MagicMock(), "SELECT 1")
        second = registry.register(MagicMock(), "SELECT 2")

        assert first != second
        assert registry.count() == 2

    def test_get_raises_for_unknown_token(self) -> None:
        registry = HandleRegistry()

        with pytest.raises(UnknownHandleError):
            registry.get("missing")

    def test_remove_is_idempotent(self) -> None:
        registry = HandleRegistry()
        token = registry.register(MagicMock(), "SELECT 1")

        registry.remove(token)
        registry.remove(token)

        assert registry.count() == 0
