"""Unit tests for index tool branches not reached by the live-cluster suite.

Covers:
- get_index_advisor_recommendations empty-result envelope.
- get_index_advisor_recommendations error propagation.
- list_indexes REST-API path with return_raw_index_stats=True.
- list_indexes top-level error propagation.
- create_index / build_index / drop_index happy paths and log-and-return error handling.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.index import (
    build_index,
    create_index,
    drop_index,
    get_index_advisor_recommendations,
    list_indexes,
)


def _make_ctx_with_index_manager() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    """Build a Context plus its underlying cluster + index manager mock.

    Returns (ctx, cluster, index_manager) so each test can program the
    manager's individual ops via ``index_manager.<op>.side_effect``.
    """
    cluster = MagicMock()
    bucket = MagicMock()
    collection = MagicMock()
    index_manager = MagicMock()
    collection.query_indexes.return_value = index_manager
    bucket.scope.return_value.collection.return_value = collection
    cluster.bucket.return_value = bucket

    ctx = SimpleNamespace()
    return ctx, cluster, index_manager


class TestGetIndexAdvisorRecommendations:
    """Branches of get_index_advisor_recommendations."""

    def test_empty_advisor_response(self) -> None:
        """An empty advisor result must return the documented empty envelope
        rather than raising or returning an unstructured payload."""
        mock_ctx = MagicMock()

        with patch(
            "cb_mcp.tools.index.run_sql_plus_plus_query",
            return_value=[],
        ):
            result = get_index_advisor_recommendations(
                mock_ctx, "b", "s", "SELECT * FROM x"
            )

        assert result == {
            "message": "No recommendations available",
            "current_used_indexes": [],
            "recommended_indexes": [],
            "recommended_covering_indexes": [],
        }

    def test_summary_reflects_recommendation_counts(self) -> None:
        """The summary block must report counts that match the data arrays."""
        mock_ctx = MagicMock()
        advisor_payload = [
            {
                "advisor_result": {
                    "current_used_indexes": [{"index": "CREATE INDEX a"}],
                    "recommended_indexes": [
                        {"index": "CREATE INDEX b"},
                        {"index": "CREATE INDEX c"},
                    ],
                    "recommended_covering_indexes": [{"index": "CREATE INDEX d"}],
                }
            }
        ]

        with patch(
            "cb_mcp.tools.index.run_sql_plus_plus_query",
            return_value=advisor_payload,
        ):
            result = get_index_advisor_recommendations(
                mock_ctx, "b", "s", "SELECT * FROM x"
            )

        assert result["summary"]["current_indexes_count"] == 1
        assert result["summary"]["recommended_indexes_count"] == 2
        assert result["summary"]["recommended_covering_indexes_count"] == 1
        assert result["summary"]["has_recommendations"] is True

    def test_no_recommendations_flag_when_empty(self) -> None:
        """has_recommendations is False when both recommendation arrays are empty."""
        mock_ctx = MagicMock()
        advisor_payload = [
            {
                "advisor_result": {
                    "current_used_indexes": [{"index": "CREATE INDEX a"}],
                    "recommended_indexes": [],
                    "recommended_covering_indexes": [],
                }
            }
        ]

        with patch(
            "cb_mcp.tools.index.run_sql_plus_plus_query",
            return_value=advisor_payload,
        ):
            result = get_index_advisor_recommendations(
                mock_ctx, "b", "s", "SELECT * FROM x"
            )

        assert result["summary"]["has_recommendations"] is False

    def test_error_propagates(self) -> None:
        """Underlying query failures must be re-raised so the caller can
        see the real Couchbase error rather than a fabricated empty result."""
        mock_ctx = MagicMock()

        with (
            patch(
                "cb_mcp.tools.index.run_sql_plus_plus_query",
                side_effect=Exception("syntax error in ADVISOR"),
            ),
            pytest.raises(Exception, match="syntax error in ADVISOR"),
        ):
            get_index_advisor_recommendations(mock_ctx, "b", "s", "SELECT * FROM x")

    def test_advised_query_bound_as_named_parameter_not_reserved_name(self) -> None:
        """Regression: the advisor must bind the advised query via a named
        parameter whose name is NOT ``query``.

        The Couchbase SDK spreads named-parameter keys as kwargs into
        ``N1QLQuery(statement, ...)`` whose first positional arg is ``query``,
        so a parameter named ``query`` raises "got multiple values for
        argument 'query'" against a live cluster. These mock-based tests can't
        see that crash, so we assert the contract directly: no reserved key,
        the placeholder in the SQL matches the bound key, and the user's query
        flows through as the value.
        """
        mock_ctx = MagicMock()
        user_query = "SELECT * FROM airline WHERE country = 'United States'"

        with patch(
            "cb_mcp.tools.index.run_sql_plus_plus_query",
            return_value=[],
        ) as mock_run:
            get_index_advisor_recommendations(mock_ctx, "b", "s", user_query)

        # The advisor SQL is passed positionally; named_parameters as a kwarg.
        args, kwargs = mock_run.call_args
        advisor_sql = args[3] if len(args) > 3 else kwargs["query"]
        named_parameters = kwargs["named_parameters"]

        # The reserved SDK name must never be used as a parameter key.
        assert "query" not in named_parameters, (
            "Named parameter 'query' collides with the SDK's N1QLQuery "
            "positional arg and crashes against a live cluster."
        )

        # Exactly one placeholder, referenced by the SQL, bound to the user query.
        assert len(named_parameters) == 1
        ((param_name, param_value),) = named_parameters.items()
        assert param_value == user_query
        assert f"${param_name}" in advisor_sql, (
            f"Placeholder ${param_name} not found in advisor SQL: {advisor_sql!r}"
        )


class TestListIndexesRestRawPath:
    """The REST-API + return_raw_index_stats=True branch."""

    def test_rest_path_returns_raw_rows_unprocessed(self) -> None:
        """On a pre-8 cluster, raw mode must short-circuit before the row
        processor runs — REST rows pass through verbatim."""
        mock_ctx = MagicMock()
        mock_cluster = MagicMock()
        info = MagicMock()
        info.nodes = [{"version": "7.6.11-enterprise"}]
        mock_cluster.cluster_info.return_value = info

        raw_rows = [
            {
                "defnId": 123,
                "indexName": "idx1",
                "definition": "CREATE INDEX idx1 ON b.s.c(x)",
                "status": "Ready",
                "bucket": "b",
                "scope": "s",
                "collection": "c",
                "lastScanTime": "NA",
            }
        ]

        with (
            patch(
                "cb_mcp.tools.index.get_settings",
                return_value={
                    "connection_string": "couchbase://localhost",
                    "username": "u",
                    "password": "p",
                },
            ),
            patch(
                "cb_mcp.tools.index.get_cluster_connection",
                return_value=mock_cluster,
            ),
            patch(
                "cb_mcp.tools.index.fetch_indexes_from_rest_api",
                return_value=raw_rows,
            ),
            patch(
                "cb_mcp.tools.index.process_index_data_from_rest_api"
            ) as mock_process,
        ):
            result = list_indexes(mock_ctx, return_raw_index_stats=True)

        # Raw mode must NOT invoke the processor — that's the whole point.
        mock_process.assert_not_called()
        assert result == raw_rows
        # Defensive: defnId is a raw-only key that should survive.
        assert result[0]["defnId"] == 123


class TestListIndexesErrorPropagation:
    """list_indexes wraps everything in a try/except — verify the re-raise."""

    def test_resolve_version_failure_propagates(self) -> None:
        """If cluster version detection fails, the error must surface so the
        caller can diagnose connectivity rather than seeing an empty list."""
        mock_ctx = MagicMock()

        with (
            patch(
                "cb_mcp.tools.index.get_settings",
                return_value={
                    "connection_string": "couchbase://localhost",
                    "username": "u",
                    "password": "p",
                },
            ),
            patch(
                "cb_mcp.tools.index.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            list_indexes(mock_ctx)


class TestCreateIndex:
    """create_index happy path and log-and-return error handling."""

    def test_creates_deferred_by_default(self) -> None:
        """deferred defaults to True and is threaded through to the SDK options."""
        ctx, cluster, index_manager = _make_ctx_with_index_manager()

        with (
            patch("cb_mcp.tools.index.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.index.CreateQueryIndexOptions") as mock_options,
        ):
            result = create_index(ctx, "b", "s", "c", "idx1", ["email"])

        assert result["success"] is True
        assert result["index_name"] == "idx1"
        assert result["deferred"] is True
        assert result["keyspace"] == "b.s.c"
        # Deferred creates carry a next-step hint pointing at build_index.
        assert "build_index" in result["next_step"]
        args, _kwargs = index_manager.create_index.call_args
        assert args[0] == "idx1"
        assert args[1] == ["email"]
        # Assert on the kwargs forwarded to the options constructor rather than
        # subscripting the constructed instance (which couples to the SDK's
        # dict-subclass representation).
        mock_options.assert_called_once_with(
            deferred=True, condition=None, num_replicas=None, ignore_if_exists=False
        )

    def test_options_forwarded(self) -> None:
        """condition, num_replicas, and ignore_if_exists all reach the SDK options."""
        ctx, cluster, _index_manager = _make_ctx_with_index_manager()

        with (
            patch("cb_mcp.tools.index.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.index.CreateQueryIndexOptions") as mock_options,
        ):
            create_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                ["type"],
                deferred=False,
                condition="type = 'user'",
                num_replicas=1,
                ignore_if_exists=True,
            )

        mock_options.assert_called_once_with(
            deferred=False,
            condition="type = 'user'",
            num_replicas=1,
            ignore_if_exists=True,
        )

    def test_sdk_error_returns_error_dict_not_raised(self) -> None:
        """An existing-index error must be caught, logged, and returned — not raised."""
        ctx, cluster, index_manager = _make_ctx_with_index_manager()
        index_manager.create_index.side_effect = Exception("index already exists")

        with patch("cb_mcp.tools.index.get_cluster_connection", return_value=cluster):
            result = create_index(ctx, "b", "s", "c", "idx1", ["email"])

        assert result == {
            "success": False,
            "error": "index already exists",
            "index_name": "idx1",
            "keyspace": "b.s.c",
        }


class TestBuildIndex:
    """build_index happy path and log-and-return error handling.

    build_index does not pre-check for deferred indexes — it just calls
    build_deferred_indexes() and lets the SDK decide whether there's
    anything to do (a harmless no-op when there's nothing deferred).
    """

    def test_triggers_build(self) -> None:
        """Happy path calls build_deferred_indexes exactly once."""
        ctx, cluster, index_manager = _make_ctx_with_index_manager()

        with patch("cb_mcp.tools.index.get_cluster_connection", return_value=cluster):
            result = build_index(ctx, "b", "s", "c")

        assert result == {"success": True, "keyspace": "b.s.c"}
        index_manager.build_deferred_indexes.assert_called_once()
        index_manager.get_all_indexes.assert_not_called()

    def test_sdk_error_returns_error_dict_not_raised(self) -> None:
        """An SDK failure building indexes must be caught and returned."""
        ctx, cluster, index_manager = _make_ctx_with_index_manager()
        index_manager.build_deferred_indexes.side_effect = Exception("connection reset")

        with patch("cb_mcp.tools.index.get_cluster_connection", return_value=cluster):
            result = build_index(ctx, "b", "s", "c")

        assert result == {
            "success": False,
            "error": "connection reset",
            "keyspace": "b.s.c",
        }


class TestDropIndex:
    """drop_index happy path and log-and-return error handling."""

    def test_drops_index(self) -> None:
        """Happy path forwards index_name and ignore_if_not_exists to the SDK."""
        ctx, cluster, index_manager = _make_ctx_with_index_manager()

        with (
            patch("cb_mcp.tools.index.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.index.DropQueryIndexOptions") as mock_options,
        ):
            result = drop_index(ctx, "b", "s", "c", "idx1", ignore_if_not_exists=True)

        assert result == {"success": True, "index_name": "idx1", "keyspace": "b.s.c"}
        args, _kwargs = index_manager.drop_index.call_args
        assert args[0] == "idx1"
        mock_options.assert_called_once_with(ignore_if_not_exists=True)

    def test_sdk_error_returns_error_dict_not_raised(self) -> None:
        """A not-found error (without ignore_if_not_exists) must be caught and returned."""
        ctx, cluster, index_manager = _make_ctx_with_index_manager()
        index_manager.drop_index.side_effect = Exception("index not found")

        with patch("cb_mcp.tools.index.get_cluster_connection", return_value=cluster):
            result = drop_index(ctx, "b", "s", "c", "idx1")

        assert result == {
            "success": False,
            "error": "index not found",
            "index_name": "idx1",
            "keyspace": "b.s.c",
        }
