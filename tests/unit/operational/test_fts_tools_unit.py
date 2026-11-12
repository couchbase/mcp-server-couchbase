"""Unit tests for the FTS tools (list, get definition, run/explain, upsert, drop).

Covers:
- list_search_indexes: cluster-level (legacy), bucket-only (enumerate scopes),
  bucket+scope, invalid filter combo, error propagation.
- get_search_index_definition: cluster-level and scope-level happy paths,
  invalid bucket/scope pairing, error propagation.
- run_search: MatchNoneQuery/SearchOptions construction (the query body is
  smuggled through SearchOptions.raw={"query": ...} rather than
  couchbase.search.RawQuery), cluster-level vs scope-level branching, result
  formatting from a mocked SearchResult, and explain=True forcing
  explain/default limit=1 with explanation extraction.
- upsert_search_index / drop_search_index: cluster-level vs scope-level branching,
  the SearchIndex passed to upsert_index, invalid bucket/scope pairing, and
  error propagation.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.operational.fts import (
    drop_search_index,
    get_search_index_definition,
    list_search_indexes,
    run_search,
    upsert_search_index,
)


def _make_ctx_with_fts_managers() -> tuple[
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
        ctx, cluster, cluster_index_manager, bucket = _make_ctx_with_fts_managers()
        cluster_index_manager.get_all_indexes.return_value = [
            _make_index(name="legacy1")
        ]

        with patch(
            "cb_mcp.tools.operational.fts.get_cluster_connection", return_value=cluster
        ):
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
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_fts_managers()
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
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.operational.fts.connect_to_bucket", return_value=bucket
            ),
        ):
            result = list_search_indexes(ctx, bucket_name="b")

        names = {(r["name"], r["bucket"], r["scope"]) for r in result}
        assert names == {
            ("scopeA-idx", "b", "scopeA"),
            ("scopeB-idx", "b", "scopeB"),
        }
        assert bucket.scope.call_count == 2

    def test_bucket_and_scope_targets_single_scope(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_fts_managers()
        scope_mgr = MagicMock()
        scope_mgr.search_indexes.return_value.get_all_indexes.return_value = [
            _make_index(name="scoped1")
        ]
        bucket.scope.return_value = scope_mgr

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.operational.fts.connect_to_bucket", return_value=bucket
            ),
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

    def test_scope_without_bucket_raises_value_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with pytest.raises(
            ValueError, match="bucket_name is required when filtering by scope_name"
        ):
            list_search_indexes(ctx, scope_name="s")

    def test_sdk_error_propagates(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster_index_manager.get_all_indexes.side_effect = Exception(
            "search unavailable"
        )

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(Exception, match="search unavailable"),
        ):
            list_search_indexes(ctx)

    def test_connection_failure_propagates(self) -> None:
        """An unreachable cluster propagates the connection error."""
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            list_search_indexes(ctx)


class TestGetSearchIndexDefinition:
    """Cluster-level vs scope-level lookup and pairing validation."""

    def test_cluster_level_lookup(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster_index_manager.get_index.return_value = _make_index(
            name="idx1", params={"mapping": {}}
        )

        with patch(
            "cb_mcp.tools.operational.fts.get_cluster_connection", return_value=cluster
        ):
            result = get_search_index_definition(ctx, "idx1")

        cluster_index_manager.get_index.assert_called_once_with("idx1")
        assert result["name"] == "idx1"
        assert result["params"] == {"mapping": {}}
        assert result["bucket"] is None
        assert result["scope"] is None

    def test_scope_level_lookup(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_fts_managers()
        scope_mgr = MagicMock()
        scope_mgr.search_indexes.return_value.get_index.return_value = _make_index(
            name="idx1"
        )
        bucket.scope.return_value = scope_mgr

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.operational.fts.connect_to_bucket", return_value=bucket
            ),
        ):
            result = get_search_index_definition(
                ctx, "idx1", bucket_name="b", scope_name="s"
            )

        bucket.scope.assert_called_once_with("s")
        scope_mgr.search_indexes.return_value.get_index.assert_called_once_with("idx1")
        assert result["bucket"] == "b"
        assert result["scope"] == "s"

    def test_partial_pair_raises_value_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with pytest.raises(ValueError, match="must be provided together"):
            get_search_index_definition(ctx, "idx1", bucket_name="b")
        with pytest.raises(ValueError, match="must be provided together"):
            get_search_index_definition(ctx, "idx1", scope_name="s")

    def test_sdk_error_propagates(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster_index_manager.get_index.side_effect = Exception("index not found")

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(Exception, match="index not found"),
        ):
            get_search_index_definition(ctx, "idx1")

    def test_connection_failure_propagates(self) -> None:
        """An unreachable cluster propagates the connection error."""
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
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


class TestRunSearch:
    """MatchNoneQuery/SearchOptions construction and result formatting (explain=False)."""

    def test_cluster_level_query_builds_options_and_formats_hits(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        search_result = _make_search_result(
            rows=[_make_row(id_="doc1", score=1.5, fields={"type": "beer"})],
            metrics=None,
        )
        cluster.search.return_value = search_result

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchOptions") as mock_options,
            patch("cb_mcp.tools.operational.fts.MatchNoneQuery") as mock_match_none,
            patch("cb_mcp.tools.operational.fts.SearchRequest") as mock_search_request,
        ):
            result = run_search(
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
            highlight_fields=["type"],
            disable_scoring=True,
            raw={
                "foo": "bar",
                "query": {"match": "ale"},
                "sort": ["-_score"],
                "facets": {"types": {}},
            },
        )
        mock_match_none.assert_called_once_with()
        mock_search_request.create.assert_called_once_with(mock_match_none.return_value)
        cluster.search.assert_called_once_with(
            "idx1", mock_search_request.create.return_value, mock_options.return_value
        )
        assert result["index_name"] == "idx1"
        assert result["explain"] is False
        assert result["total_hits"] == 1
        assert result["hits"][0]["id"] == "doc1"
        assert result["hits"][0]["score"] == 1.5

    def test_default_limit_is_ten_when_omitted(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster.search.return_value = _make_search_result()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchOptions") as mock_options,
            patch("cb_mcp.tools.operational.fts.MatchNoneQuery"),
            patch("cb_mcp.tools.operational.fts.SearchRequest"),
        ):
            result = run_search(ctx, "idx1", {"match": "ale"})

        assert mock_options.call_args.kwargs["limit"] == 10
        assert result["limit"] == 10

    def test_scoped_query_uses_bucket_scope_search(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_fts_managers()
        scope_obj = MagicMock()
        scope_obj.search.return_value = _make_search_result()
        bucket.scope.return_value = scope_obj

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.operational.fts.connect_to_bucket", return_value=bucket
            ),
            patch("cb_mcp.tools.operational.fts.SearchOptions"),
            patch("cb_mcp.tools.operational.fts.MatchNoneQuery"),
            patch("cb_mcp.tools.operational.fts.SearchRequest"),
        ):
            run_search(ctx, "idx1", {"match": "ale"}, bucket_name="b", scope_name="s")

        bucket.scope.assert_called_once_with("s")
        scope_obj.search.assert_called_once()
        cluster.search.assert_not_called()

    def test_partial_pair_raises_value_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with pytest.raises(ValueError, match="must be provided together"):
            run_search(ctx, "idx1", {"match": "ale"}, bucket_name="b")

    def test_sdk_error_propagates(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster.search.side_effect = Exception("query failed")

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchOptions"),
            patch("cb_mcp.tools.operational.fts.MatchNoneQuery"),
            patch("cb_mcp.tools.operational.fts.SearchRequest"),
            pytest.raises(Exception, match="query failed"),
        ):
            run_search(ctx, "idx1", {"match": "ale"})

    def test_connection_failure_propagates(self) -> None:
        """An unreachable cluster propagates the connection error."""
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            run_search(ctx, "idx1", {"match": "ale"})


class TestRunSearchExplain:
    """explain=True forces SearchOptions(explain=True) and default limit=1."""

    def test_forces_explain_and_default_limit(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster.search.return_value = _make_search_result(
            rows=[_make_row(id_="doc1", score=2.0, explanation={"plan": "x"})]
        )

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchOptions") as mock_options,
            patch("cb_mcp.tools.operational.fts.MatchNoneQuery"),
            patch("cb_mcp.tools.operational.fts.SearchRequest"),
        ):
            result = run_search(ctx, "idx1", {"match": "ale"}, explain=True)

        mock_options.assert_called_once_with(
            explain=True, limit=1, raw={"query": {"match": "ale"}}
        )
        assert result["explain"] is True
        assert result["hits"] == [
            {"id": "doc1", "score": 2.0, "explanation": {"plan": "x"}}
        ]
        assert result["facets"] == {}

    def test_custom_limit_forwarded(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster.search.return_value = _make_search_result()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchOptions") as mock_options,
            patch("cb_mcp.tools.operational.fts.MatchNoneQuery"),
            patch("cb_mcp.tools.operational.fts.SearchRequest"),
        ):
            run_search(ctx, "idx1", {"match": "ale"}, explain=True, limit=5)

        mock_options.assert_called_once_with(
            explain=True, limit=5, raw={"query": {"match": "ale"}}
        )

    def test_sdk_error_propagates(self) -> None:
        ctx, cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster.search.side_effect = Exception("explain failed")

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchOptions"),
            patch("cb_mcp.tools.operational.fts.MatchNoneQuery"),
            patch("cb_mcp.tools.operational.fts.SearchRequest"),
            pytest.raises(Exception, match="explain failed"),
        ):
            run_search(ctx, "idx1", {"match": "ale"}, explain=True)

    def test_connection_failure_propagates(self) -> None:
        """An unreachable cluster propagates the connection error."""
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            run_search(ctx, "idx1", {"match": "ale"}, explain=True)


class TestUpsertSearchIndex:
    """Cluster-level vs scope-level upsert, SearchIndex construction, pairing validation."""

    def test_cluster_level_upsert_builds_search_index(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchIndex") as mock_search_index,
        ):
            result = upsert_search_index(
                ctx,
                "idx1",
                source_name="b",
                params={"mapping": {}},
            )

        mock_search_index.assert_called_once_with(
            name="idx1",
            source_type="couchbase",
            idx_type="fulltext-index",
            source_name="b",
            uuid=None,
            params={"mapping": {}},
            source_uuid=None,
            source_params={},
            plan_params={},
        )
        cluster_index_manager.upsert_index.assert_called_once_with(
            mock_search_index.return_value
        )
        assert result == {
            "success": True,
            "index_name": "idx1",
            "bucket": None,
            "scope": None,
        }

    def test_vector_field_mapping_passes_through_unmodified(self) -> None:
        """A vector field mapping in params reaches SearchIndex() untouched --
        this tool does no mapping validation/transformation of its own."""
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        vector_params = {
            "mapping": {
                "types": {
                    "_default._default": {
                        "properties": {
                            "embedding": {
                                "fields": [
                                    {
                                        "type": "vector",
                                        "dims": 1536,
                                        "similarity": "dot_product",
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch("cb_mcp.tools.operational.fts.SearchIndex") as mock_search_index,
        ):
            result = upsert_search_index(
                ctx,
                "idx_vec",
                source_name="b",
                params=vector_params,
            )

        mock_search_index.assert_called_once_with(
            name="idx_vec",
            source_type="couchbase",
            idx_type="fulltext-index",
            source_name="b",
            uuid=None,
            params=vector_params,
            source_uuid=None,
            source_params={},
            plan_params={},
        )
        cluster_index_manager.upsert_index.assert_called_once_with(
            mock_search_index.return_value
        )
        assert result["success"] is True

    def test_scope_level_upsert_targets_scope_manager(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_fts_managers()
        scope_mgr = MagicMock()
        bucket.scope.return_value = scope_mgr

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.operational.fts.connect_to_bucket", return_value=bucket
            ),
            patch("cb_mcp.tools.operational.fts.SearchIndex") as mock_search_index,
        ):
            result = upsert_search_index(
                ctx,
                "idx1",
                source_name="b",
                bucket_name="b",
                scope_name="s",
                uuid="existing-uuid",
            )

        bucket.scope.assert_called_once_with("s")
        scope_mgr.search_indexes.return_value.upsert_index.assert_called_once_with(
            mock_search_index.return_value
        )
        assert result == {
            "success": True,
            "index_name": "idx1",
            "bucket": "b",
            "scope": "s",
        }

    def test_partial_pair_raises_value_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with pytest.raises(ValueError, match="must be provided together"):
            upsert_search_index(ctx, "idx1", source_name="b", bucket_name="b")
        with pytest.raises(ValueError, match="must be provided together"):
            upsert_search_index(ctx, "idx1", source_name="b", scope_name="s")

    @pytest.mark.parametrize("bad_mapping", ["notadict", 123, [], None])
    def test_non_object_mapping_rejected_before_sdk_call(self, bad_mapping) -> None:
        """A non-object params.mapping segfaults the SDK, so it must never reach it."""
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(ValueError, match=r"params\.mapping must be an object"),
        ):
            upsert_search_index(
                ctx, "idx1", source_name="b", params={"mapping": bad_mapping}
            )
        cluster_index_manager.upsert_index.assert_not_called()

    @pytest.mark.parametrize(
        ("mapping", "bad_path"),
        [
            ({"types": {"hotel": "x"}}, "params.mapping.types.hotel"),
            ({"types": {"hotel": 5}}, "params.mapping.types.hotel"),
            ({"types": {"hotel": None}}, "params.mapping.types.hotel"),
            ({"types": {"hotel": []}}, "params.mapping.types.hotel"),
            (
                {"types": {"hotel": {"properties": {"name": "x"}}}},
                "params.mapping.types.hotel.properties.name",
            ),
            (
                {"types": {"hotel": {"properties": {"a": {"properties": {"b": 5}}}}}},
                "params.mapping.types.hotel.properties.a.properties.b",
            ),
            (
                {"types": {"hotel": {"properties": {"n": {"fields": [None]}}}}},
                "params.mapping.types.hotel.properties.n.fields[0]",
            ),
        ],
    )
    def test_non_object_type_mapping_entries_rejected_before_sdk_call(
        self, mapping, bad_path
    ) -> None:
        """Non-object entries under params.mapping.types segfault the SDK (DA-2407)."""
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(ValueError, match=re.escape(f"{bad_path} must be an object")),
        ):
            upsert_search_index(
                ctx, "idx1", source_name="b", params={"mapping": mapping}
            )
        cluster_index_manager.upsert_index.assert_not_called()

    def test_valid_type_mapping_reaches_sdk(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        mapping = {
            "types": {
                "hotel": {
                    "dynamic": False,
                    "properties": {"name": {"fields": [{"name": "name"}]}},
                }
            }
        }

        with patch(
            "cb_mcp.tools.operational.fts.get_cluster_connection", return_value=cluster
        ):
            result = upsert_search_index(
                ctx, "idx1", source_name="b", params={"mapping": mapping}
            )

        assert result["success"] is True
        cluster_index_manager.upsert_index.assert_called_once()

    def test_sdk_error_propagates(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster_index_manager.upsert_index.side_effect = Exception("invalid mapping")

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(Exception, match="invalid mapping"),
        ):
            upsert_search_index(ctx, "idx1", source_name="b")

    def test_connection_failure_propagates(self) -> None:
        """An unreachable cluster propagates the connection error."""
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            upsert_search_index(ctx, "idx1", source_name="b")


class TestDropSearchIndex:
    """Cluster-level vs scope-level drop and pairing validation."""

    def test_cluster_level_drop(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with patch(
            "cb_mcp.tools.operational.fts.get_cluster_connection", return_value=cluster
        ):
            result = drop_search_index(ctx, "idx1")

        cluster_index_manager.drop_index.assert_called_once_with("idx1")
        assert result == {
            "success": True,
            "index_name": "idx1",
            "bucket": None,
            "scope": None,
        }

    def test_scope_level_drop_targets_scope_manager(self) -> None:
        ctx, cluster, _cluster_index_manager, bucket = _make_ctx_with_fts_managers()
        scope_mgr = MagicMock()
        bucket.scope.return_value = scope_mgr

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            patch(
                "cb_mcp.tools.operational.fts.connect_to_bucket", return_value=bucket
            ),
        ):
            result = drop_search_index(ctx, "idx1", bucket_name="b", scope_name="s")

        bucket.scope.assert_called_once_with("s")
        scope_mgr.search_indexes.return_value.drop_index.assert_called_once_with("idx1")
        assert result == {
            "success": True,
            "index_name": "idx1",
            "bucket": "b",
            "scope": "s",
        }

    def test_partial_pair_raises_value_error(self) -> None:
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with pytest.raises(ValueError, match="must be provided together"):
            drop_search_index(ctx, "idx1", bucket_name="b")
        with pytest.raises(ValueError, match="must be provided together"):
            drop_search_index(ctx, "idx1", scope_name="s")

    def test_sdk_error_propagates(self) -> None:
        ctx, cluster, cluster_index_manager, _bucket = _make_ctx_with_fts_managers()
        cluster_index_manager.drop_index.side_effect = Exception("index not found")

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(Exception, match="index not found"),
        ):
            drop_search_index(ctx, "idx1")

    def test_connection_failure_propagates(self) -> None:
        """An unreachable cluster propagates the connection error."""
        ctx, _cluster, _cluster_index_manager, _bucket = _make_ctx_with_fts_managers()

        with (
            patch(
                "cb_mcp.tools.operational.fts.get_cluster_connection",
                side_effect=Exception("cluster down"),
            ),
            pytest.raises(Exception, match="cluster down"),
        ):
            drop_search_index(ctx, "idx1")
