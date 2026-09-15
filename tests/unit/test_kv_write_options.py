"""Unit tests for the durability and expiry options on the KV write tools.

These cover the option-building layer specifically: which SDK option object
reaches the SDK, and which inputs are rejected before a write is attempted.
The tools' success/error return paths are covered in test_kv_tools_unit.py.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from couchbase.durability import DurabilityLevel, ServerDurability

from cb_mcp.tools.kv import (
    DURABILITY_LEVELS,
    delete_document_by_id,
    insert_document_by_id,
    replace_document_by_id,
    upsert_document_by_id,
)


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


def _options_passed(mock_op: MagicMock) -> dict:
    """Return the options object handed to a collection op, as a dict."""
    args = mock_op.call_args.args
    assert len(args) >= 2, f"expected an options positional arg, got {args!r}"
    return dict(args[-1])


class TestNoOptions:
    """Omitting every option must reproduce the previous behaviour exactly."""

    def test_upsert_call_shape_is_unchanged(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result == {"success": True}
        collection.upsert.assert_called_once_with("doc1", {"a": 1})

    def test_insert_call_shape_is_unchanged(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            insert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        collection.insert.assert_called_once_with("doc1", {"a": 1})

    def test_replace_call_shape_is_unchanged(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            replace_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        collection.replace.assert_called_once_with("doc1", {"a": 1})

    def test_delete_call_shape_is_unchanged(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            delete_document_by_id(ctx, "b", "s", "c", "doc1")

        collection.remove.assert_called_once_with("doc1")


class TestDurability:
    def test_uses_the_supported_option_key(self) -> None:
        """Durability must travel under the ``durability`` key wrapped in
        ServerDurability. ``durability_level`` is silently dropped by the SDK,
        which would leave the caller believing in a guarantee they don't have."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}, durability="PERSIST_TO_MAJORITY"
            )

        assert result["success"] is True
        options = _options_passed(collection.upsert)
        assert "durability_level" not in options
        assert isinstance(options["durability"], ServerDurability)
        assert options["durability"].level is DurabilityLevel.PERSIST_TO_MAJORITY

    def test_every_documented_level_is_accepted(self) -> None:
        """The advertised names must all map to the SDK enum. A name in the
        docstring that the mapping rejects is a broken tool contract."""
        for name in DURABILITY_LEVELS:
            ctx, cluster, collection = _make_ctx_with_collection()

            with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
                result = upsert_document_by_id(
                    ctx, "b", "s", "c", "doc1", {"a": 1}, durability=name
                )

            assert result["success"] is True, name
            assert (
                _options_passed(collection.upsert)["durability"].level
                is (DURABILITY_LEVELS[name])
            )

    def test_documented_levels_match_the_docstring(self) -> None:
        """Guard against the mapping and the docstring drifting apart."""
        for name in DURABILITY_LEVELS:
            assert name in upsert_document_by_id.__doc__, name

    def test_unknown_level_is_rejected_before_writing(self) -> None:
        """An unusable level must fail loudly rather than fall back to writing
        without the requested guarantee."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}, durability="MAJORITY_ISH"
            )

        assert result["success"] is False
        assert "MAJORITY_ISH" in result["error"]
        collection.upsert.assert_not_called()

    def test_applies_to_delete(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            delete_document_by_id(ctx, "b", "s", "c", "doc1", durability="MAJORITY")

        assert _options_passed(collection.remove)["durability"].level is (
            DurabilityLevel.MAJORITY
        )


class TestExpiry:
    def test_becomes_a_timedelta(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            insert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}, expiry_seconds=90
            )

        assert _options_passed(collection.insert)["expiry"] == timedelta(seconds=90)

    def test_zero_is_sent_rather_than_treated_as_unset(self) -> None:
        """0 is meaningful - it clears an existing TTL - so it must reach the
        SDK rather than being folded into "no option given"."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            upsert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}, expiry_seconds=0
            )

        assert _options_passed(collection.upsert)["expiry"] == timedelta(seconds=0)

    def test_negative_is_rejected_before_writing(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}, expiry_seconds=-1
            )

        assert result["success"] is False
        assert "expiry_seconds" in result["error"]
        collection.upsert.assert_not_called()

    def test_delete_does_not_accept_expiry(self) -> None:
        """RemoveOptions has no expiry; offering one would be a lie."""
        assert (
            "expiry_seconds" not in inspect.signature(delete_document_by_id).parameters
        )


class TestCombined:
    def test_both_options_reach_the_sdk_together(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            replace_document_by_id(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                {"a": 1},
                durability="MAJORITY",
                expiry_seconds=30,
            )

        options = _options_passed(collection.replace)
        assert options["durability"].level is DurabilityLevel.MAJORITY
        assert options["expiry"] == timedelta(seconds=30)

    def test_durability_failure_is_reported_not_retried(self) -> None:
        """A write the cluster cannot make durable must surface the failure,
        never silently succeed at a weaker level."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.upsert.side_effect = Exception("DurabilityImpossibleException")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}, durability="MAJORITY"
            )

        assert result == {
            "success": False,
            "error": "DurabilityImpossibleException",
        }
        collection.upsert.assert_called_once()
