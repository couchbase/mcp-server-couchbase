"""Unit tests for FTS/Search tools (list/get/run/explain).

Covers:
- list_search_indexes: cluster-level (legacy), bucket-only (enumerate scopes),
  bucket+scope, invalid filter combo, error propagation.
- get_search_index_definition: cluster-level and scope-level happy paths,
  invalid bucket/scope pairing, error propagation.
- run_fts_query: RawQuery/SearchOptions construction, cluster-level vs
  scope-level branching, result formatting from a mocked SearchResult.
- explain_fts_query: forced explain=True and default limit=1, explanation
  extraction.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.search import (
    explain_fts_query,
    get_search_index_definition,
    list_search_indexes,
    run_fts_query,
)


def _make_ctx_with_search_managers() -> tuple[
    SimpleNamespace, MagicMock, MagicMock, MagicMock
]:
    """Build a Context plus cluster/bucket mocks wired for FTS index tools.

    Returns (ctx, cluster, cluster_index_manager, bucket) so tests can
    program cluster.search_indexes() and bucket.scope(name).search_indexes()
    independently, and bucket.collections().get_all_scopes() for enumeration.
    """
    cluster = MagicMock()
    cluster_index_manager = MagicMock()
    cluster.search_indexes.return_value = cluster_index_manager

    bucket = MagicMock()
    cluster.bucket.return_value = bucket

    ctx = SimpleNamespace()
    return ctx, cluster, cluster_index_manager, bucket


def _make_index(
    name="idx1",
    uuid="u1",
    source_name="b",
    source_type="couchbase",
    idx_type="fulltext-index",
    params=None,
    source_uuid="su1",
    source_params=None,
    plan_params=None,
) -> MagicMock:
    idx = MagicMock()
    idx.name = name
    idx.uuid = uuid
    idx.source_name = source_name
    idx.source_type = source_type
    idx.idx_type = idx_type
    idx.params = params or {}
    idx.source_uuid = source_uuid
    idx.source_params = source_params or {}
    idx.plan_params = plan_params or {}
    return idx


class TestListSearchIndexes:
    """Filter branches of list_search_indexes."""

    def test_no_filters_lists_cluster_level_indexes(self) -> None:
        ctx, cluster, cluster_index_manager, bucket = _make_ctx_with_search_managers()
        cluster_index_manager.get_all_indexes.return_value = [
            _make_index(name="legacy1")
        ]

        with patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster):
            result = list_search_indexes(ctx)

        assert result == [
            {
                "name": "legacy1",
                "uuid": "u1",
                "source_name": "b",
                "source_type": "couchbase",
                "idx_type": "fulltext-index",
                "bucket": None,
                "scope": None,
            }
        ]
        bucket.collections.assert_not_called()

    def test_bucket_only_enumerates_all_scopes(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_search_managers()
        scope_a = SimpleNamespace(name="scopeA")
        scope_b = SimpleNamespace(name="scopeB")
        bucket.collections.return_value.get_all_scopes.return_value = [scope_a, scope_b]

        scope_managers = {}

        def _scope_side_effect(name):
            mgr = MagicMock()
            mgr.search_indexes.return_value.get_all_indexes.return_value = [
                _make_index(name=f"{name}-idx")
            ]
            scope_managers[name] = mgr
            return mgr

        bucket.scope.side_effect = _scope_side_effect

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.connect_to_bucket", return_value=bucket),
        ):
            result = list_search_indexes(ctx, bucket_name="b")

        names = {(r["name"], r["bucket"], r["scope"]) for r in result}
        assert names == {
            ("scopeA-idx", "b", "scopeA"),
            ("scopeB-idx", "b", "scopeB"),
        }
        assert bucket.scope.call_count == 2

    def test_bucket_and_scope_targets_single_scope(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_search_managers()
        scope_mgr = MagicMock()
        scope_mgr.search_indexes.return_value.get_all_indexes.return_value = [
            _make_index(name="scoped1")
        ]
        bucket.scope.return_value = scope_mgr

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.connect_to_bucket", return_value=bucket),
        ):
            result = list_search_indexes(ctx, bucket_name="b", scope_name="s")

        assert result == [
            {
                "name": "scoped1",
                "uuid": "u1",
                "source_name": "b",
                "source_type": "couchbase",
                "idx_type": "fulltext-index",
                "bucket": "b",
                "scope": "s",
            }
        ]
        bucket.scope.assert_called_once_with("s")
        bucket.collections.assert_not_called()

    def test_scope_without_bucket_returns_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = (
            _make_ctx_with_search_managers()
        )

        result = list_search_indexes(ctx, scope_name="s")

        assert result == [
            {"error": "bucket_name is required when filtering by scope_name"}
        ]

    def test_sdk_error_returns_error_entry_not_raised(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        cluster_index_manager.get_all_indexes.side_effect = Exception(
            "search unavailable"
        )

        with patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster):
            result = list_search_indexes(ctx)

        assert result == [{"error": "search unavailable"}]

    def test_connection_failure_propagates(self) -> None:
        """The one case that must still raise: the cluster is unreachable."""
        ctx, _cluster, _cluster_index_manager, _bucket = (
            _make_ctx_with_search_managers()
        )

        with (
            patch(
                "cb_mcp.tools.search.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            list_search_indexes(ctx)


class TestGetSearchIndexDefinition:
    """Cluster-level vs scope-level lookup and pairing validation."""

    def test_cluster_level_lookup(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        cluster_index_manager.get_index.return_value = _make_index(
            name="idx1", params={"mapping": {}}
        )

        with patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster):
            result = get_search_index_definition(ctx, "idx1")

        cluster_index_manager.get_index.assert_called_once_with("idx1")
        assert result["name"] == "idx1"
        assert result["params"] == {"mapping": {}}
        assert result["bucket"] is None
        assert result["scope"] is None

    def test_scope_level_lookup(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_search_managers()
        scope_mgr = MagicMock()
        scope_mgr.search_indexes.return_value.get_index.return_value = _make_index(
            name="idx1"
        )
        bucket.scope.return_value = scope_mgr

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.connect_to_bucket", return_value=bucket),
        ):
            result = get_search_index_definition(
                ctx, "idx1", bucket_name="b", scope_name="s"
            )

        bucket.scope.assert_called_once_with("s")
        scope_mgr.search_indexes.return_value.get_index.assert_called_once_with("idx1")
        assert result["bucket"] == "b"
        assert result["scope"] == "s"

    def test_partial_pair_returns_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = (
            _make_ctx_with_search_managers()
        )

        result_bucket_only = get_search_index_definition(ctx, "idx1", bucket_name="b")
        result_scope_only = get_search_index_definition(ctx, "idx1", scope_name="s")

        assert "must be provided together" in result_bucket_only["error"]
        assert "must be provided together" in result_scope_only["error"]

    def test_sdk_error_returns_error_dict_not_raised(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        cluster_index_manager.get_index.side_effect = Exception("index not found")

        with patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster):
            result = get_search_index_definition(ctx, "idx1")

        assert result == {"error": "index not found", "index_name": "idx1"}

    def test_connection_failure_propagates(self) -> None:
        """The one case that must still raise: the cluster is unreachable."""
        ctx, _cluster, _cluster_index_manager, _bucket = (
            _make_ctx_with_search_managers()
        )

        with (
            patch(
                "cb_mcp.tools.search.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            get_search_index_definition(ctx, "idx1")


def _make_search_result(rows=None, errors=None, metrics=None, facets=None):
    result = MagicMock()
    result.rows.return_value = rows or []
    metadata = MagicMock()
    metadata.errors.return_value = errors or {}
    metadata.metrics.return_value = metrics
    result.metadata.return_value = metadata
    result.facets.return_value = facets or {}
    return result


def _make_row(
    id_="doc1", score=1.0, fields=None, locations=None, fragments=None, explanation=None
):
    row = MagicMock()
    row.id = id_
    row.score = score
    row.fields = fields or {}
    row.locations = locations or {}
    row.fragments = fragments or {}
    row.explanation = explanation or {}
    return row


class TestRunFtsQuery:
    """RawQuery/SearchOptions construction and result formatting."""

    def test_cluster_level_query_builds_options_and_formats_hits(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        search_result = _make_search_result(
            rows=[_make_row(id_="doc1", score=1.5, fields={"type": "beer"})],
            metrics=None,
        )
        cluster.search.return_value = search_result

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.SearchOptions") as mock_options,
            patch("cb_mcp.tools.search.RawQuery") as mock_raw_query,
            patch("cb_mcp.tools.search.SearchRequest") as mock_search_request,
        ):
            result = run_fts_query(
                ctx,
                "idx1",
                {"match": "ale"},
                limit=10,
                skip=0,
                fields=["type"],
                sort=["-_score"],
                facets={"types": {}},
                highlight_fields=["type"],
                disable_scoring=True,
                raw={"foo": "bar"},
            )

        mock_options.assert_called_once_with(
            limit=10,
            skip=0,
            fields=["type"],
            sort=["-_score"],
            facets={"types": {}},
            highlight_fields=["type"],
            disable_scoring=True,
            raw={"foo": "bar"},
        )
        mock_raw_query.assert_called_once_with({"match": "ale"})
        mock_search_request.create.assert_called_once_with(mock_raw_query.return_value)
        cluster.search.assert_called_once_with(
            "idx1", mock_search_request.create.return_value, mock_options.return_value
        )
        assert result["index_name"] == "idx1"
        assert result["total_hits"] == 1
        assert result["hits"][0]["id"] == "doc1"
        assert result["hits"][0]["score"] == 1.5

    def test_scoped_query_uses_bucket_scope_search(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_search_managers()
        scope_obj = MagicMock()
        scope_obj.search.return_value = _make_search_result()
        bucket.scope.return_value = scope_obj

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.connect_to_bucket", return_value=bucket),
            patch("cb_mcp.tools.search.SearchOptions"),
            patch("cb_mcp.tools.search.RawQuery"),
            patch("cb_mcp.tools.search.SearchRequest"),
        ):
            run_fts_query(
                ctx, "idx1", {"match": "ale"}, bucket_name="b", scope_name="s"
            )

        bucket.scope.assert_called_once_with("s")
        scope_obj.search.assert_called_once()
        cluster.search.assert_not_called()

    def test_partial_pair_returns_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = (
            _make_ctx_with_search_managers()
        )

        result = run_fts_query(ctx, "idx1", {"match": "ale"}, bucket_name="b")

        assert "must be provided together" in result["error"]

    def test_sdk_error_returns_error_dict_not_raised(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        cluster.search.side_effect = Exception("query failed")

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.SearchOptions"),
            patch("cb_mcp.tools.search.RawQuery"),
            patch("cb_mcp.tools.search.SearchRequest"),
        ):
            result = run_fts_query(ctx, "idx1", {"match": "ale"})

        assert result == {"error": "query failed", "index_name": "idx1"}

    def test_connection_failure_propagates(self) -> None:
        """The one case that must still raise: the cluster is unreachable."""
        ctx, _cluster, _cluster_index_manager, _bucket = (
            _make_ctx_with_search_managers()
        )

        with (
            patch(
                "cb_mcp.tools.search.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            run_fts_query(ctx, "idx1", {"match": "ale"})


class TestExplainFtsQuery:
    """explain=True and default limit=1 are always forced."""

    def test_forces_explain_and_default_limit(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        cluster.search.return_value = _make_search_result(
            rows=[_make_row(id_="doc1", score=2.0, explanation={"plan": "x"})]
        )

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.SearchOptions") as mock_options,
            patch("cb_mcp.tools.search.RawQuery"),
            patch("cb_mcp.tools.search.SearchRequest"),
        ):
            result = explain_fts_query(ctx, "idx1", {"match": "ale"})

        mock_options.assert_called_once_with(explain=True, limit=1)
        assert result["query_explained"] is True
        assert result["limit"] == 1
        assert result["explanations"] == [
            {"id": "doc1", "score": 2.0, "explanation": {"plan": "x"}}
        ]

    def test_custom_limit_forwarded(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        cluster.search.return_value = _make_search_result()

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.SearchOptions") as mock_options,
            patch("cb_mcp.tools.search.RawQuery"),
            patch("cb_mcp.tools.search.SearchRequest"),
        ):
            explain_fts_query(ctx, "idx1", {"match": "ale"}, limit=5)

        mock_options.assert_called_once_with(explain=True, limit=5)

    def test_sdk_error_returns_error_dict_not_raised(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_search_managers()
        cluster.search.side_effect = Exception("explain failed")

        with (
            patch("cb_mcp.tools.search.get_cluster_connection", return_value=cluster),
            patch("cb_mcp.tools.search.SearchOptions"),
            patch("cb_mcp.tools.search.RawQuery"),
            patch("cb_mcp.tools.search.SearchRequest"),
        ):
            result = explain_fts_query(ctx, "idx1", {"match": "ale"})

        assert result == {"error": "explain failed", "index_name": "idx1"}

    def test_connection_failure_propagates(self) -> None:
        """The one case that must still raise: the cluster is unreachable."""
        ctx, _cluster, _cluster_index_manager, _bucket = (
            _make_ctx_with_search_managers()
        )

        with (
            patch(
                "cb_mcp.tools.search.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            explain_fts_query(ctx, "idx1", {"match": "ale"})
