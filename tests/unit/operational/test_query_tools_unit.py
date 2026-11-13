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

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from jsonschema import validate
from lark_sqlpp import modifies_data, modifies_structure, parse_sqlpp

from cb_mcp.tools.operational.query import (
    _blocked_write_kind,
    _run_query_tool_with_empty_message,
    explain_sql_plus_plus_query,
    get_longest_running_queries,
    get_most_frequent_queries,
    get_queries_not_selective,
    get_queries_with_large_result_count,
    get_queries_with_largest_response_sizes,
    get_schema_for_collection,
    run_cluster_query,
    run_sql_plus_plus_query,
)
from cb_mcp.utils.sqlpp import safe_ident


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


class TestRunSqlPlusPlusQueryRawResults:
    """SELECT RAW / VALUE returns bare JSON values, not objects (DA-2240)."""

    def test_scalar_rows_pass_through(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=False)
        scope.query.return_value = iter(["40-Mile Air", "Texas Wings", 7])

        result = run_sql_plus_plus_query(
            ctx, "b", "s", "SELECT RAW name FROM airline LIMIT 3"
        )

        assert result == ["40-Mile Air", "Texas Wings", 7]

    def test_output_schema_accepts_non_object_items(self) -> None:
        mcp = FastMCP("test")
        mcp.tool(run_sql_plus_plus_query)
        tool = asyncio.run(mcp.get_tool("run_sql_plus_plus_query"))

        # Must not raise: strings/numbers/arrays are valid result items.
        validate({"result": ["40-Mile Air", 3, [1, 2], {"a": 1}]}, tool.output_schema)


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

    def test_escapes_embedded_backtick_in_collection_name(self) -> None:
        """A collection_name containing a backtick must not be able to close
        the identifier early and inject a second SQL++ statement.

        read_only_mode=False here to isolate query construction from the
        separate, pre-existing lark_sqlpp INFER-parsing quirk covered by
        test_infer_with_doubled_backtick_is_unparseable_by_write_guard below.
        """
        ctx, _, scope = _make_ctx(read_only_mode=False)
        scope.query.return_value = iter([])
        malicious_name = "x`; DELETE FROM `users"

        get_schema_for_collection(ctx, "b", "s", malicious_name)

        query = scope.query.call_args[0][0]
        assert query == "INFER `x``; DELETE FROM ``users`"

    def test_infer_with_doubled_backtick_is_unparseable_by_write_guard(self) -> None:
        """Known lark_sqlpp limitation: its INFER grammar rule can't parse a
        doubled-backtick escaped identifier (unlike its SELECT/FROM rule, which
        can). Under the default read_only_mode=True, this means a
        collection_name containing a backtick fails loudly via the write-guard
        parse step rather than executing — fail-safe, not a security
        regression, since real Couchbase collection names can't contain
        backticks anyway."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([])
        malicious_name = "x`; DELETE FROM `users"

        with pytest.raises(Exception, match="terminal"):
            get_schema_for_collection(ctx, "b", "s", malicious_name)


class TestSafeIdent:
    """safe_ident: backtick-quoting for SQL++ identifiers."""

    def test_wraps_plain_identifier_in_backticks(self) -> None:
        assert safe_ident("users") == "`users`"

    def test_doubles_embedded_backticks(self) -> None:
        assert safe_ident("x`; DELETE FROM `users") == "`x``; DELETE FROM ``users`"


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


class TestGetQueriesNotSelective:
    """DA-2239: scans logged under primaryScan (primary/sequential) must count."""

    def test_query_counts_index_and_primary_scans(self, monkeypatch) -> None:
        """The SQL++ sums indexScan and primaryScan so WHERE-clause queries are not dropped."""
        captured: dict = {}

        def fake_run(ctx, query, **kwargs):
            captured["query"] = query
            return []

        monkeypatch.setattr(
            "cb_mcp.tools.operational.query._run_query_tool_with_empty_message",
            fake_run,
        )
        get_queries_not_selective(MagicMock(), limit=5)

        # Only the WHERE clause decides which queries are listed, so assert on it
        # alone: primaryScan also appears in the SELECT, which would mask a
        # WHERE that still filters on indexScan only.
        where = " ".join(
            captured["query"].split("WHERE")[1].split("GROUP BY")[0].split()
        )
        assert "phaseCounts.`indexScan`" in where
        assert "phaseCounts.`primaryScan`" in where
        assert "> resultCount" in where
        # Sub-keys (.GSI/.Seq) repeat the top-level count and must not be added.
        assert ".GSI" not in captured["query"]
        assert ".Seq" not in captured["query"]


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


class TestGetSchemaForCollectionNumSampleValues:
    """num_sample_values: WITH-clause construction and input validation.

    The parameter is annotated `int | None`, but annotations are not enforced
    at runtime and arguments arrive as decoded JSON over MCP — so a client can
    genuinely deliver a float, a bool, or a string here.
    """

    def test_omitted_by_default(self) -> None:
        """Unset must leave the query untouched so the server applies its own
        default of 5 — existing callers see no behavior change."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([])

        get_schema_for_collection(ctx, "b", "s", "users")

        assert scope.query.call_args[0][0] == "INFER `users`"

    def test_explicit_value_builds_with_clause(self) -> None:
        """A positive value must be rendered as a JSON object literal."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([])

        get_schema_for_collection(ctx, "b", "s", "users", num_sample_values=10)

        query = scope.query.call_args[0][0]
        assert query == 'INFER `users` WITH {"num_sample_values": 10}'

    def test_zero_is_allowed(self) -> None:
        """0 is a meaningful value to INFER — it drops the `samples` key from
        every field while keeping structure and type info — so it must not be
        treated as 'unset'."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([])

        get_schema_for_collection(ctx, "b", "s", "users", num_sample_values=0)

        query = scope.query.call_args[0][0]
        assert query == 'INFER `users` WITH {"num_sample_values": 0}'

    @pytest.mark.parametrize(
        "bad_value",
        [
            pytest.param(-3, id="negative"),
            # INFER silently truncates a fraction (2.7 -> 2), returning a
            # different count than asked for with no warning.
            pytest.param(2.7, id="fractional"),
            # bool subclasses int, so isinstance(True, int) is True and
            # True < 0 is False — without an explicit bool guard this would
            # pass both checks and interpolate as Python-cased "True",
            # which is not valid JSON.
            pytest.param(True, id="bool"),
            # A string must raise ValueError, not crash with a TypeError
            # from the `< 0` comparison.
            pytest.param("5", id="string"),
        ],
    )
    def test_invalid_values_rejected_before_query(self, bad_value) -> None:
        """Invalid values must raise before the cluster is touched, with a
        message naming the offending parameter — the upstream SQL++ parse
        error never mentions num_sample_values."""
        ctx, _, scope = _make_ctx(read_only_mode=True)

        with pytest.raises(ValueError, match="num_sample_values"):
            get_schema_for_collection(
                ctx, "b", "s", "users", num_sample_values=bad_value
            )

        scope.query.assert_not_called()


