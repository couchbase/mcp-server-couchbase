"""Unit tests for KV tool error return paths.

The integration suite covers happy paths and the "document already exists"
/ "document missing" semantics against a live cluster. These unit tests
cover the unexpected-SDK-error branches that can't reliably be triggered
end-to-end:

- upsert_document_by_id returns {"success": False, "error": ...} on unexpected exception.
- insert / replace / delete error branches (parallel coverage).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import couchbase.subdocument as subdoc
from couchbase.exceptions import PathMismatchException, PathNotFoundException

from cb_mcp.tools.kv import (
    delete_document_by_id,
    insert_document_by_id,
    lookup_subdocument,
    mutate_subdocument,
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
        """An unexpected SDK error must be swallowed and surfaced with its
        reason — callers rely on the structured return rather than exception
        handling."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.upsert.side_effect = Exception("transient error")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result == {"success": False, "error": "transient error"}
        collection.upsert.assert_called_once_with("doc1", {"a": 1})

    def test_returns_true_on_success(self) -> None:
        """Happy path returns {"success": True} after invoking collection.upsert."""
        ctx, cluster, _collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = upsert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result == {"success": True}


class TestInsertDocument:
    """insert_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-exists or any other SDK error must return a failure dict
        with the reason."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.insert.side_effect = Exception("DocumentExistsException")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = insert_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result == {"success": False, "error": "DocumentExistsException"}


class TestReplaceDocument:
    """replace_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-not-found or any other SDK error must return a failure
        dict with the reason."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.replace.side_effect = Exception("DocumentNotFoundException")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = replace_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        assert result == {"success": False, "error": "DocumentNotFoundException"}


