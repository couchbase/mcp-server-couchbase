"""Unit tests for metadata introspection tools.

Mocks the cluster's execute_query()/get_all_rows() chain so these tests can
verify query construction and the raise-on-error behavior without a live
Enterprise Analytics cluster.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ea_mcp.tools.metadata import (
    MAX_SCHEMA_SAMPLE_SIZE,
    get_collections_in_scope,
    get_databases_in_cluster,
    get_schema_for_collection,
    get_scopes_in_database,
    safe_ident,
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
        # SELECT VALUE array_infer_schema(...) yields a single row whose
        # value is the array of detected flavor objects. The exact envelope
        # shape isn't asserted on here — this is a pass-through check.
        flavors = [{"properties": {"id": {"type": ["string"]}}}]
        cluster.execute_query.return_value.get_all_rows.return_value = [flavors]

        with patch(
            "ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster
        ):
            result = get_schema_for_collection(ctx, "Default", "Default", "eatest_coll")

        assert result == flavors

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.side_effect = Exception("boom")

        with (
            patch("ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster),
            pytest.raises(Exception, match="boom"),
        ):
            get_schema_for_collection(ctx, "Default", "Default", "eatest_coll")

    def test_rejects_non_positive_sample_size(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()

        with (
            patch("ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster),
            pytest.raises(ValueError, match="sample_size must be positive"),
        ):
            get_schema_for_collection(
                ctx, "Default", "Default", "eatest_coll", sample_size=0
            )

        cluster.execute_query.assert_not_called()

    def test_clamps_sample_size_to_max(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = []

        with patch(
            "ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster
        ):
            get_schema_for_collection(
                ctx,
                "Default",
                "Default",
                "eatest_coll",
                sample_size=MAX_SCHEMA_SAMPLE_SIZE * 10,
            )

        query_options = cluster.execute_query.call_args[0][1]
        assert (
            query_options["named_parameters"]["sample_size"] == MAX_SCHEMA_SAMPLE_SIZE
        )

    def test_escapes_identifiers_containing_backticks(self) -> None:
        ctx, cluster = _make_ctx_with_cluster()
        cluster.execute_query.return_value.get_all_rows.return_value = []

        with patch(
            "ea_mcp.tools.metadata.get_cluster_connection", return_value=cluster
        ):
            get_schema_for_collection(ctx, "db`.`evil", "s", "c")

        query = cluster.execute_query.call_args[0][0]
        # Each embedded backtick must be doubled (escaped), not left able to
        # close the identifier early.
        assert "`db``.``evil`.`s`.`c`" in query


class TestSafeIdent:
    def test_passes_through_plain_identifier(self) -> None:
        assert safe_ident("my_scope") == "`my_scope`"

    def test_doubles_embedded_backticks(self) -> None:
        assert safe_ident("weird`name") == "`weird``name`"
