"""Unit tests for query execution tools.

Mocks the cluster so these tests can cover the error branches without a live
cluster. run_query_sync returns {"success": False, "error": ...} on error;
explain_query raises instead (it follows the read-tool convention since it
never executes the statement it's given).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ea_mcp.tools.query import explain_query, run_query_sync


def _make_ctx_with_cluster() -> tuple[SimpleNamespace, MagicMock]:
    cluster = MagicMock()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(cluster=cluster)
        )
    )
    return ctx, cluster


class TestRunQuerySync:
    def test_returns_success_envelope(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [{"one": 1}]

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            result = run_query_sync(ctx, "SELECT 1 AS one")

        assert result == {"success": True, "rows": [{"one": 1}], "row_count": 1}

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("syntax error")

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            result = run_query_sync(ctx, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }


class TestExplainQuery:
    def test_prepends_explain_to_statement(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [{"plan": {}}]

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            result = explain_query(ctx, "SELECT 1 AS one")

        cluster.execute_query.assert_called_once_with("EXPLAIN SELECT 1 AS one")
        assert result == [{"plan": {}}]

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("syntax error")

        with (
            patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster),
            pytest.raises(Exception, match="syntax error"),
        ):
            explain_query(ctx, "SELECT bad(")
