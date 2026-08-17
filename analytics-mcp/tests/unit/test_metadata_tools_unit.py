"""Unit tests for metadata introspection tools.

Mocks the cluster's execute_query()/get_all_rows() chain so these tests can
verify query construction and the raise-on-error behavior without a live
Enterprise Analytics cluster.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ea_mcp.tools.metadata import (
    get_collections_in_scope,
    get_databases_in_cluster,
    get_schema_for_collection,
    get_scopes_in_database,
)


def _make_ctx_with_cluster() -> tuple[SimpleNamespace, MagicMock]:
    """Build a Context plus its underlying cluster mock.

    Returns (ctx, cluster) so each test can program
    cluster.execute_query(...).get_all_rows() directly.
    """
    cluster = MagicMock()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(cluster=cluster)
        )
    )
    return ctx, cluster


class TestGetDatabasesInCluster:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [
            {"DatabaseName": "Default"}
        ]

        with patch(
            "ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster
        ):
            result = get_databases_in_cluster(ctx)

        assert result == [{"DatabaseName": "Default"}]

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("connection refused")

        with (
            patch("ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster),
            pytest.raises(Exception, match="connection refused"),
        ):
            get_databases_in_cluster(ctx)


class TestGetScopesInDatabase:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [
            {"DatabaseName": "Default", "ScopeName": "Default"}
        ]

        with patch(
            "ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster
        ):
            result = get_scopes_in_database(ctx, "Default")

        assert result == [{"DatabaseName": "Default", "ScopeName": "Default"}]

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("boom")

        with (
            patch("ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster),
            pytest.raises(Exception, match="boom"),
        ):
            get_scopes_in_database(ctx, "Default")


class TestGetCollectionsInScope:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [
            {
                "DatabaseName": "Default",
                "ScopeName": "Default",
                "CollectionName": "eatest_coll",
            }
        ]

        with patch(
            "ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster
        ):
            result = get_collections_in_scope(ctx, "Default", "Default")

        assert result[0]["CollectionName"] == "eatest_coll"

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("boom")

        with (
            patch("ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster),
            pytest.raises(Exception, match="boom"),
        ):
            get_collections_in_scope(ctx, "Default", "Default")


class TestGetSchemaForCollection:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = [
            {"field": "id", "data_type": "string", "occurrences": 2}
        ]

        with patch(
            "ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster
        ):
            result = get_schema_for_collection(ctx, "Default", "Default", "eatest_coll")

        assert result == [{"field": "id", "data_type": "string", "occurrences": 2}]

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("boom")

        with (
            patch("ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster),
            pytest.raises(Exception, match="boom"),
        ):
            get_schema_for_collection(ctx, "Default", "Default", "eatest_coll")
