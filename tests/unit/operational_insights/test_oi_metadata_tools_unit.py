"""Unit tests for Operational Insights metadata introspection tools.

Ported from the ``analytics-mcp`` prototype (branch
``DA-2027/Add-enterprise-tools``). Mocks the cluster's
``execute_query()``/``get_all_rows()`` chain so these tests can verify query
construction and the raise-on-error behavior without a live Operational
Insights cluster.
"""

import pytest
from _oi_fakes import make_oi_ctx

from cb_mcp.tools.operational_insights.metadata import (
    MAX_SCHEMA_SAMPLE_SIZE,
    get_collections_in_scope,
    get_databases_in_cluster,
    get_schema_for_collection,
    get_scopes_in_database,
)
from cb_mcp.utils.sqlpp import safe_ident


class TestGetDatabasesInCluster:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = [
            {"DatabaseName": "Default"}
        ]

        result = get_databases_in_cluster(ctx)

        assert result == [{"DatabaseName": "Default"}]

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.side_effect = Exception("connection refused")

        with pytest.raises(Exception, match="connection refused"):
            get_databases_in_cluster(ctx)


class TestGetScopesInDatabase:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = [
            {"DatabaseName": "Default", "ScopeName": "Default"}
        ]

        result = get_scopes_in_database(ctx, "Default")

        assert result == [{"DatabaseName": "Default", "ScopeName": "Default"}]

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.side_effect = Exception("boom")

        with pytest.raises(Exception, match="boom"):
            get_scopes_in_database(ctx, "Default")


class TestGetCollectionsInScope:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = [
            {
                "DatabaseName": "Default",
                "ScopeName": "Default",
                "CollectionName": "oitest_coll",
            }
        ]

        result = get_collections_in_scope(ctx, "Default", "Default")

        assert result[0]["CollectionName"] == "oitest_coll"

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.side_effect = Exception("boom")

        with pytest.raises(Exception, match="boom"):
            get_collections_in_scope(ctx, "Default", "Default")


class TestGetSchemaForCollection:
    def test_returns_rows_on_success(self) -> None:
        ctx, cluster = make_oi_ctx()
        # SELECT VALUE array_infer_schema(...) yields a single row whose
        # value is the array of detected flavor objects. The exact envelope
        # shape isn't asserted on here — this is a pass-through check.
        flavors = [{"properties": {"id": {"type": ["string"]}}}]
        cluster.execute_query.return_value.get_all_rows.return_value = [flavors]

        result = get_schema_for_collection(ctx, "Default", "Default", "oitest_coll")

        assert result == flavors

    def test_raises_on_sdk_error(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.side_effect = Exception("boom")

        with pytest.raises(Exception, match="boom"):
            get_schema_for_collection(ctx, "Default", "Default", "oitest_coll")

    def test_rejects_non_positive_sample_size(self) -> None:
        ctx, cluster = make_oi_ctx()

        with pytest.raises(ValueError, match="sample_size must be positive"):
            get_schema_for_collection(
                ctx, "Default", "Default", "oitest_coll", sample_size=0
            )

        cluster.execute_query.assert_not_called()

    def test_clamps_sample_size_to_max(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = []

        get_schema_for_collection(
            ctx,
            "Default",
            "Default",
            "oitest_coll",
            sample_size=MAX_SCHEMA_SAMPLE_SIZE * 10,
        )

        query_options = cluster.execute_query.call_args[0][1]
        assert (
            query_options["named_parameters"]["sample_size"] == MAX_SCHEMA_SAMPLE_SIZE
        )

    def test_escapes_identifiers_containing_backticks(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = []

        get_schema_for_collection(ctx, "db`.`evil", "s", "c")

        query = cluster.execute_query.call_args[0][0]
        # Each embedded backtick must be doubled (escaped), not left able to
        # close the identifier early.
        assert "`db``.``evil`.`s`.`c`" in query

    def test_defaults_to_no_sample_values(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = []

        get_schema_for_collection(ctx, "Default", "Default", "oitest_coll")

        query_options = cluster.execute_query.call_args[0][1]
        assert query_options["named_parameters"]["infer_params"] == {
            "num_sample_values": 0
        }

    def test_passes_through_requested_num_sample_values(self) -> None:
        ctx, cluster = make_oi_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = []

        get_schema_for_collection(
            ctx, "Default", "Default", "oitest_coll", num_sample_values=5
        )

        query_options = cluster.execute_query.call_args[0][1]
        assert query_options["named_parameters"]["infer_params"] == {
            "num_sample_values": 5
        }

    def test_rejects_negative_num_sample_values(self) -> None:
        ctx, cluster = make_oi_ctx()

        with pytest.raises(ValueError, match="num_sample_values must be non-negative"):
            get_schema_for_collection(
                ctx, "Default", "Default", "oitest_coll", num_sample_values=-1
            )

        cluster.execute_query.assert_not_called()


class TestSafeIdent:
    def test_passes_through_plain_identifier(self) -> None:
        assert safe_ident("my_scope") == "`my_scope`"

    def test_doubles_embedded_backticks(self) -> None:
        assert safe_ident("weird`name") == "`weird``name`"
