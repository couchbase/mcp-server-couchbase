"""Unit tests for create_query_index (scalar / Hyperscale vector / Composite vector).

Covers: the index_type validation matrix (mutually-exclusive/required-together
params per shape), the exact generated SQL++ DDL for each shape, the
required-dimension/required-similarity/DOT_PRODUCT-rejection checks,
with_options merge and reserved-key collision, the list(result)
execution-forcing behavior, and the raise-vs-tool_error split (connection
problems propagate uncaught, everything else is a tool_error).

Follows tests/unit/operational/test_index_tools_unit.py and
tests/unit/operational/test_vector_search_tools_unit.py's pattern: a fake
Context via SimpleNamespace, MagicMock cluster/bucket/scope, and patches
applied at the tool module's own import path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.operational.vector_index import create_query_index

_MODULE = "cb_mcp.tools.operational.vector_index"


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace()


def _cluster_with_scope_query(rows=None):
    cluster = MagicMock()
    bucket = MagicMock()
    cluster.bucket.return_value = bucket
    scope = MagicMock()
    bucket.scope.return_value = scope
    scope.query.return_value = rows if rows is not None else []
    return cluster, bucket, scope


class TestCreateQueryIndexValidation:
    """Guards that fire before any statement is built or executed."""

    def test_connection_failure_propagates(self) -> None:
        ctx = _make_ctx()
        with (
            patch(
                f"{_MODULE}.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            create_query_index(ctx, "b", "s", "c", "idx1", "scalar", keys=["email"])

    def test_bucket_connection_failure_propagates(self) -> None:
        ctx = _make_ctx()
        cluster = MagicMock()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(
                f"{_MODULE}.connect_to_bucket", side_effect=Exception("no bucket 'b'")
            ),
            pytest.raises(Exception, match="no bucket 'b'"),
        ):
            create_query_index(ctx, "b", "s", "c", "idx1", "scalar", keys=["email"])

    def test_invalid_index_type_is_error(self) -> None:
        ctx = _make_ctx()
        cluster, _bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=cluster.bucket()),
        ):
            result = create_query_index(ctx, "b", "s", "c", "idx1", "bogus")
        assert result["success"] is False
        assert "index_type" in result["error"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("vector_field", "embedding"),
            ("dimension", 768),
            ("similarity", "COSINE"),
            ("description", "IVF,SQ8"),
            ("include", ["type"]),
            ("vector_index_position", 0),
        ],
    )
    def test_scalar_rejects_vector_only_params(self, field, value) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx, "b", "s", "c", "idx1", "scalar", keys=["email"], **{field: value}
            )
        assert result["success"] is False
        assert field in result["error"]

    def test_scalar_requires_nonempty_keys(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(ctx, "b", "s", "c", "idx1", "scalar")
        assert result["success"] is False
        assert "keys" in result["error"]

    def test_hyperscale_rejects_keys_param(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                keys=["type"],
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
            )
        assert result["success"] is False
        assert "keys" in result["error"]

    def test_hyperscale_rejects_vector_index_position(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
                vector_index_position=0,
            )
        assert result["success"] is False
        assert "vector_index_position" in result["error"]

    def test_composite_rejects_include_param(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "composite_vector",
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
                include=["type"],
            )
        assert result["success"] is False
        assert "include" in result["error"]

    def test_composite_rejects_out_of_range_vector_index_position(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "composite_vector",
                keys=["type"],
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
                vector_index_position=5,
            )
        assert result["success"] is False
        assert "vector_index_position" in result["error"]

    @pytest.mark.parametrize("index_type", ["hyperscale_vector", "composite_vector"])
    def test_vector_requires_vector_field(self, index_type) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                index_type,
                dimension=768,
                similarity="COSINE",
            )
        assert result["success"] is False
        assert "vector_field" in result["error"]

    @pytest.mark.parametrize("index_type", ["hyperscale_vector", "composite_vector"])
    def test_vector_requires_dimension(self, index_type) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                index_type,
                vector_field="embedding",
                similarity="COSINE",
            )
        assert result["success"] is False
        assert "dimension" in result["error"]

    @pytest.mark.parametrize("bad_dimension", [0, -5, 768.5, True])
    def test_vector_rejects_invalid_dimension(self, bad_dimension) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=bad_dimension,
                similarity="COSINE",
            )
        assert result["success"] is False
        assert "dimension" in result["error"]

    @pytest.mark.parametrize("index_type", ["hyperscale_vector", "composite_vector"])
    def test_vector_requires_similarity(self, index_type) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                index_type,
                vector_field="embedding",
                dimension=768,
            )
        assert result["success"] is False
        assert "similarity" in result["error"]

    def test_rejects_dot_product_with_clear_message(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=768,
                similarity="DOT_PRODUCT",
            )
        assert result["success"] is False
        assert "DOT_PRODUCT" in result["error"]
        assert "FTS" in result["error"]

    def test_with_options_collision_with_reserved_key_is_error(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
                with_options={"dimension": 1536},
            )
        assert result["success"] is False
        assert "with_options" in result["error"]


class TestCreateQueryIndexScalarDDL:
    def test_minimal_scalar_statement(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx, "b", "s", "c", "idx1", "scalar", keys=["email"]
            )
        assert result["success"] is True
        statement = result["statement"]
        assert statement == (
            "CREATE INDEX `idx1` ON `c` (email) WITH "
            + json.dumps({"defer_build": True})
        )
        scope.query.assert_called_once_with(statement)

    def test_scalar_with_condition_replicas_and_ignore_if_exists(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "scalar",
                keys=["type", "created_at DESC"],
                condition="type = 'user'",
                num_replicas=1,
                deferred=False,
                ignore_if_exists=True,
            )
        assert result["success"] is True
        assert result["statement"] == (
            "CREATE INDEX `idx1` IF NOT EXISTS ON `c` (type, created_at DESC) "
            "WHERE type = 'user' WITH "
            + json.dumps({"defer_build": False, "num_replicas": 1})
        )
        assert "next_step" not in result


class TestCreateQueryIndexHyperscaleDDL:
    def test_minimal_hyperscale_statement(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=768,
                similarity="cosine",
            )
        assert result["success"] is True
        assert result["statement"] == (
            "CREATE VECTOR INDEX `idx1` ON `c` (`embedding` VECTOR) WITH "
            + json.dumps(
                {"defer_build": True, "dimension": 768, "similarity": "COSINE"}
            )
        )

    def test_hyperscale_include_clause_rendered(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
                include=["type", "metadata.category"],
            )
        assert result["success"] is True
        assert "INCLUDE (`type`, `metadata`.`category`)" in result["statement"]

    def test_hyperscale_description_omitted_when_not_given(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
            )
        assert '"description"' not in result["statement"]

    def test_hyperscale_with_options_merged(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "hyperscale_vector",
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
                description="IVF,SQ8",
                with_options={"scan_nprobes": 4},
            )
        assert result["success"] is True
        expected_with = json.dumps(
            {
                "defer_build": True,
                "dimension": 768,
                "similarity": "COSINE",
                "description": "IVF,SQ8",
                "scan_nprobes": 4,
            }
        )
        assert result["statement"].endswith(f"WITH {expected_with}")


class TestCreateQueryIndexCompositeDDL:
    def test_vector_key_appended_by_default(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "composite_vector",
                keys=["type", "category"],
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
            )
        assert result["success"] is True
        assert "(type, category, `embedding` VECTOR)" in result["statement"]
        assert "USING GSI" in result["statement"]

    def test_vector_key_inserted_at_explicit_position(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "composite_vector",
                keys=["type", "category"],
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
                vector_index_position=1,
            )
        assert result["success"] is True
        assert "(type, `embedding` VECTOR, category)" in result["statement"]

    def test_zero_scalar_keys_is_allowed(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx,
                "b",
                "s",
                "c",
                "idx1",
                "composite_vector",
                vector_field="embedding",
                dimension=768,
                similarity="COSINE",
            )
        assert result["success"] is True
        assert "(`embedding` VECTOR)" in result["statement"]


class TestCreateQueryIndexExecution:
    def test_forces_query_iteration_so_ddl_actually_runs(self) -> None:
        """scope.query() returns a lazily-streamed iterator in the real SDK --
        a mock that is never consumed would let a bug where the result is
        discarded without iterating go unnoticed."""
        ctx = _make_ctx()
        cluster = MagicMock()
        bucket = MagicMock()
        cluster.bucket.return_value = bucket
        scope = MagicMock()
        bucket.scope.return_value = scope

        consumed = []

        def _fake_query(_statement):
            def _gen():
                consumed.append(True)
                yield from ()

            return _gen()

        scope.query.side_effect = _fake_query

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx, "b", "s", "c", "idx1", "scalar", keys=["email"]
            )
        assert result["success"] is True
        assert consumed == [True]

    def test_sdk_error_returns_tool_error_not_raised(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, scope = _cluster_with_scope_query()
        scope.query.side_effect = Exception("index already exists")

        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx, "b", "s", "c", "idx1", "scalar", keys=["email"]
            )
        assert result == {
            "success": False,
            "error": "index already exists",
            "index_name": "idx1",
            "keyspace": "b.s.c",
            "index_type": "scalar",
        }

    def test_deferred_true_includes_next_step(self) -> None:
        ctx = _make_ctx()
        cluster, bucket, _scope = _cluster_with_scope_query()
        with (
            patch(f"{_MODULE}.get_cluster_connection", return_value=cluster),
            patch(f"{_MODULE}.connect_to_bucket", return_value=bucket),
        ):
            result = create_query_index(
                ctx, "b", "s", "c", "idx1", "scalar", keys=["email"], deferred=True
            )
        assert "next_step" in result
        assert "build_index" in result["next_step"]
