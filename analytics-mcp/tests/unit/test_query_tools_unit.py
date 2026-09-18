"""Unit tests for query execution tools.

Mocks the cluster so these tests can cover the error branches (returned as
{"success": False, "error": ...}, never raised) without a live cluster.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

from ea_mcp.result_config import ResultConfig
from ea_mcp.result_store import ResultStore
from ea_mcp.tools.query import explain_query, run_query_sync


def _make_ctx_with_cluster(
    result_config: ResultConfig | None = None,
    result_store: ResultStore | None = None,
) -> tuple[SimpleNamespace, MagicMock]:
    """Build a ctx with a mock cluster.

    Large-result handling defaults to off (no store, saving disabled) to match
    the server default, so results come back whole unless a test opts in.
    """
    cluster = MagicMock()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster=cluster,
                result_config=result_config or ResultConfig(),
                result_store=result_store,
            )
        )
    )
    return ctx, cluster


class TestRunQuerySync:
    def test_returns_success_envelope(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [{"one": 1}]

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            result = run_query_sync(ctx, "SELECT 1 AS one")

        # truncated: False is now always reported, so callers can tell a whole
        # result from a partial one without comparing counts.
        assert result == {
            "success": True,
            "rows": [{"one": 1}],
            "row_count": 1,
            "truncated": False,
        }

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


class TestRunQuerySyncLargeResults:
    """The size/opt-in decisions themselves are covered in
    test_result_handling_unit; these check the tool is wired to them."""

    ROWS: ClassVar[list[dict]] = [
        {"i": i, "pad": "x" * 50} for i in range(100)
    ]  # ~7 KB

    def _run(self, *, save_flag: bool, opt_in: bool, limit: int, store=None):
        config = ResultConfig(save_large_results=save_flag, truncate_bytes=limit)
        ctx, cluster = _make_ctx_with_cluster(config, store)
        cluster.execute_query.return_value.get_all_rows.return_value = self.ROWS

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            return run_query_sync(ctx, "SELECT *", save_result_if_large=opt_in)

    def test_defaults_to_not_saving(self, tmp_path: Path) -> None:
        store = ResultStore(tmp_path, 10 * 1024 * 1024)
        config = ResultConfig(save_large_results=True, truncate_bytes=1_000)
        ctx, cluster = _make_ctx_with_cluster(config, store)
        cluster.execute_query.return_value.get_all_rows.return_value = self.ROWS

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            result = run_query_sync(ctx, "SELECT *")  # opt-in omitted

        assert result["truncated"] is True
        assert "result_id" not in result
        assert store.stats()["entries"] == 0

    def test_truncates_a_large_result_without_saving_it(self) -> None:
        result = self._run(save_flag=False, opt_in=True, limit=1_000)

        assert result["success"] is True
        assert result["truncated"] is True
        assert result["total_row_count"] == 100
        assert "result_id" not in result

    def test_saves_a_large_result_when_enabled_and_opted_in(
        self, tmp_path: Path
    ) -> None:
        store = ResultStore(tmp_path, 10 * 1024 * 1024)

        result = self._run(save_flag=True, opt_in=True, limit=1_000, store=store)

        assert result["truncated"] is True
        assert result["resource_uri"] == f"ea://results/{result['result_id']}"
        assert store.read_rows(result["result_id"]) == self.ROWS

    def test_a_small_result_comes_back_whole(self, tmp_path: Path) -> None:
        store = ResultStore(tmp_path, 10 * 1024 * 1024)

        result = self._run(save_flag=True, opt_in=True, limit=10**6, store=store)

        assert result["truncated"] is False
        assert result["rows"] == self.ROWS
        assert store.stats()["entries"] == 0


class TestExplainQuery:
    def test_prepends_explain_to_statement(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [{"plan": {}}]

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            result = explain_query(ctx, "SELECT 1 AS one")

        cluster.execute_query.assert_called_once_with("EXPLAIN SELECT 1 AS one")
        assert result == {"success": True, "plan": [{"plan": {}}]}

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("syntax error")

        with patch("ea_mcp.tools.query.get_cluster_connection", return_value=cluster):
            result = explain_query(ctx, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }
