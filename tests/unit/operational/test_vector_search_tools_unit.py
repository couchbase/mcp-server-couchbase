"""Unit tests for the vector search tools (run_vector_search, run_search_vector_search).

Covers:
- run_vector_search: SQL++ query construction (named-parameter vector,
  distance_metric/select_fields/where interpolation, Composite vs Hyperscale
  arity), embedding-provider resolution and failure handling, the
  num_probes/rerank/top_n_scan all-or-nothing guard, cluster-connection-
  failure propagation vs. everything-else returning tool_error.
- run_search_vector_search: VectorQuery/VectorSearch/SearchRequest
  construction, hybrid (scalar_query present) vs. vector-only branching,
  cluster-level vs. scoped search branching, the bucket/scope invalid-combo
  guard, and the same raise-vs-error-dict split as run_vector_search.

Follows tests/unit/operational/test_fts_tools_unit.py's pattern: a fake
Context via SimpleNamespace (tools only touch ctx through patched
accessors), MagicMock cluster/bucket/scope, and patches applied at the tool
module's own import path.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.operational.vector_search import (
    run_search_vector_search,
    run_vector_search,
)
from cb_mcp.utils.operational.embeddings import EmbeddingConfigError

_MODULE = "cb_mcp.tools.operational.vector_search"


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace()


@contextmanager
def _patch_embedding(
    vector=None, model="text-embedding-3-small", dimensions=3, side_effect=None
):
    """Patch get_settings + embed_query_text (registry.embed_query_text,
    re-exported into this tool module) so _embed_query returns a fixed
    (vector, info) without needing a real FastMCP request context on the
    fake ctx (_make_ctx() is a bare SimpleNamespace) or any provider config.
    get_settings must stay patched too: _embed_query's shim still evaluates
    get_settings(ctx) as an argument before the mocked embed_query_text ever
    runs, and the real implementation would crash on a bare SimpleNamespace."""
    with patch(f"{_MODULE}.get_settings", return_value={"embedding_model": model}):
        if side_effect is not None:
            with patch(f"{_MODULE}.embed_query_text", side_effect=side_effect) as mock:
                yield mock
        else:
            info = {"embedding_model": model, "embedding_dimensions": dimensions}
            with patch(
                f"{_MODULE}.embed_query_text",
                return_value=(vector or [0.1, 0.2, 0.3], info),
            ) as mock:
                yield mock


class TestRunVectorSearchValidation:
    """Guards that fire before any cluster/embedding call."""

    def test_partial_hyperscale_params_is_error(self) -> None:
        ctx = _make_ctx()
        result = run_vector_search(
            ctx,
            "b",
            "s",
            "c",
            "embedding",
            "query text",
            distance_metric="l2_squared",
            num_probes=10,
            # rerank/top_n_scan omitted -> invalid
        )
        assert result["success"] is False
        assert "num_probes" in result["error"]

    def test_connection_failure_propagates(self) -> None:
        """A cluster that can't be reached at all must still raise."""
        ctx = _make_ctx()

        with (
            patch(
                f"{_MODULE}.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            run_vector_search(
                ctx,
                "b",
                "s",
                "c",
                "embedding",
                "query text",
                distance_metric="l2_squared",
            )

    def test_bucket_connection_failure_propagates(self) -> None:
        """A bucket that can't be reached is the same class of problem as an
        unreachable cluster -- it must also raise, not become tool_error."""
        ctx = _make_ctx()
        cluster = MagicMock()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(
                f"{_MODULE}.connect_to_bucket", side_effect=Exception("no bucket 'b'")
            ),
            pytest.raises(Exception, match="no bucket 'b'"),
        ):
            run_vector_search(
                ctx,
                "b",
                "s",
                "c",
                "embedding",
                "query text",
                distance_metric="l2_squared",
            )


class TestRunVectorSearchQueryConstruction:
    """SQL++ built against a GSI vector index, embedding-provider driven."""

    def _cluster_with_scope_query(self, rows):
        cluster = MagicMock()
        bucket = MagicMock()
        cluster.bucket.return_value = bucket
        scope = MagicMock()
        bucket.scope.return_value = scope
        scope.query.return_value = rows
        cluster.cluster_info.return_value.nodes = [{"version": "8.0.0-1928-enterprise"}]
        return cluster, bucket, scope

    def test_composite_query_uses_named_parameter_and_default_projection(self) -> None:
        ctx = _make_ctx()
        rows = [{"id": "doc1", "distance": 0.1, "document": {"name": "widget"}}]
        cluster, bucket, scope = self._cluster_with_scope_query(rows)

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
            _patch_embedding(vector=[0.1, 0.2, 0.3]),
        ):
            result = run_vector_search(
                ctx,
                "b",
                "s",
                "c",
                "embedding",
                "find widgets",
                distance_metric="l2_squared",
                limit=5,
            )

        assert result["success"] is True
        assert result["total_hits"] == 1
        assert result["hits"] == rows
        assert result["cluster_major_version"] == 8
        assert "warning" not in result

        scope.query.assert_called_once()
        query_text, kwargs = scope.query.call_args
        query_text = query_text[0]
        assert "APPROX_VECTOR_DISTANCE" in query_text
        assert "doc.`embedding`" in query_text
        assert '"l2_squared"' in query_text
        # Nested under "document", never flattened with doc.* -- see
        # test_select_fields_projects_as_nested_object_not_flattened for why.
        assert "doc AS document" in query_text
        assert "doc.*" not in query_text
        assert "FROM `c` AS doc" in query_text
        assert "LIMIT 5" in query_text
        assert "WHERE" not in query_text
        assert kwargs["named_parameters"] == {"query_vector": [0.1, 0.2, 0.3]}

    def test_hyperscale_tuning_params_extend_function_arity(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, scope = self._cluster_with_scope_query([])

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
            _patch_embedding(),
        ):
            run_vector_search(
                ctx,
                "b",
                "s",
                "c",
                "embedding",
                "query",
                distance_metric="l2_squared",
                num_probes=10,
                rerank=1,
                top_n_scan=1000,
            )

        query_text = scope.query.call_args[0][0]
        assert (
            'APPROX_VECTOR_DISTANCE(doc.`embedding`, $query_vector, "l2_squared", 10, 1, 1000)'
            in query_text
        )

    def test_where_and_select_fields_applied(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, scope = self._cluster_with_scope_query([])

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
            _patch_embedding(),
        ):
            run_vector_search(
                ctx,
                "b",
                "s",
                "c",
                "embedding",
                "query",
                distance_metric="l2_squared",
                where="doc.status = 'active'",
                select_fields=["name", "status"],
            )

        query_text = scope.query.call_args[0][0]
        assert "WHERE doc.status = 'active'" in query_text
        # Nested object literal, not a flattened doc.`name`, doc.`status` --
        # select_fields never bypasses the "document" namespacing either.
        assert '{"name": doc.`name`, "status": doc.`status`} AS document' in query_text
        assert "doc.*" not in query_text

    def test_pre_8_cluster_gets_warning_not_a_block(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = self._cluster_with_scope_query([])
        cluster.cluster_info.return_value.nodes = [{"version": "7.6.0"}]

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
            _patch_embedding(),
        ):
            result = run_vector_search(
                ctx, "b", "s", "c", "embedding", "query", distance_metric="l2_squared"
            )

        assert result["success"] is True
        assert result["cluster_major_version"] == 7
        assert "warning" in result

    def test_version_detection_failure_does_not_fail_the_search(self) -> None:
        """Version detection is purely advisory -- if it errors (e.g.
        cluster_info() itself fails), the search must still succeed, with
        cluster_major_version=None and no warning key, not propagate."""
        ctx = _make_ctx()
        rows = [{"id": "doc1", "distance": 0.1}]
        cluster, bucket, _scope = self._cluster_with_scope_query(rows)
        cluster.cluster_info.side_effect = Exception("stats endpoint unreachable")

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
            _patch_embedding(),
        ):
            result = run_vector_search(
                ctx, "b", "s", "c", "embedding", "query", distance_metric="l2_squared"
            )

        assert result["success"] is True
        assert result["total_hits"] == 1
        assert result["cluster_major_version"] is None
        assert "warning" not in result

    def test_embedding_config_error_returns_tool_error_not_raise(self) -> None:
        ctx = _make_ctx()
        cluster = MagicMock()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            _patch_embedding(
                side_effect=EmbeddingConfigError(
                    "EMBEDDING_PROVIDER is not configured."
                )
            ),
        ):
            result = run_vector_search(
                ctx, "b", "s", "c", "embedding", "query", distance_metric="l2_squared"
            )

        assert result["success"] is False
        assert "EMBEDDING_PROVIDER" in result["error"]


