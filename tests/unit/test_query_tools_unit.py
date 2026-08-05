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
from lark_sqlpp import modifies_data, modifies_structure, parse_sqlpp

from cb_mcp.tools.query import (
    _blocked_write_kind,
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

    def test_grant_blocked_in_read_only_mode(self) -> None:
        """GRANT (SQL++ DCL) in read-only mode must raise before hitting the cluster.

        Regression guard for the read-only bypass reported against DCL: lark-sqlpp
        classifies GRANT/REVOKE as neither data nor structure modification, so the
        old ``modifies_data``/``modifies_structure`` gate let them through. The
        deny-by-default guard must now block them.
        """
        ctx, _, scope = _make_ctx(read_only_mode=True)

        with pytest.raises(
            ValueError, match="Privilege modification query is not allowed"
        ):
            run_sql_plus_plus_query(
                ctx, "b", "s", "GRANT cluster_admin ON default TO attacker_user"
            )

        scope.query.assert_not_called()

    def test_revoke_blocked_in_read_only_mode(self) -> None:
        """REVOKE (SQL++ DCL) in read-only mode must also be blocked."""
        ctx, _, scope = _make_ctx(read_only_mode=True)

        with pytest.raises(
            ValueError, match="Privilege modification query is not allowed"
        ):
            run_sql_plus_plus_query(
                ctx, "b", "s", "REVOKE query_select ON `travel-sample` FROM alice"
            )

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


# Collection-expression query forms — ANY / SOME / EVERY / WITHIN / EXISTS used
# as bare paths or expressions rather than subqueries. These are all valid
# read-only SQL++ SELECTs, so each one must parse, classify as non-modifying,
# and pass the read-only write guard through to the cluster.
COLLECTION_EXPR_QUERIES = [
    pytest.param(
        "SELECT h.name, h.city, h.country "
        "FROM `travel-sample`.`inventory`.`hotel` h "
        "WHERE ANY v WITHIN h.reviews SATISFIES v = 5 END LIMIT 10;",
        id="any-within-satisfies",
    ),
    pytest.param(
        "SELECT h.name FROM hotel AS h "
        "WHERE EVERY v IN h.reviews SATISFIES v.rating >= 3 END LIMIT 10;",
        id="every-in-satisfies",
    ),
    pytest.param(
        "SELECT any v in [1,2,3] satisfies v > 1 end as result",
        id="any-in-projection",
    ),
    pytest.param(
        "SELECT some v in [1,2,3] satisfies v > 1 end as result",
        id="some-in-projection",
    ),
    pytest.param(
        'SELECT * FROM hotel AS t WHERE "Walton Wolf" WITHIN t;',
        id="within-postfix",
    ),
    pytest.param(
        "SELECT DISTINCT h.city FROM hotel AS h WHERE EXISTS h.reviews;",
        id="exists-bare-path",
    ),
]


class TestCollectionExpressionParsing:
    """The parser must accept ANY/SOME/EVERY/WITHIN/EXISTS collection
    expressions in their bare (non-subquery) forms and classify them as
    read-only.
    """

    @pytest.mark.parametrize("query", COLLECTION_EXPR_QUERIES)
    def test_parses_without_error(self, query: str) -> None:
        """The grammar must accept the statement (no parse exception)."""
        parse_sqlpp(query)

    @pytest.mark.parametrize("query", COLLECTION_EXPR_QUERIES)
    def test_classified_as_read_only(self, query: str) -> None:
        """These SELECTs modify neither data nor structure."""
        tree = parse_sqlpp(query)
        assert modifies_data(tree) is False
        assert modifies_structure(tree) is False


class TestCollectionExpressionReadOnlyGuard:
    """The read-only write guard must let these SELECTs through: the guard
    parses each statement before running it, so a form it cannot parse would
    block a legitimate read-only query from reaching the cluster.
    """

    @pytest.mark.parametrize("query", COLLECTION_EXPR_QUERIES)
    def test_not_blocked_in_read_only_mode(self, query: str) -> None:
        """In read-only mode the query must reach the cluster and return rows."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([{"ok": 1}])

        result = run_sql_plus_plus_query(ctx, "b", "s", query)

        assert result == [{"ok": 1}]
        scope.query.assert_called_once()


class TestBlockedWriteKind:
    """Deny-by-default classification behind the read-only write guard.

    ``_blocked_write_kind`` returns None only for genuine reads (DQL/utility)
    and a label for everything else, so the guard blocks by allow-list rather
    than by enumerating forbidden statement classes.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM users LIMIT 1",
            "INFER `users`",
            "ADVISE SELECT * FROM users",
        ],
    )
    def test_read_only_statements_return_none(self, query: str) -> None:
        assert _blocked_write_kind(parse_sqlpp(query)) is None

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("UPDATE users SET age = 25", "data"),
            ("DELETE FROM users WHERE id = 1", "data"),
            ("CREATE INDEX idx ON users(name)", "structure"),
            ("DROP INDEX users.idx", "structure"),
            ("GRANT cluster_admin ON default TO attacker_user", "privilege"),
            ("REVOKE query_select ON `travel-sample` FROM alice", "privilege"),
        ],
    )
    def test_write_statements_are_labeled(self, query: str, expected: str) -> None:
        assert _blocked_write_kind(parse_sqlpp(query)) == expected

    @pytest.mark.parametrize(
        "query",
        [
            "GRANT query_select ON `travel-sample` TO alice",
            "REVOKE query_select ON `travel-sample` FROM alice",
        ],
    )
    def test_dcl_slips_past_lark_but_guard_catches_it(self, query: str) -> None:
        """The upstream lark-sqlpp gap the advisory reported: modifies_data and
        modifies_structure both return False for GRANT/REVOKE. Our top-level-rule
        classification must still flag them so the guard does not fall through.
        """
        tree = parse_sqlpp(query)
        # Document the upstream gap (root cause lives in lark-sqlpp).
        assert modifies_data(tree) is False
        assert modifies_structure(tree) is False
        # Our guard closes it regardless.
        assert _blocked_write_kind(tree) == "privilege"


def test_array_slice_projection_with_is_not_missing_parses() -> None:
    """Array-slice projection (h.public_likes[0:2]) combined with an
    IS NOT MISSING predicate must parse and classify as a read-only SELECT.
    """
    query = (
        "SELECT h.name, h.public_likes[0:2] AS top_likes "
        "FROM hotel AS h "
        "WHERE h.public_likes IS NOT MISSING "
        "LIMIT 5;"
    )
    tree = parse_sqlpp(query)
    assert modifies_data(tree) is False
    assert modifies_structure(tree) is False
