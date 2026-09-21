"""Unit tests for Operational Insights query execution tools.

Ported from the ``analytics-mcp`` prototype (branch
``DA-2027/Add-enterprise-tools``). Mocks the cluster so these tests can cover
the error branches (returned as ``{"success": False, "error": ...}``, never
raised) without a live cluster.

Also covers the read-only wiring that the prototype never had:
``run_query_sync`` passes ``QueryOptions(readonly=True)`` to the SDK itself
whenever the server is in read-only mode or the caller's token lacks the
write scope, and additionally blocks ``COPY ... TO`` client-side under those
same conditions — the server itself classifies COPY ... TO as read-only
(readonly=True alone does not block it), but it writes its result to
external storage or a KV collection.
"""

from types import SimpleNamespace
from unittest.mock import patch

from _oi_fakes import make_oi_ctx

from cb_mcp.tools.operational_insights.query import (
    _is_copy_to_statement,
    explain_query,
    run_query_sync,
)
from cb_mcp.utils.constants import SCOPE_READ, SCOPE_WRITE


class TestRunQuerySync:
    def test_returns_success_envelope(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = [{"one": 1}]

        result = run_query_sync(ctx, "SELECT 1 AS one")

        assert result == {"success": True, "rows": [{"one": 1}], "row_count": 1}

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.side_effect = Exception("syntax error")

        result = run_query_sync(ctx, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }

    def test_passes_readonly_true_under_read_only_mode(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=True)
        cluster.execute_query.return_value.get_all_rows.return_value = []

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            run_query_sync(ctx, "SELECT 1 AS one")

        args, _kwargs = cluster.execute_query.call_args
        assert args[0] == "SELECT 1 AS one"
        assert args[1]["readonly"] is True

    def test_does_not_pass_readonly_when_mode_off_and_no_token(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.get_all_rows.return_value = []

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            run_query_sync(ctx, "SELECT 1 AS one")

        cluster.execute_query.assert_called_once_with("SELECT 1 AS one")

    def test_passes_readonly_true_for_a_read_only_token(self) -> None:
        """A SCOPE_READ-only token must not mutate via SQL++, even with
        read_only_mode=False — same scope-gap closure as
        run_sql_plus_plus_query on the operational server."""
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.get_all_rows.return_value = []
        token = SimpleNamespace(scopes=[SCOPE_READ])

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=token,
        ):
            run_query_sync(ctx, "SELECT 1 AS one")

        args, _ = cluster.execute_query.call_args
        assert args[1]["readonly"] is True

    def test_does_not_pass_readonly_for_a_write_token(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.get_all_rows.return_value = []
        token = SimpleNamespace(scopes=[SCOPE_READ, SCOPE_WRITE])

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=token,
        ):
            run_query_sync(ctx, "SELECT 1 AS one")

        cluster.execute_query.assert_called_once_with("SELECT 1 AS one")


class TestExplainQuery:
    def test_prepends_explain_to_statement(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = [{"plan": {}}]

        result = explain_query(ctx, "SELECT 1 AS one")

        cluster.execute_query.assert_called_once_with("EXPLAIN SELECT 1 AS one")
        assert result == {"success": True, "plan": [{"plan": {}}]}

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.side_effect = Exception("syntax error")

        result = explain_query(ctx, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }


class TestIsCopyToStatement:
    def test_matches_export_to_external_storage(self) -> None:
        assert _is_copy_to_statement("COPY ds TO 's3://bucket/path' AT link_name")

    def test_matches_export_to_kv(self) -> None:
        assert _is_copy_to_statement("COPY ds TO `bucket`.`scope`.`coll`")

    def test_case_insensitive(self) -> None:
        assert _is_copy_to_statement("copy ds to 's3://bucket/path'")

    def test_tolerates_leading_whitespace(self) -> None:
        assert _is_copy_to_statement("  \n  COPY ds TO 's3://bucket/path'")

    def test_does_not_match_other_statements(self) -> None:
        assert not _is_copy_to_statement("SELECT 1 AS one")
        assert not _is_copy_to_statement("INSERT INTO ds VALUES ({'a': 1})")

    def test_does_not_match_an_identifier_that_merely_starts_with_copy(self) -> None:
        """ "COPY" must be its own leading token, not a prefix of a longer one."""
        assert not _is_copy_to_statement("COPYRIGHT_YEAR = 1")


class TestRunQuerySyncBlocksCopyToUnderReadOnly:
    def test_blocked_under_read_only_mode(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=True)

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            result = run_query_sync(ctx, "COPY ds TO 's3://bucket/path'")

        assert result["success"] is False
        assert "COPY" in result["error"]
        cluster.execute_query.assert_not_called()

    def test_blocked_for_a_read_only_token(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        token = SimpleNamespace(scopes=[SCOPE_READ])

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=token,
        ):
            result = run_query_sync(ctx, "COPY ds TO 's3://bucket/path'")

        assert result["success"] is False
        cluster.execute_query.assert_not_called()

    def test_allowed_in_write_mode(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.get_all_rows.return_value = []

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            result = run_query_sync(ctx, "COPY ds TO 's3://bucket/path'")

        assert result["success"] is True
        cluster.execute_query.assert_called_once_with("COPY ds TO 's3://bucket/path'")

    def test_allowed_for_a_write_token(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.get_all_rows.return_value = []
        token = SimpleNamespace(scopes=[SCOPE_READ, SCOPE_WRITE])

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=token,
        ):
            result = run_query_sync(ctx, "COPY ds TO 's3://bucket/path'")

        assert result["success"] is True
