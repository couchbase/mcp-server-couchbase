"""Unit tests for the Server Async Request API tools.

Ported from the ``analytics-mcp`` prototype (branch
``DA-2027/Add-enterprise-tools``). Mocks both the cluster and the handle
registry so the whole handle lifecycle (start -> poll -> fetch / discard /
cancel) can be covered without a live cluster, including the error branches
-- which are returned as {"success": False, "error": ...}, never raised.

Also covers the read-only wiring that the prototype never had:
``operational_insights_run_query_async`` passes ``QueryOptions(readonly=True)`` to the SDK itself
whenever the server is in read-only mode or the caller's token lacks the
write scope, and additionally blocks ``COPY ... TO`` client-side under those
same conditions -- mirroring ``operational_insights_run_query_sync``'s tests in
test_operational_insights_query_tools_unit.py.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from _operational_insights_fakes import make_operational_insights_ctx
from couchbase_operational_insights.errors import (
    TimeoutError as OperationalInsightsTimeoutError,
)

from cb_mcp.servers.operational_insights.cli import (
    OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT_KEY,
)
from cb_mcp.servers.operational_insights.constants import (
    DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT,
    MAX_ASYNC_QUERY_RESULT_TIMEOUT,
)
from cb_mcp.tools.operational_insights.query import (
    operational_insights_cancel_async_query,
    operational_insights_discard_async_query_results,
    operational_insights_get_async_query_results,
    operational_insights_run_query_async,
)
from cb_mcp.utils.constants import SCOPE_READ, SCOPE_WRITE
from cb_mcp.utils.operational_insights.handle_registry import (
    QueryResultsRegistry,
    UnknownHandleError,
)

TIMEOUT_ENV_VAR = "CB_MCP_OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT"


def _make_ctx(*, read_only_mode: bool = False):
    """Build a ctx carrying a mock cluster and its (real) handle registry.

    Returns ``(ctx, cluster, registry)``; the registry is real rather than
    mocked so the tests exercise the actual token minting/eviction the tools
    depend on.
    """
    ctx, cluster = make_operational_insights_ctx(read_only_mode=read_only_mode)
    registry = ctx.request_context.lifespan_context.cluster_provider.handle_registry
    return ctx, cluster, registry


def _make_result(rows: list, result_count: int = 1, result_size: int = 42) -> MagicMock:
    """A mock query result with the metadata chain the tools read.

    Timing accessors return ``timedelta`` like the real SDK does, so the
    conversion to JSON-safe milliseconds is actually exercised.
    """
    result = MagicMock()
    # The tool streams via rows(); get_all_rows is kept for any test that
    # still asserts against the older accessor.
    result.get_all_rows.return_value = rows
    # A fresh iterator per call, mirroring the SDK: the server keeps the
    # result buffers after a fetch, so re-fetching a handle must yield the
    # rows again rather than an exhausted stream.
    result.rows.side_effect = lambda: iter(rows)
    meta = result.metadata.return_value
    meta.warnings.return_value = []
    metrics = meta.metrics.return_value
    metrics.elapsed_time.return_value = timedelta(milliseconds=12.5)
    metrics.execution_time.return_value = timedelta(milliseconds=3)
    metrics.result_count.return_value = result_count
    metrics.result_size.return_value = result_size
    metrics.processed_objects.return_value = 7
    return result


def _start(ctx, cluster, statement: str = "SELECT 1 AS one") -> tuple[str, MagicMock]:
    """Run operational_insights_run_query_async and return (token, the mock handle it registered)."""
    handle = MagicMock()
    cluster.start_query.return_value = handle
    with patch(
        "cb_mcp.tools.operational_insights.query.get_access_token",
        return_value=None,
    ):
        result = operational_insights_run_query_async(ctx, statement)
    assert result["success"] is True
    return result["query_handle"], handle


class TestRunQueryAsync:
    def test_returns_handle_token(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)

        cluster.start_query.assert_called_once_with("SELECT 1 AS one")
        assert registry.get(token).handle is handle
        assert registry.count() == 1

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster, registry = _make_ctx()
        cluster.start_query.side_effect = Exception("syntax error")

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            result = operational_insights_run_query_async(ctx, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }
        assert registry.count() == 0

    def test_passes_readonly_true_under_read_only_mode(self) -> None:
        ctx, cluster, _ = _make_ctx(read_only_mode=True)
        cluster.start_query.return_value = MagicMock()

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            operational_insights_run_query_async(ctx, "SELECT 1 AS one")

        args, _kwargs = cluster.start_query.call_args
        assert args[0] == "SELECT 1 AS one"
        assert args[1]["readonly"] is True

    def test_does_not_pass_readonly_when_mode_off_and_no_token(self) -> None:
        ctx, cluster, _ = _make_ctx(read_only_mode=False)
        cluster.start_query.return_value = MagicMock()

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            operational_insights_run_query_async(ctx, "SELECT 1 AS one")

        cluster.start_query.assert_called_once_with("SELECT 1 AS one")

    def test_passes_readonly_true_for_a_read_only_token(self) -> None:
        ctx, cluster, _ = _make_ctx(read_only_mode=False)
        cluster.start_query.return_value = MagicMock()
        token = SimpleNamespace(scopes=[SCOPE_READ])

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=token,
        ):
            operational_insights_run_query_async(ctx, "SELECT 1 AS one")

        args, _ = cluster.start_query.call_args
        assert args[1]["readonly"] is True

    def test_does_not_pass_readonly_for_a_write_token(self) -> None:
        ctx, cluster, _ = _make_ctx(read_only_mode=False)
        cluster.start_query.return_value = MagicMock()
        token = SimpleNamespace(scopes=[SCOPE_READ, SCOPE_WRITE])

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=token,
        ):
            operational_insights_run_query_async(ctx, "SELECT 1 AS one")

        cluster.start_query.assert_called_once_with("SELECT 1 AS one")


class TestRunQueryAsyncBlocksCopyToUnderReadOnly:
    def test_blocked_under_read_only_mode(self) -> None:
        ctx, cluster, _ = _make_ctx(read_only_mode=True)

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            result = operational_insights_run_query_async(
                ctx, "COPY ds TO 's3://bucket/path'"
            )

        assert result["success"] is False
        assert "COPY" in result["error"]
        cluster.start_query.assert_not_called()

    def test_allowed_in_write_mode(self) -> None:
        ctx, cluster, _ = _make_ctx(read_only_mode=False)
        cluster.start_query.return_value = MagicMock()

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            result = operational_insights_run_query_async(
                ctx, "COPY ds TO 's3://bucket/path'"
            )

        assert result["success"] is True
        cluster.start_query.assert_called_once_with("COPY ds TO 's3://bucket/path'")


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

        result = operational_insights_get_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["rows"] == [{"one": 1}]
        assert result["row_count"] == 1
        assert result["metadata"] == {
            "warnings": [],
            "metrics": {
                "elapsed_time_ms": 12.5,
                "execution_time_ms": 3.0,
                "result_count": 1,
                "result_size": 42,
                "processed_objects": 7,
            },
        }
        # The server does not free buffers on fetch, so the token stays
        # valid for a re-fetch or an explicit discard.
        assert registry.count() == 1

    def test_checks_status_before_every_fetch(self) -> None:
        """Readiness is re-derived on each call -- nothing is cached."""
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.return_value = _make_result(
            [{"two": 2}]
        )

        operational_insights_get_async_query_results(ctx, token)
        operational_insights_get_async_query_results(ctx, token)

        assert handle.fetch_status.call_count == 2

    def test_reports_not_ready_and_keeps_the_handle(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = operational_insights_get_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["ready"] is False
        assert "rows" not in result
        # The query stays tracked so it can be checked again later.
        assert registry.count() == 1

    def test_unknown_token_returns_error_envelope(self) -> None:
        ctx, _, _ = _make_ctx()

        result = operational_insights_get_async_query_results(ctx, "nope")

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

        first = operational_insights_get_async_query_results(ctx, token)
        second = operational_insights_get_async_query_results(ctx, token)

        assert first["rows"] == [{"one": 1}]
        assert second["rows"] == [{"one": 1}]
        assert registry.count() == 1

    def test_discard_after_fetch_releases_buffers(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value
        result_handle.fetch_results.return_value = _make_result([{"one": 1}])

        operational_insights_get_async_query_results(ctx, token)
        result = operational_insights_discard_async_query_results(ctx, token)

        assert result["discarded"] is True
        result_handle.discard_results.assert_called_once()
        assert registry.count() == 0

    def test_not_ready_returns_ready_false_without_evicting(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = operational_insights_get_async_query_results(ctx, token)

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

        result = operational_insights_get_async_query_results(ctx, token)

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

        result = operational_insights_get_async_query_results(ctx, token)

        assert result["success"] is True
        metrics = result["metadata"]["metrics"]
        assert metrics["result_size"] is None
        # Every other field survives the one failed read.
        assert metrics["result_count"] == 1
        assert metrics["elapsed_time_ms"] == 12.5

    def test_returns_error_envelope_on_fetch_error(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.side_effect = Exception("boom")

        result = operational_insights_get_async_query_results(ctx, token)

        assert result == {
            "success": False,
            "error": "boom",
            "query_handle": token,
        }


class TestAsyncQueryTimeouts:
    """SDK timeouts become a message that says what timed out and which setting to change."""

    def test_download_timeout_says_query_finished_and_names_setting(self) -> None:
        ctx, cluster, registry = _make_ctx()
        ctx.request_context.lifespan_context.settings = {
            OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT_KEY: 45
        }
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result = MagicMock()
        result.rows.side_effect = OperationalInsightsTimeoutError(
            message="Unable to complete iteration. Request timed out."
        )
        status.result_handle.return_value.fetch_results.return_value = result

        out = operational_insights_get_async_query_results(ctx, token)

        assert out["success"] is False
        assert out["ready"] is True
        assert out["timed_out"] is True
        assert out["query_handle"] == token
        assert "query finished successfully" in out["error"]
        assert "45-second" in out["error"]
        assert "Request timed out" not in out["error"]
        assert TIMEOUT_ENV_VAR in out["suggestion"]
        assert "currently 45 seconds" in out["suggestion"]
        # The results are still on the server, so the token must survive.
        registry.get(token)

    def test_download_timeout_from_fetch_results_call(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.side_effect = (
            OperationalInsightsTimeoutError(message="Request timed out.")
        )

        out = operational_insights_get_async_query_results(ctx, token)

        assert out["ready"] is True
        assert out["timed_out"] is True
        # No settings configured: the default is reported.
        assert f"{DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT}-second" in out["error"]

    def test_download_timeout_at_maximum_does_not_suggest_raising(self) -> None:
        ctx, cluster, _ = _make_ctx()
        ctx.request_context.lifespan_context.settings = {
            OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT_KEY: (
                MAX_ASYNC_QUERY_RESULT_TIMEOUT
            )
        }
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.side_effect = (
            OperationalInsightsTimeoutError(message="Request timed out.")
        )

        out = operational_insights_get_async_query_results(ctx, token)

        assert "already at its maximum" in out["suggestion"]
        assert "can raise" not in out["suggestion"]

    def test_error_fits_under_the_length_cap(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        status.result_handle.return_value.fetch_results.side_effect = (
            OperationalInsightsTimeoutError(message="Request timed out.")
        )

        out = operational_insights_get_async_query_results(ctx, token)

        assert not out["error"].endswith("[truncated]")

    def test_status_timeout_is_not_reported_as_a_finished_query(self) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.side_effect = OperationalInsightsTimeoutError(
            message="Request timed out."
        )

        out = operational_insights_get_async_query_results(ctx, token)

        assert out["success"] is False
        assert out["timed_out"] is True
        assert "ready" not in out
        assert "check the query status" in out["error"]
        assert TIMEOUT_ENV_VAR in out["suggestion"]

    @pytest.mark.parametrize(
        ("tool", "action"),
        [
            (operational_insights_discard_async_query_results, "discard the results"),
            (operational_insights_cancel_async_query, "cancel the query"),
        ],
    )
    def test_discard_and_cancel_timeouts(self, tool, action) -> None:
        ctx, cluster, _ = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.side_effect = OperationalInsightsTimeoutError(
            message="Request timed out."
        )

        out = tool(ctx, token)

        assert out["timed_out"] is True
        assert action in out["error"]
        assert out["query_handle"] == token

    def test_start_timeout(self) -> None:
        ctx, cluster, _ = _make_ctx()
        cluster.start_query.side_effect = OperationalInsightsTimeoutError(
            message="Request timed out."
        )
        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            out = operational_insights_run_query_async(ctx, "SELECT 1")

        assert out["timed_out"] is True
        assert "start the query" in out["error"]
        assert out["statement"] == "SELECT 1"


class TestDiscardAsyncQueryResults:
    def test_discards_ready_results_and_evicts(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value

        result = operational_insights_discard_async_query_results(ctx, token)

        assert result == {"success": True, "query_handle": token, "discarded": True}
        result_handle.discard_results.assert_called_once()
        assert registry.count() == 0

    def test_not_ready_is_a_no_op_that_keeps_the_handle(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = operational_insights_discard_async_query_results(ctx, token)

        assert result["success"] is True
        assert result["discarded"] is False
        # Reports readiness with the same field name the fetch tool uses, so
        # a caller can tell "still running" from other discard failures.
        assert result["ready"] is False
        assert registry.count() == 1

    def test_unknown_token_returns_error_envelope(self) -> None:
        ctx, _, _ = _make_ctx()

        result = operational_insights_discard_async_query_results(ctx, "nope")

        assert result["success"] is False
        assert "nope" in result["error"]


class TestCancelAsyncQuery:
    def test_cancels_running_query_and_evicts(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.return_value.results_ready.return_value = False

        result = operational_insights_cancel_async_query(ctx, token)

        assert result == {"success": True, "query_handle": token, "cancelled": True}
        handle.cancel.assert_called_once()
        assert registry.count() == 0

    def test_completed_query_is_not_cancelled_and_token_survives(self) -> None:
        """The server 404s a cancel for a finished query and the SDK hides
        it, so the tool checks status first rather than reporting a false
        success."""
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True

        result = operational_insights_cancel_async_query(ctx, token)

        assert result["success"] is True
        assert result["cancelled"] is False
        assert "operational_insights_discard_async_query_results" in result["message"]
        # cancel() must NOT have been attempted.
        handle.cancel.assert_not_called()
        # The entry survives so the discard the message recommends works.
        assert registry.count() == 1

    def test_discard_works_after_a_refused_cancel(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result_handle = status.result_handle.return_value

        operational_insights_cancel_async_query(ctx, token)
        discarded = operational_insights_discard_async_query_results(ctx, token)

        assert discarded["discarded"] is True
        result_handle.discard_results.assert_called_once()
        assert registry.count() == 0

    def test_returns_error_envelope_on_status_error(self) -> None:
        ctx, cluster, registry = _make_ctx()
        token, handle = _start(ctx, cluster)
        handle.fetch_status.side_effect = Exception("status unavailable")

        result = operational_insights_cancel_async_query(ctx, token)

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

        result = operational_insights_cancel_async_query(ctx, token)

        assert result == {
            "success": False,
            "error": "already finished",
            "query_handle": token,
        }
        # Cancel failed, so the handle is deliberately still tracked.
        assert registry.count() == 1

    def test_unknown_token_returns_error_envelope(self) -> None:
        ctx, _, _ = _make_ctx()

        result = operational_insights_cancel_async_query(ctx, "nope")

        assert result["success"] is False
        assert "nope" in result["error"]


class TestQueryResultsRegistry:
    def test_tokens_are_unique_per_registration(self) -> None:
        registry = QueryResultsRegistry()

        first = registry.register(MagicMock(), "SELECT 1")
        second = registry.register(MagicMock(), "SELECT 2")

        assert first != second
        assert registry.count() == 2

    def test_get_raises_for_unknown_token(self) -> None:
        registry = QueryResultsRegistry()

        with pytest.raises(UnknownHandleError):
            registry.get("missing")

    def test_unknown_handle_error_stringifies_without_quotes(self) -> None:
        """DA-2354: the message must not inherit ``KeyError``'s repr quoting."""
        registry = QueryResultsRegistry()

        with pytest.raises(UnknownHandleError) as excinfo:
            registry.get("missing")

        assert str(excinfo.value) == excinfo.value.args[0]
        assert not str(excinfo.value).startswith(("'", '"'))

    def test_remove_is_idempotent(self) -> None:
        registry = QueryResultsRegistry()
        token = registry.register(MagicMock(), "SELECT 1")

        registry.remove(token)
        registry.remove(token)

        assert registry.count() == 0

    def test_clear_evicts_every_entry(self) -> None:
        registry = QueryResultsRegistry()
        registry.register(MagicMock(), "SELECT 1")
        registry.register(MagicMock(), "SELECT 2")

        registry.clear()

        assert registry.count() == 0

    def test_clear_on_empty_registry_is_a_no_op(self) -> None:
        registry = QueryResultsRegistry()

        registry.clear()  # must not raise

        assert registry.count() == 0


@pytest.mark.parametrize(
    "tool",
    [
        operational_insights_get_async_query_results,
        operational_insights_discard_async_query_results,
        operational_insights_cancel_async_query,
    ],
)
def test_unknown_token_error_is_not_quoted(tool) -> None:
    """DA-2354: every handle-taking tool reports the message unquoted."""
    ctx, _, _ = _make_ctx()

    error = tool(ctx, "bogus")["error"]

    assert error == (
        "Unknown query_handle 'bogus'. It may be invalid, already "
        "discarded/cancelled, or created by a different server process."
    )
