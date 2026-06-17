"""Unit tests for query tool helpers and runtime guards.

Covers behaviors that the integration suite cannot reach because they
require a real Couchbase cluster failure or a deliberately-crafted SQL++
statement to trigger a read-only block:

- run_sql_plus_plus_query: read-only-mode write blocking (DML and DDL),
  EXPLAIN passthrough, and error propagation.
- explain_sql_plus_plus_query: empty-query validation and EXPLAIN prefixing.
- get_schema_for_collection / run_cluster_query: error propagation.
- _run_query_tool_with_empty_message: extra_payload merging on empty results.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cb_mcp.tools.query import (
    _run_query_tool_with_empty_message,
    explain_sql_plus_plus_query,
    get_schema_for_collection,
    run_cluster_query,
    run_sql_plus_plus_query,
)


def _make_ctx(*, read_only_mode: bool = True):
    """Build a fake Context wired with the read-only flag and a cluster stub.

    The cluster's `scope().query()` returns an iterable of rows so the tool
    body's `for row in result` loop works without a real SDK.
    """
    cluster = MagicMock()
    scope = MagicMock()
    cluster.bucket.return_value.scope.return_value = scope
    # Default: query returns no rows. Tests override scope.query as needed.
    scope.query.return_value = iter([])

    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster_provider=SimpleNamespace(
                    get_cluster=lambda c: cluster,
                ),
                read_only_mode=read_only_mode,
            )
        )
    )
    return ctx, cluster, scope


class TestRunSqlPlusPlusQueryReadOnly:
    """Read-only mode must block DML/DDL but allow EXPLAIN passthrough."""

    def test_data_modification_blocked_in_read_only_mode(self) -> None:
        """UPDATE in read-only mode must raise ValueError before hitting the cluster."""
        ctx, _, scope = _make_ctx(read_only_mode=True)

        with pytest.raises(ValueError, match="Data modification query is not allowed"):
            run_sql_plus_plus_query(
                ctx, "b", "s", "UPDATE users SET age = 25 WHERE id = 1"
            )

        # Query must not have been forwarded to the cluster.
        scope.query.assert_not_called()

    def test_structure_modification_blocked_in_read_only_mode(self) -> None:
        """CREATE INDEX in read-only mode must raise ValueError."""
        ctx, _, scope = _make_ctx(read_only_mode=True)

        with pytest.raises(
            ValueError, match="Structure modification query is not allowed"
        ):
            run_sql_plus_plus_query(ctx, "b", "s", "CREATE INDEX idx ON users(name)")

        scope.query.assert_not_called()

    def test_explain_bypasses_read_only_check(self) -> None:
        """EXPLAIN of a DML query must NOT be blocked — EXPLAIN is read-only."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([{"plan": "..."}])

        # Should not raise.
        result = run_sql_plus_plus_query(
            ctx, "b", "s", "EXPLAIN UPDATE users SET x = 1"
        )

        assert result == [{"plan": "..."}]
        scope.query.assert_called_once()

    def test_writes_allowed_when_read_only_mode_false(self) -> None:
        """With read-only mode off, DML must pass through."""
        ctx, _, scope = _make_ctx(read_only_mode=False)
        scope.query.return_value = iter([])

        result = run_sql_plus_plus_query(ctx, "b", "s", "UPDATE users SET age = 25")
        assert result == []
        scope.query.assert_called_once()

    def test_select_returns_rows(self) -> None:
        """A SELECT query should collect all yielded rows into a list."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([{"id": 1}, {"id": 2}])

        result = run_sql_plus_plus_query(ctx, "b", "s", "SELECT * FROM users")
        assert result == [{"id": 1}, {"id": 2}]

    def test_cluster_query_failure_propagates(self) -> None:
        """If the SDK raises during query execution, the error must propagate."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.side_effect = Exception("query timeout")

        with pytest.raises(Exception, match="query timeout"):
            run_sql_plus_plus_query(ctx, "b", "s", "SELECT 1")


