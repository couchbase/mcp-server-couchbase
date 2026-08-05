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

import couchbase.subdocument as subdoc
from couchbase.exceptions import PathMismatchException, PathNotFoundException

from cb_mcp.tools.kv import (
    delete_document_by_id,
    insert_document_by_id,
    replace_document_by_id,
    sub_document_lookup_in,
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

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result is False
        collection.upsert.assert_called_once_with("doc1", {"a": 1})

    def test_returns_true_on_success(self) -> None:
        """Happy path returns True after invoking collection.upsert."""
        ctx, cluster, _collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result is True


class TestInsertDocument:
    """insert_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-exists or any other SDK error must return False."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.insert.side_effect = Exception("DocumentExistsException")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = insert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result is False


class TestReplaceDocument:
    """replace_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-not-found or any other SDK error must return False."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.replace.side_effect = Exception("DocumentNotFoundException")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = replace_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result is False


class TestDeleteDocument:
    """delete_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-not-found or any other SDK error must return False."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.remove.side_effect = Exception("DocumentNotFoundException")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = delete_document_by_id(ctx, "b", "s", "c", "doc1")

        assert result is False


class _FakeLookupInResult:
    """Mimics the parts of couchbase.result.LookupInResult exercised by
    sub_document_lookup_in: ``.exists(index)`` and ``.content_as[type](index)``.

    ``entries`` is a list positionally aligned with the specs passed to
    ``lookup_in`` — each entry is either ``{"value": <value>}`` (success) or
    ``{"error": <exception instance>}`` (raises when read).
    """

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    def exists(self, index: int) -> bool:
        entry = self._entries[index]
        if "error" in entry:
            raise entry["error"]
        return entry["value"]

    @property
    def content_as(self):
        entries = self._entries

        class _Proxy:
            def __getitem__(self, type_):
                def getter(index: int):
                    entry = entries[index]
                    if "error" in entry:
                        raise entry["error"]
                    return type_(entry["value"])

                return getter

        return _Proxy()


class TestSubDocumentLookupIn:
    """sub_document_lookup_in: spec building, result grouping, and error handling."""

    def test_combined_ops_success(self) -> None:
        """get + exists + count in one call build the right spec list and
        return values grouped by category, keyed by the requested path."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.lookup_in.return_value = _FakeLookupInResult(
            [
                {"value": "Austin"},  # get: address.city
                {"value": True},  # exists: tags
                {"value": 3},  # count: tags
            ]
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = sub_document_lookup_in(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                get_paths=["address.city"],
                exists_paths=["tags"],
                count_paths=["tags"],
            )

        expected_specs = [
            subdoc.get("address.city"),
            subdoc.exists("tags"),
            subdoc.count("tags"),
        ]
        collection.lookup_in.assert_called_once_with("doc1", expected_specs)
        assert result == {
            "get": {"address.city": {"value": "Austin"}},
            "exists": {"tags": {"value": True}},
            "count": {"tags": {"value": 3}},
        }

    def test_missing_get_path_reported_not_raised(self) -> None:
        """A PathNotFoundException on one spec is reported per-path, not raised."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.lookup_in.return_value = _FakeLookupInResult(
            [{"error": PathNotFoundException("Path could not be found.")}]
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = sub_document_lookup_in(
                ctx, "b", "s", "c", "doc1", get_paths=["missing.path"]
            )

        assert "error" in result["get"]["missing.path"]

    def test_exists_path_error_reported_not_raised(self) -> None:
        """A non-not-found error on an exists spec (e.g. path mismatch) is
        reported per-path rather than propagating."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.lookup_in.return_value = _FakeLookupInResult(
            [{"error": PathMismatchException("Path mismatch.")}]
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = sub_document_lookup_in(
                ctx, "b", "s", "c", "doc1", exists_paths=["bad.path"]
            )

        assert result["exists"]["bad.path"] == {
            "error": str(PathMismatchException("Path mismatch.")),
        }

    def test_no_paths_returns_error(self) -> None:
        """Calling with no paths at all is a usage error, not an SDK call."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = sub_document_lookup_in(ctx, "b", "s", "c", "doc1")

        assert "error" in result
        collection.lookup_in.assert_not_called()

    def test_lookup_in_sdk_error_returns_error(self) -> None:
        """An unexpected SDK/connection error is logged and returned, not raised."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.lookup_in.side_effect = Exception("connection reset")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = sub_document_lookup_in(
                ctx, "b", "s", "c", "doc1", get_paths=["name"]
            )

        assert result == {"error": "connection reset"}

    def test_count_path_mismatch_reported_not_raised(self) -> None:
        """A PathMismatchException on a count spec (e.g. counting a scalar field)
        is reported per-path, and other paths in the same call still resolve."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.lookup_in.return_value = _FakeLookupInResult(
            [
                {
                    "error": PathMismatchException("Path mismatch.")
                },  # count: name (scalar)
                {"value": 2},  # count: tags (array)
            ]
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = sub_document_lookup_in(
                ctx, "b", "s", "c", "doc1", count_paths=["name", "tags"]
            )

        assert "error" in result["count"]["name"]
        assert result["count"]["tags"] == {"value": 2}