class TestRunSearchVectorSearchValidation:
    def test_bucket_without_scope_is_error(self) -> None:
        ctx = _make_ctx()
        result = run_search_vector_search(
            ctx, "idx1", "embedding", "query text", bucket_name="b"
        )
        assert result["success"] is False
        assert "bucket_name" in result["error"]

    def test_empty_scalar_query_is_rejected_not_silently_vector_only(self) -> None:
        """{} is falsy but not None -- must be rejected explicitly rather
        than silently becoming a vector-only search that still claims
        is_hybrid=True (the truthy-vs-is-not-None mismatch this guards)."""
        ctx = _make_ctx()
        result = run_search_vector_search(
            ctx, "idx1", "embedding", "query text", scalar_query={}
        )
        assert result["success"] is False
        assert "scalar_query" in result["error"]

    def test_empty_prefilter_is_rejected(self) -> None:
        ctx = _make_ctx()
        result = run_search_vector_search(
            ctx, "idx1", "embedding", "query text", prefilter={}
        )
        assert result["success"] is False
        assert "prefilter" in result["error"]

    def test_connection_failure_propagates(self) -> None:
        ctx = _make_ctx()

        with (
            patch(
                f"{_MODULE}.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            run_search_vector_search(ctx, "idx1", "embedding", "query text")

    def test_bucket_connection_failure_propagates(self) -> None:
        """Same class of problem as an unreachable cluster -- must raise,
        not become tool_error. Exercises the scoped-index path, which is the
        only one that calls connect_to_bucket at all."""
        ctx = _make_ctx()
        cluster = MagicMock()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(
                f"{_MODULE}.connect_to_bucket", side_effect=Exception("no bucket 'b'")
            ),
            pytest.raises(Exception, match="no bucket 'b'"),
        ):
            run_search_vector_search(
                ctx, "idx1", "embedding", "query text", bucket_name="b", scope_name="s"
            )


class TestRunSearchVectorSearchConstruction:
    def _make_search_result(self, rows=None):
        result = MagicMock()
        result.rows.return_value = rows or []
        return result

    def _make_row(self, id_="doc1", score=1.0, fields=None):
        row = MagicMock()
        row.id = id_
        row.score = score
        row.fields = fields or {}
        return row

    def test_vector_only_cluster_level_search(self) -> None:
        ctx = _make_ctx()
        cluster = MagicMock()
        cluster.search.return_value = self._make_search_result(
            rows=[self._make_row(id_="doc1", score=1.2, fields={"name": "widget"})]
        )

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.VectorQuery") as mock_vq,
            patch(f"{_MODULE}.VectorSearch") as mock_vs,
            patch(f"{_MODULE}.SearchRequest") as mock_request,
            patch(f"{_MODULE}.SearchOptions") as mock_options,
            _patch_embedding(vector=[0.1, 0.2]),
        ):
            result = run_search_vector_search(ctx, "idx1", "embedding", "find widgets")

        mock_vq.assert_called_once_with(
            "embedding", [0.1, 0.2], num_candidates=10, prefilter=None
        )
        mock_vs.from_vector_query.assert_called_once_with(mock_vq.return_value)
        mock_request.create.assert_called_once_with(
            mock_vs.from_vector_query.return_value
        )
        mock_options.assert_called_once_with(limit=10, fields=None, raw=None)
        cluster.search.assert_called_once_with(
            "idx1", mock_request.create.return_value, mock_options.return_value
        )

        assert result["success"] is True
        assert result["is_hybrid"] is False
        assert result["is_prefiltered"] is False
        assert result["total_hits"] == 1
        assert result["hits"] == [
            {"id": "doc1", "score": 1.2, "fields": {"name": "widget"}}
        ]

    def test_prefilter_wired_into_vector_query(self) -> None:
        ctx = _make_ctx()
        cluster = MagicMock()
        cluster.search.return_value = self._make_search_result()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.VectorQuery") as mock_vq,
            patch(f"{_MODULE}.VectorSearch"),
            patch(f"{_MODULE}.SearchRequest"),
            patch(f"{_MODULE}.SearchOptions"),
            patch(f"{_MODULE}.RawQuery") as mock_raw_query,
            _patch_embedding(vector=[0.1, 0.2]),
        ):
            result = run_search_vector_search(
                ctx,
                "idx1",
                "embedding",
                "find widgets",
                prefilter={"match": "in-stock", "field": "status"},
            )

        mock_raw_query.assert_called_once_with({"match": "in-stock", "field": "status"})
        mock_vq.assert_called_once_with(
            "embedding",
            [0.1, 0.2],
            num_candidates=10,
            prefilter=mock_raw_query.return_value,
        )
        assert result["is_prefiltered"] is True
        assert result["is_hybrid"] is False

    def test_prefilter_and_scalar_query_can_combine(self) -> None:
        """prefilter (narrows candidates) and scalar_query (hybrid ranking)
        are independent knobs -- both can be set on the same call."""
        ctx = _make_ctx()
        cluster = MagicMock()
        cluster.search.return_value = self._make_search_result()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.VectorQuery") as mock_vq,
            patch(f"{_MODULE}.VectorSearch"),
            patch(f"{_MODULE}.SearchRequest"),
            patch(f"{_MODULE}.SearchOptions") as mock_options,
            patch(f"{_MODULE}.RawQuery") as mock_raw_query,
            _patch_embedding(),
        ):
            result = run_search_vector_search(
                ctx,
                "idx1",
                "embedding",
                "query text",
                scalar_query={"match": "jacket", "field": "description"},
                prefilter={"match": "in-stock", "field": "status"},
            )

        # Only prefilter goes through RawQuery -- scalar_query attaches via
        # SearchOptions(raw=...) instead, see test_hybrid_search_attaches_
        # scalar_query_via_raw_option.
        mock_raw_query.assert_called_once_with({"match": "in-stock", "field": "status"})
        assert mock_vq.call_args.kwargs["prefilter"] is not None
        mock_options.assert_called_once_with(
            limit=10,
            fields=None,
            raw={"query": {"match": "jacket", "field": "description"}},
        )
        assert result["is_hybrid"] is True
        assert result["is_prefiltered"] is True

    def test_hybrid_search_attaches_scalar_query_via_raw_option(self) -> None:
        """scalar_query reuses fts.py's SearchOptions(raw={"query": ...})
        convention -- it's a top-level request param, not a structural field
        of VectorQuery, so it doesn't need RawQuery the way prefilter does."""
        ctx = _make_ctx()
        cluster = MagicMock()
        cluster.search.return_value = self._make_search_result()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.VectorQuery"),
            patch(f"{_MODULE}.VectorSearch"),
            patch(f"{_MODULE}.SearchRequest"),
            patch(f"{_MODULE}.SearchOptions") as mock_options,
            patch(f"{_MODULE}.RawQuery") as mock_raw_query,
            _patch_embedding(),
        ):
            result = run_search_vector_search(
                ctx,
                "idx1",
                "embedding",
                "query text",
                scalar_query={"match": "jacket", "field": "description"},
            )

        mock_raw_query.assert_not_called()
        mock_options.assert_called_once_with(
            limit=10,
            fields=None,
            raw={"query": {"match": "jacket", "field": "description"}},
        )
        assert result["is_hybrid"] is True

    def test_scoped_index_uses_bucket_scope_search(self) -> None:
        ctx = _make_ctx()
        cluster = MagicMock()
        bucket = MagicMock()
        scope = MagicMock()
        bucket.scope.return_value = scope
        scope.search.return_value = self._make_search_result()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
            patch(f"{_MODULE}.VectorQuery"),
            patch(f"{_MODULE}.VectorSearch"),
            patch(f"{_MODULE}.SearchRequest") as mock_request,
            patch(f"{_MODULE}.SearchOptions") as mock_options,
            _patch_embedding(),
        ):
            run_search_vector_search(
                ctx, "idx1", "embedding", "query", bucket_name="b", scope_name="s"
            )

        scope.search.assert_called_once_with(
            "idx1", mock_request.create.return_value, mock_options.return_value
        )
        cluster.search.assert_not_called()

    def test_embedding_failure_returns_tool_error_not_raise(self) -> None:
        ctx = _make_ctx()
        cluster = MagicMock()

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            _patch_embedding(side_effect=RuntimeError("upstream 500")),
        ):
            result = run_search_vector_search(ctx, "idx1", "embedding", "query")

        assert result["success"] is False
        assert "upstream 500" in result["error"]