class TestExplainSqlPlusPlusQuery:
    """explain_sql_plus_plus_query input validation and EXPLAIN prefixing."""

    def test_empty_query_raises_value_error(self) -> None:
        """Empty / whitespace-only queries must be rejected before any work."""
        ctx, _, _ = _make_ctx(read_only_mode=True)

        with pytest.raises(ValueError, match="Query cannot be empty"):
            explain_sql_plus_plus_query(ctx, "b", "s", "   \n  \t ")

    def test_prepends_explain_when_missing(self) -> None:
        """A plain SELECT must be wrapped in EXPLAIN before execution."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([{"plan": {"#operator": "Sequence"}}])

        result = explain_sql_plus_plus_query(ctx, "b", "s", "SELECT 1")

        assert result["explain_statement"] == "EXPLAIN SELECT 1"
        assert result["query"] == "SELECT 1"
        assert result["query_context"] == {"bucket_name": "b", "scope_name": "s"}

    def test_keeps_existing_explain_prefix(self) -> None:
        """If the caller already provided EXPLAIN, do not double-prefix."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([{"plan": {"#operator": "Sequence"}}])

        result = explain_sql_plus_plus_query(ctx, "b", "s", "EXPLAIN SELECT 1")
        assert result["explain_statement"] == "EXPLAIN SELECT 1"


class TestGetSchemaForCollection:
    """get_schema_for_collection error propagation."""

    def test_propagates_underlying_failure(self) -> None:
        """Failures from INFER should be re-raised — the schema tool must
        not swallow connectivity / parsing errors."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.side_effect = Exception("infer failed")

        with pytest.raises(Exception, match="infer failed"):
            get_schema_for_collection(ctx, "b", "s", "users")

    def test_empty_schema_when_no_results(self) -> None:
        """If INFER returns no rows, schema should be the empty default."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([])

        result = get_schema_for_collection(ctx, "b", "s", "users")
        assert result == {"collection_name": "users", "schema": []}


class TestRunClusterQuery:
    """run_cluster_query error propagation."""

    def test_failure_propagates(self) -> None:
        """Cluster-level query errors should not be hidden by the helper."""
        ctx, cluster, _ = _make_ctx(read_only_mode=True)
        cluster.query.side_effect = Exception("network error")

        with pytest.raises(Exception, match="network error"):
            run_cluster_query(ctx, "SELECT 1")


class TestRunQueryToolWithEmptyMessage:
    """Empty-result envelope used by every performance analysis tool."""

    def test_results_returned_when_present(self) -> None:
        """When the cluster returns rows, the helper returns them verbatim."""
        ctx, cluster, _ = _make_ctx(read_only_mode=True)
        cluster.query.return_value = iter([{"statement": "SELECT 1"}])

        result = _run_query_tool_with_empty_message(
            ctx, "SELECT * FROM x", limit=10, empty_message="nope"
        )

        assert result == [{"statement": "SELECT 1"}]

    def test_extra_payload_merged_on_empty(self) -> None:
        """When no rows, the empty envelope merges any extra_payload fields."""
        ctx, cluster, _ = _make_ctx(read_only_mode=True)
        cluster.query.return_value = iter([])

        result = _run_query_tool_with_empty_message(
            ctx,
            "SELECT * FROM x",
            limit=10,
            empty_message="No data",
            extra_payload={"hint": "try later"},
        )

        assert result == [{"message": "No data", "results": [], "hint": "try later"}]

    def test_empty_envelope_without_extra_payload(self) -> None:
        """Empty results without extras should yield just message + results."""
        ctx, cluster, _ = _make_ctx(read_only_mode=True)
        cluster.query.return_value = iter([])

        result = _run_query_tool_with_empty_message(
            ctx, "SELECT * FROM x", limit=10, empty_message="No data"
        )
        assert result == [{"message": "No data", "results": []}]
