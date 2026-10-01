"""Unit tests for document_exists_by_id, touch and get_and_touch."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cb_mcp.tools.operational.kv import (
    document_exists_by_id,
    get_and_touch_document_by_id,
    touch_document_by_id,
)


def _make_ctx_with_collection() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
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


class TestDocumentExists:
    def test_present_document_reports_true(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.exists.return_value = SimpleNamespace(exists=True)

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            result = document_exists_by_id(ctx, "b", "s", "c", "doc1")

        assert result == {"exists": True}
        collection.exists.assert_called_once_with("doc1")

    def test_absent_document_reports_false_rather_than_erroring(self) -> None:
        """A missing document is a normal answer here, not a failure."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.exists.return_value = SimpleNamespace(exists=False)

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            result = document_exists_by_id(ctx, "b", "s", "c", "doc1")

        assert result == {"exists": False}
        assert "error" not in result

    def test_server_failure_is_distinguishable_from_absence(self) -> None:
        """A false 'exists' must never be how a failure is reported."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.exists.side_effect = RuntimeError("connection reset")

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            result = document_exists_by_id(ctx, "b", "s", "c", "doc1")

        assert result.get("success") is False
        assert "exists" not in result

    def test_the_body_is_never_fetched(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.exists.return_value = SimpleNamespace(exists=True)

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            document_exists_by_id(ctx, "b", "s", "c", "doc1")

        collection.get.assert_not_called()


class TestTouch:
    def test_expiry_reaches_the_sdk_as_a_timedelta(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            result = touch_document_by_id(ctx, "b", "s", "c", "doc1", 120)

        assert result["success"] is True
        collection.touch.assert_called_once_with("doc1", timedelta(seconds=120))

    def test_zero_clears_the_expiry(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            touch_document_by_id(ctx, "b", "s", "c", "doc1", 0)

        collection.touch.assert_called_once_with("doc1", timedelta(seconds=0))

    def test_negative_expiry_is_rejected_before_any_call(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            result = touch_document_by_id(ctx, "b", "s", "c", "doc1", -1)

        assert result["success"] is False
        assert "expiry_seconds" in result["error"]
        collection.touch.assert_not_called()

    def test_zero_is_documented_as_clearing_the_ttl(self) -> None:
        assert "0 clears an existing expiry" in touch_document_by_id.__doc__


class TestGetAndTouch:
    def test_returns_the_document_content(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_and_touch.return_value = SimpleNamespace(
            content_as={dict: {"field": "value"}}
        )

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            result = get_and_touch_document_by_id(ctx, "b", "s", "c", "doc1", 60)

        assert result == {"field": "value"}
        collection.get_and_touch.assert_called_once_with("doc1", timedelta(seconds=60))

    def test_negative_expiry_is_rejected_before_any_call(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            result = get_and_touch_document_by_id(ctx, "b", "s", "c", "doc1", -5)

        assert result["success"] is False
        collection.get_and_touch.assert_not_called()

    def test_one_round_trip_rather_than_a_get_then_a_touch(self) -> None:
        """The point of the tool is that it is a single operation."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_and_touch.return_value = SimpleNamespace(content_as={dict: {}})

        with patch(
            "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
        ):
            get_and_touch_document_by_id(ctx, "b", "s", "c", "doc1", 60)

        collection.get.assert_not_called()
        collection.touch.assert_not_called()
        assert collection.get_and_touch.call_count == 1
