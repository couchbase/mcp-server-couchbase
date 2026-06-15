"""Unit tests for KV tool error return paths.

The integration suite covers happy paths and the "document already exists"
/ "document missing" semantics against a live cluster. These unit tests
cover the unexpected-SDK-error branches that can't reliably be triggered
end-to-end:

- upsert_document_by_id returns False on unexpected exception.
- insert / replace / delete error branches (parallel coverage).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cb_mcp.tools.kv import (
    delete_document_by_id,
    insert_document_by_id,
    replace_document_by_id,
    upsert_document_by_id,
)


def _make_ctx_with_collection() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    """Build a Context plus its underlying cluster + collection mock.

    Returns (ctx, cluster, collection) so each test can program the
    collection's individual ops via ``collection.<op>.side_effect``.
    """
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


class TestUpsertDocument:
    """upsert_document_by_id error branch."""

    def test_returns_false_on_sdk_error(self) -> None:
        """An unexpected SDK error must be swallowed and surfaced as False —
        callers rely on the boolean return rather than exception handling."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.upsert.side_effect = Exception("transient error")

        with patch(
            "cb_mcp.tools.kv.get_cluster_connection", return_value=cluster
        ):
            result = upsert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}
            )

        assert result is False
        collection.upsert.assert_called_once_with("doc1", {"a": 1})

    def test_returns_true_on_success(self) -> None:
        """Happy path returns True after invoking collection.upsert."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch(
            "cb_mcp.tools.kv.get_cluster_connection", return_value=cluster
        ):
            result = upsert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}
            )

        assert result is True


class TestInsertDocument:
    """insert_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-exists or any other SDK error must return False."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.insert.side_effect = Exception("DocumentExistsException")

        with patch(
            "cb_mcp.tools.kv.get_cluster_connection", return_value=cluster
        ):
            result = insert_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}
            )

        assert result is False


class TestReplaceDocument:
    """replace_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-not-found or any other SDK error must return False."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.replace.side_effect = Exception("DocumentNotFoundException")

        with patch(
            "cb_mcp.tools.kv.get_cluster_connection", return_value=cluster
        ):
            result = replace_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}
            )

        assert result is False


class TestDeleteDocument:
    """delete_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-not-found or any other SDK error must return False."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.remove.side_effect = Exception("DocumentNotFoundException")

        with patch(
            "cb_mcp.tools.kv.get_cluster_connection", return_value=cluster
        ):
            result = delete_document_by_id(ctx, "b", "s", "c", "doc1")

        assert result is False