class TestDeleteDocument:
    """delete_document_by_id error branch (parallels upsert)."""

    def test_returns_false_on_sdk_error(self) -> None:
        """Document-not-found or any other SDK error must return a failure
        dict with the reason."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.remove.side_effect = Exception("DocumentNotFoundException")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = delete_document_by_id(ctx, "b", "s", "c", "doc1")

        assert result == {"success": False, "error": "DocumentNotFoundException"}


class _FakeLookupInResult:
    """Mimics the parts of couchbase.result.LookupInResult exercised by
    lookup_subdocument: ``.exists(index)`` and ``.content_as[type](index)``.

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
    """lookup_subdocument: spec building, result grouping, and error handling."""

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
            result = lookup_subdocument(
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
            result = lookup_subdocument(
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
            result = lookup_subdocument(
                ctx, "b", "s", "c", "doc1", exists_paths=["bad.path"]
            )

        assert result["exists"]["bad.path"] == {
            "error": str(PathMismatchException("Path mismatch.")),
        }

    def test_no_paths_returns_error(self) -> None:
        """Calling with no paths at all is a usage error, not an SDK call."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = lookup_subdocument(ctx, "b", "s", "c", "doc1")

        assert "error" in result
        collection.lookup_in.assert_not_called()

    def test_lookup_in_sdk_error_returns_error(self) -> None:
        """An unexpected SDK/connection error is logged and returned, not raised."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.lookup_in.side_effect = Exception("connection reset")

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = lookup_subdocument(ctx, "b", "s", "c", "doc1", get_paths=["name"])

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
            result = lookup_subdocument(
                ctx, "b", "s", "c", "doc1", count_paths=["name", "tags"]
            )

        assert "error" in result["count"]["name"]
        assert result["count"]["tags"] == {"value": 2}


class TestSubDocumentMutateIn:
    """mutate_subdocument: spec building, atomic error handling, and result shaping."""

    class _FakeMutateInResult:
        """Mimics the parts of couchbase.result.MutateInResult exercised by
        mutate_subdocument for reading back a counter's new value.

        ``content_as_works`` models whether the installed SDK's ``content_as``
        actually works for mutate_in results (it doesn't in the version this
        tool was built against, since MutateInResult is built without a
        transcoder there) — when False, ``content_as`` raises like it does in
        that broken SDK version and the tool must fall back to decoding
        ``_orig.raw_result``. ``values`` is positionally aligned with the specs
        passed to ``mutate_in``; non-counter entries can be ``None``.
        """

        def __init__(self, values: list[int | None], *, content_as_works: bool) -> None:
            self._values = values
            self._content_as_works = content_as_works
            self._orig = SimpleNamespace(
                raw_result={
                    "fields": [
                        {"value": json.dumps(v).encode()} if v is not None else {}
                        for v in values
                    ]
                }
            )

        @property
        def content_as(self):
            works = self._content_as_works
            values = self._values

            class _Proxy:
                def __getitem__(self, type_):
                    def getter(index: int):
                        if not works:
                            raise TypeError("simulated missing transcoder")
                        return type_(values[index])

                    return getter

            return _Proxy()

    def test_combined_ops_success(self) -> None:
        """upsert + array_append + counter in one call build the right spec list
        and return values grouped by category, keyed by the requested path."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.mutate_in.return_value = self._FakeMutateInResult(
            [None, None, 5], content_as_works=True
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                upsert_specs=[{"path": "address.city", "value": "Austin"}],
                array_append_specs=[{"path": "tags", "values": ["new"]}],
                counter_specs=[{"path": "views", "delta": 1}],
            )

        expected_specs = [
            subdoc.upsert("address.city", "Austin", create_parents=False),
            subdoc.array_append("tags", "new", create_parents=False),
            subdoc.increment("views", 1, create_parents=False),
        ]
        collection.mutate_in.assert_called_once_with("doc1", expected_specs)
        assert result == {
            "upsert": {"address.city": {"success": True}},
            "array_append": {"tags": {"success": True}},
            "counter": {"views": {"success": True, "value": 5}},
        }

    def test_counter_value_read_via_raw_fallback_when_content_as_broken(self) -> None:
        """When content_as raises (the installed SDK's missing-transcoder gap
        for mutate_in results), the counter's new value is still recovered by
        decoding the raw field data instead of being dropped."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.mutate_in.return_value = self._FakeMutateInResult(
            [7], content_as_works=False
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                counter_specs=[{"path": "views", "delta": 7}],
            )

        assert result == {"counter": {"views": {"success": True, "value": 7}}}

    def test_array_add_unique_uses_array_addunique_builder(self) -> None:
        """array_add_unique_specs must build via subdoc.array_addunique (no second
        underscore in the SDK function name)."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                array_add_unique_specs=[{"path": "tags", "value": "unique-tag"}],
            )

        expected_specs = [
            subdoc.array_addunique("tags", "unique-tag", create_parents=False)
        ]
        collection.mutate_in.assert_called_once_with("doc1", expected_specs)

    def test_counter_negative_delta_dispatches_to_decrement(self) -> None:
        """A negative delta must use subdoc.decrement, not the deprecated subdoc.counter."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.mutate_in.return_value = self._FakeMutateInResult(
            [3], content_as_works=True
        )

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                counter_specs=[{"path": "views", "delta": -2}],
            )

        expected_specs = [subdoc.decrement("views", 2, create_parents=False)]
        collection.mutate_in.assert_called_once_with("doc1", expected_specs)
        assert result == {"counter": {"views": {"success": True, "value": 3}}}

    def test_create_parents_threaded_through(self) -> None:
        """create_parents=True must be passed through to every spec that supports it."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                upsert_specs=[{"path": "a.b.c", "value": 1}],
                create_parents=True,
            )

        expected_specs = [subdoc.upsert("a.b.c", 1, create_parents=True)]
        collection.mutate_in.assert_called_once_with("doc1", expected_specs)

    def test_atomic_sdk_error_returns_error_not_raised(self) -> None:
        """mutate_in is atomic: any failing spec fails the whole call. The error
        must be logged and returned, not raised, and mutate_in called exactly once
        (no partial-success retry logic)."""
        ctx, cluster, collection = _make_ctx_with_collection()
        error_context = SimpleNamespace(
            first_error_index=1, first_error_path="missing.path"
        )
        sdk_error = PathNotFoundException("Path could not be found.")
        sdk_error._context = error_context
        collection.mutate_in.side_effect = sdk_error

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                upsert_specs=[{"path": "a", "value": 1}],
                replace_specs=[{"path": "missing.path", "value": 2}],
            )

        assert "error" in result
        assert "missing.path" in result["error"]
        collection.mutate_in.assert_called_once()

    def test_no_specs_returns_error(self) -> None:
        """Calling with no mutation specs at all is a usage error, not an SDK call."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(ctx, "b", "s", "c", "doc1")

        assert "error" in result
        collection.mutate_in.assert_not_called()

    def test_malformed_spec_missing_key_returns_error_not_raised(self) -> None:
        """A spec dict missing a required key (e.g. 'value') must be reported as
        an error, not raise an uncaught KeyError."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx, "b", "s", "c", "doc1", upsert_specs=[{"path": "a"}]
            )

        assert "error" in result
        collection.mutate_in.assert_not_called()