class TestQueryParameterForwarding:
    """Named values must reach the SDK unchanged, and only when the caller
    supplied them: the SDK does not accept a ``None`` binding.
    """

    def test_no_parameters_are_forwarded_when_none_given(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)

        run_sql_plus_plus_query(ctx, "b", "s", "SELECT 1")

        _, kwargs = scope.query.call_args
        assert "named_parameters" not in kwargs

    def test_named_parameters_are_forwarded(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)

        run_sql_plus_plus_query(
            ctx,
            "b",
            "s",
            "SELECT * FROM t WHERE country = $country",
            named_parameters={"country": "France"},
        )

        _, kwargs = scope.query.call_args
        assert kwargs["named_parameters"] == {"country": "France"}

    def test_explain_with_a_positional_placeholder_skips_the_parse(self) -> None:
        """EXPLAIN bypasses the write check, so a statement the SQL++ parser
        cannot parse still reaches the cluster in read-only mode."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([{"plan": {"#operator": "Sequence"}}])

        explain_sql_plus_plus_query(ctx, "b", "s", "SELECT * FROM t WHERE country = ?")

        args, kwargs = scope.query.call_args
        assert args[0] == "EXPLAIN SELECT * FROM t WHERE country = ?"
        assert "positional_parameters" not in kwargs

    def test_empty_mappings_are_still_forwarded(self) -> None:
        """An explicit empty binding is a caller decision, not an absence."""
        ctx, _, scope = _make_ctx(read_only_mode=True)

        run_sql_plus_plus_query(ctx, "b", "s", "SELECT 1", named_parameters={})

        _, kwargs = scope.query.call_args
        assert kwargs["named_parameters"] == {}


PRIMARY_SCAN_ROW = {
    "plan": {
        "#operator": "Sequence",
        "~children": [
            {
                "#operator": "PrimaryScan3",
                "index": "def_primary",
                "keyspace": "hotel",
            },
            {"#operator": "Fetch", "keyspace": "hotel"},
        ],
    }
}


class TestExplainSqlPlusPlusQueryParameters:
    """EXPLAIN plans with the caller's values, and says so when it cannot."""

    def test_named_parameters_reach_the_sdk(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        explain_sql_plus_plus_query(
            ctx,
            "b",
            "s",
            "SELECT * FROM hotel WHERE country = $country",
            named_parameters={"country": "France"},
        )

        args, kwargs = scope.query.call_args
        assert args[0] == "EXPLAIN SELECT * FROM hotel WHERE country = $country"
        assert kwargs["named_parameters"] == {"country": "France"}

    def test_non_parameterized_query_is_unchanged(self) -> None:
        """The pre-existing contract: no parameters, no warnings, no caveats."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        result = explain_sql_plus_plus_query(ctx, "b", "s", "SELECT * FROM hotel")

        _, kwargs = scope.query.call_args
        assert "named_parameters" not in kwargs
        assert result["warnings"] == []
        assert result["parameters"]["parameterized"] is False
        assert (
            result["plan_evaluation"]["summary"]
            == "Plan has optimization opportunities."
        )
        issues = {f["issue"] for f in result["plan_evaluation"]["findings"]}
        assert "primary_index_scan" in issues

    def test_unbound_named_parameter_warns_and_downgrades(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        result = explain_sql_plus_plus_query(
            ctx, "b", "s", "SELECT * FROM hotel WHERE country = $country"
        )

        (warning,) = result["warnings"]
        assert warning["issue"] == "unbound_parameters"
        assert warning["severity"] == "warning"
        assert "$country" in warning["message"]

        (finding,) = [
            f
            for f in result["plan_evaluation"]["findings"]
            if f["issue"] == "primary_index_scan"
        ]
        assert finding["confidence"] == "possible"
        assert "may not match runtime" in result["plan_evaluation"]["summary"]

    def test_unbound_positional_parameter_warns(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        result = explain_sql_plus_plus_query(
            ctx, "b", "s", "SELECT * FROM hotel WHERE country = ? AND city = ?"
        )

        (warning,) = result["warnings"]
        assert warning["issue"] == "unbound_parameters"
        assert "2 value(s)" in warning["message"]
        assert "cannot be bound by this tool" in warning["message"]

    def test_bound_parameters_produce_no_warning_and_definite_findings(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        result = explain_sql_plus_plus_query(
            ctx,
            "b",
            "s",
            "SELECT * FROM hotel WHERE country = $country",
            named_parameters={"country": "France"},
        )

        assert result["warnings"] == []
        assert result["parameters"]["fully_bound"] is True
        (finding,) = [
            f
            for f in result["plan_evaluation"]["findings"]
            if f["issue"] == "primary_index_scan"
        ]
        assert finding["confidence"] == "definite"

    def test_misspelled_parameter_name_is_flagged(self) -> None:
        """Both warnings fire: the typo'd value is unused and the real
        placeholder is unbound, which is what makes the plan wrong."""
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        result = explain_sql_plus_plus_query(
            ctx,
            "b",
            "s",
            "SELECT * FROM hotel WHERE country = $country",
            named_parameters={"contry": "France"},
        )

        issues = {w["issue"] for w in result["warnings"]}
        assert issues == {"unbound_parameters", "unused_parameters"}

    def test_dollar_inside_a_string_literal_is_not_a_placeholder(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        result = explain_sql_plus_plus_query(
            ctx, "b", "s", "SELECT * FROM hotel WHERE name = 'costs $5'"
        )

        assert result["warnings"] == []
        assert result["parameters"]["parameterized"] is False

    def test_explain_prefixed_query_still_detects_parameters(self) -> None:
        ctx, _, scope = _make_ctx(read_only_mode=True)
        scope.query.return_value = iter([PRIMARY_SCAN_ROW])

        result = explain_sql_plus_plus_query(
            ctx, "b", "s", "EXPLAIN SELECT * FROM hotel WHERE country = $country"
        )

        assert result["explain_statement"] == (
            "EXPLAIN SELECT * FROM hotel WHERE country = $country"
        )
        assert result["parameters"]["detected"]["named"] == ["country"]

    def test_parameters_are_exposed_as_an_mcp_tool_schema(self) -> None:
        """named_parameters must be callable over MCP, not just in Python."""
        mcp = FastMCP("test")
        mcp.tool(explain_sql_plus_plus_query)
        tool = asyncio.run(mcp.get_tool("explain_sql_plus_plus_query"))

        properties = tool.parameters["properties"]
        assert "named_parameters" in properties
        assert "positional_parameters" not in properties
        assert set(tool.parameters.get("required", [])) == {
            "bucket_name",
            "scope_name",
            "query",
        }


STATEMENT_AGGREGATING_TOOLS = [
    get_longest_running_queries,
    get_most_frequent_queries,
    get_queries_with_largest_response_sizes,
    get_queries_with_large_result_count,
    get_queries_not_selective,
]


def _capture_tool_query(tool, monkeypatch) -> str:
    """Run a completed_requests tool and return the SQL++ it would execute."""
    captured: dict = {}

    def fake_run(ctx, query, **kwargs):
        captured["query"] = query
        return []

    monkeypatch.setattr(
        "cb_mcp.tools.operational.query._run_query_tool_with_empty_message",
        fake_run,
    )
    tool(MagicMock(), limit=5)
    return captured["query"]


class TestNamedArgsSampling:
    """The statement-aggregating tools group by statement, which drops the
    per-execution parameter values. Each projects a namedArgs sample so a
    parameterized statement can be re-explained with real values.
    """

    @pytest.mark.parametrize(
        "tool", STATEMENT_AGGREGATING_TOOLS, ids=lambda t: t.__name__
    )
    def test_named_args_are_projected(self, tool, monkeypatch) -> None:
        query = _capture_tool_query(tool, monkeypatch)

        projection = " ".join(query.split("FROM system:completed_requests")[0].split())
        assert "MAX([" in projection
        assert "namedArgs])[1] AS namedArgs" in projection

    @pytest.mark.parametrize(
        "tool", STATEMENT_AGGREGATING_TOOLS, ids=lambda t: t.__name__
    )
    def test_positional_args_are_not_projected(self, tool, monkeypatch) -> None:
        """Positional values are out of scope: nothing can bind them back."""
        assert "positionalArgs" not in _capture_tool_query(tool, monkeypatch)

    @pytest.mark.parametrize(
        "tool", STATEMENT_AGGREGATING_TOOLS, ids=lambda t: t.__name__
    )
    def test_grouping_and_limit_are_unchanged(self, tool, monkeypatch) -> None:
        """The sample is an extra projection, not a change to what is ranked."""
        query = _capture_tool_query(tool, monkeypatch)

        assert "GROUP BY statement" in query
        assert "LIMIT $limit" in query
