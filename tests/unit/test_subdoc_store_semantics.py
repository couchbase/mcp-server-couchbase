"""Unit tests for store_semantics on mutate_subdocument."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from couchbase.subdocument import StoreSemantics

from cb_mcp.tools.kv import STORE_SEMANTICS, mutate_subdocument


def _make_ctx_with_collection() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    """Build a Context plus its underlying cluster + collection mock."""
    cluster = MagicMock()
    bucket = MagicMock()
    collection = MagicMock()
    bucket.scope.return_value.collection.return_value = collection
    cluster.bucket.return_value = bucket

    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster_provider=SimpleNamespace(get_cluster=lambda c: cluster),
            )
        )
    )
    return ctx, cluster, collection


SPEC = [{"path": "a", "value": 1}]


class TestStoreSemantics:
    def test_omitting_it_preserves_the_previous_call_shape(self) -> None:
        """The default must remain exactly what it was: no options object."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(ctx, "b", "s", "c", "doc1", upsert_specs=SPEC)

        assert len(collection.mutate_in.call_args.args) == 2

    def test_every_documented_value_reaches_the_sdk(self) -> None:
        for name, expected in STORE_SEMANTICS.items():
            ctx, cluster, collection = _make_ctx_with_collection()

            with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
                result = mutate_subdocument(
                    ctx,
                    "b",
                    "s",
                    "c",
                    "doc1",
                    upsert_specs=SPEC,
                    store_semantics=name,
                )

            assert "error" not in result, name
            options = dict(collection.mutate_in.call_args.args[2])
            assert options["store_semantics"] is expected, name

    def test_documented_values_match_the_docstring(self) -> None:
        """Guard against the mapping and the docstring drifting apart."""
        for name in STORE_SEMANTICS:
            assert f'"{name}"' in mutate_subdocument.__doc__, name

    def test_upsert_semantics_is_the_document_creating_one(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(
                ctx, "b", "s", "c", "doc1", upsert_specs=SPEC, store_semantics="UPSERT"
            )

        options = dict(collection.mutate_in.call_args.args[2])
        assert options["store_semantics"] is StoreSemantics.UPSERT

    def test_unknown_value_is_rejected_before_mutating(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                upsert_specs=SPEC,
                store_semantics="CREATE_IF_MISSING",
            )

        assert "CREATE_IF_MISSING" in result["error"]
        collection.mutate_in.assert_not_called()

    def test_missing_specs_are_reported_before_store_semantics(self) -> None:
        """A caller making both mistakes is told about the one that makes the
        call meaningless, rather than about an option it would never use."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(ctx, "b", "s", "c", "doc1", store_semantics="X")

        assert "At least one mutation spec" in result["error"]
        collection.mutate_in.assert_not_called()
