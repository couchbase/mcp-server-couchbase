"""Unit tests for get_documents_by_ids (bulk document read)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from couchbase.exceptions import DocumentNotFoundException

from cb_mcp.tools.kv import MAX_BULK_GET_IDS, get_documents_by_ids


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


def _multi_result(results: dict, exceptions: dict | None = None) -> SimpleNamespace:
    """Stand in for MultiGetResult, which exposes results + exceptions maps."""
    return SimpleNamespace(results=results, exceptions=exceptions or {})


def _document(content: dict) -> MagicMock:
    document = MagicMock()
    document.content_as = {dict: content}
    return document


class TestBatchRead:
    def test_returns_documents_keyed_by_id(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_multi.return_value = _multi_result(
            {"doc1": _document({"a": 1}), "doc2": _document({"b": 2})}
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", ["doc1", "doc2"])

        assert result == {
            "documents": {"doc1": {"a": 1}, "doc2": {"b": 2}},
            "errors": {},
        }

    def test_fetches_in_a_single_round_trip(self) -> None:
        """The point of the tool is one call, not one call per ID."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_multi.return_value = _multi_result({})

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            get_documents_by_ids(ctx, "b", "s", "c", ["doc1", "doc2", "doc3"])

        collection.get_multi.assert_called_once()
        assert collection.get_multi.call_args.args[0] == ["doc1", "doc2", "doc3"]

    def test_requests_exceptions_rather_than_raising(self) -> None:
        """Without return_exceptions a single missing key fails the whole
        batch and discards documents that were read successfully."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_multi.return_value = _multi_result({})

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            get_documents_by_ids(ctx, "b", "s", "c", ["doc1"])

        assert collection.get_multi.call_args.kwargs["return_exceptions"] is True


class TestPartialSuccess:
    def test_missing_document_does_not_lose_the_batch(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_multi.return_value = _multi_result(
            {"doc1": _document({"a": 1})},
            {"missing": DocumentNotFoundException("document not found")},
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", ["doc1", "missing"])

        assert result["documents"] == {"doc1": {"a": 1}}
        assert "missing" in result["errors"]

    def test_undecodable_document_is_reported_per_document(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        broken = MagicMock()
        type(broken).content_as = property(
            lambda _self: (_ for _ in ()).throw(ValueError("not JSON"))
        )
        collection.get_multi.return_value = _multi_result(
            {"doc1": _document({"a": 1}), "doc2": broken}
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", ["doc1", "doc2"])

        assert result["documents"] == {"doc1": {"a": 1}}
        assert "not JSON" in result["errors"]["doc2"]

    def test_every_requested_id_appears_exactly_once(self) -> None:
        """The two maps must partition the request, so a caller can tell the
        difference between "absent" and "never looked at"."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_multi.return_value = _multi_result(
            {"doc1": _document({"a": 1})},
            {"doc2": DocumentNotFoundException("nope")},
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", ["doc1", "doc2"])

        assert set(result["documents"]) | set(result["errors"]) == {"doc1", "doc2"}
        assert not set(result["documents"]) & set(result["errors"])


class TestInputBounds:
    def test_empty_list_is_rejected_without_calling_the_cluster(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", [])

        assert "error" in result
        collection.get_multi.assert_not_called()

    def test_batch_at_the_limit_is_allowed(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_multi.return_value = _multi_result({})
        ids = [f"doc{n}" for n in range(MAX_BULK_GET_IDS)]

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", ids)

        assert "documents" in result
        collection.get_multi.assert_called_once()

    def test_oversized_batch_is_rejected_without_calling_the_cluster(self) -> None:
        """Tool output goes into a context window, so the batch is bounded."""
        ctx, cluster, collection = _make_ctx_with_collection()
        ids = [f"doc{n}" for n in range(MAX_BULK_GET_IDS + 1)]

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", ids)

        assert str(MAX_BULK_GET_IDS) in result["error"]
        collection.get_multi.assert_not_called()


class TestFailure:
    def test_connection_failure_matches_the_neighbouring_convention(self) -> None:
        """lookup_subdocument, the tool with the same partial-success shape,
        reports whole-call failure as {"error": ...}. This follows it."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get_multi.side_effect = Exception("connection reset")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_documents_by_ids(ctx, "b", "s", "c", ["doc1"])

        assert result == {"error": "connection reset"}
