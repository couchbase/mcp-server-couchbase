"""Unit tests for the CAS-based optimistic concurrency parameters.

Covers the parse-and-build layer: which CAS reaches the SDK, and which values
are rejected before any write is attempted.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.kv import (
    _parse_cas,
    delete_document_by_id,
    get_document_by_id,
    mutate_subdocument,
    replace_document_by_id,
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
    args = mock_op.call_args.args
    assert len(args) >= 2, f"expected an options positional arg, got {args!r}"
    return dict(args[-1])


def _program_get(collection: MagicMock, content: dict, cas: int) -> None:
    document = MagicMock()
    document.content_as = {dict: content}
    document.cas = cas
    collection.get.return_value = document


class TestParseCas:
    """CAS is unsigned 64-bit and arrives as an LLM-generated string."""

    def test_accepts_a_plain_decimal_string(self) -> None:
        assert _parse_cas("12345") == 12345

    def test_accepts_values_above_2_to_the_53(self) -> None:
        """The reason CAS is a string at all: these do not survive JSON."""
        big = 2**63 + 12345
        assert _parse_cas(str(big)) == big

    @pytest.mark.parametrize(
        "value", ["1_0", " 10 ", "+10", "-1", "0x10", "", "abc", "1.0"]
    )
    def test_rejects_anything_that_is_not_digits(self, value: str) -> None:
        """int() would silently accept several of these as a different number,
        which for a concurrency guard means matching the wrong revision."""
        with pytest.raises(ValueError):
            _parse_cas(value)

    def test_rejects_values_too_large_for_64_bits(self) -> None:
        with pytest.raises(ValueError):
            _parse_cas(str(2**64))


class TestGetWithCas:
    def test_default_return_shape_is_unchanged(self) -> None:
        """Existing callers must see exactly the bare document."""
        ctx, cluster, collection = _make_ctx_with_collection()
        _program_get(collection, {"a": 1}, 12345)

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1")

        assert result == {"a": 1}

    def test_with_cas_returns_content_and_cas(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        _program_get(collection, {"a": 1}, 12345)

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1", with_cas=True)

        assert result == {"content": {"a": 1}, "cas": "12345"}

    def test_large_cas_survives_the_round_trip(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        big = 2**63 + 12345
        _program_get(collection, {"a": 1}, big)

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1", with_cas=True)

        assert _parse_cas(result["cas"]) == big

    def test_missing_document_still_raises(self) -> None:
        """with_cas must not change the not-found contract."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = Exception("DocumentNotFoundException")

        with (
            patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster),
            pytest.raises(Exception, match="DocumentNotFoundException"),
        ):
            get_document_by_id(ctx, "b", "s", "c", "doc1", with_cas=True)


class TestRoundTrip:
    """The CAS a read hands out must be the CAS a write accepts."""

    def test_cas_from_get_is_usable_by_replace(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        _program_get(collection, {"a": 1}, 987654321)

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            read = get_document_by_id(ctx, "b", "s", "c", "doc1", with_cas=True)
            replace_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 2}, cas=read["cas"]
            )

        assert _options_passed(collection.replace)["cas"] == 987654321


class TestCasOnWrites:
    def test_replace_without_cas_preserves_call_shape(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            replace_document_by_id(ctx, "b", "s", "c", "doc1", {"a": 1})

        collection.replace.assert_called_once_with("doc1", {"a": 1})

    def test_delete_without_cas_preserves_call_shape(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            delete_document_by_id(ctx, "b", "s", "c", "doc1")

        collection.remove.assert_called_once_with("doc1")

    def test_delete_passes_cas(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = delete_document_by_id(ctx, "b", "s", "c", "doc1", cas="42")

        assert result == {"success": True}
        assert _options_passed(collection.remove)["cas"] == 42

    def test_mutate_subdocument_passes_cas(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                upsert_specs=[{"path": "a", "value": 1}],
                cas="42",
            )

        assert _options_passed(collection.mutate_in)["cas"] == 42

    def test_mutate_subdocument_without_cas_preserves_call_shape(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(
                ctx, "b", "s", "c", "doc1", upsert_specs=[{"path": "a", "value": 1}]
            )

        assert len(collection.mutate_in.call_args.args) == 2


class TestRejectionBeforeWriting:
    """A malformed CAS must never degrade into an unguarded write."""

    def test_replace(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = replace_document_by_id(
                ctx, "b", "s", "c", "doc1", {"a": 1}, cas="not-a-number"
            )

        assert result["success"] is False
        assert "cas" in result["error"]
        collection.replace.assert_not_called()

    def test_delete(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = delete_document_by_id(ctx, "b", "s", "c", "doc1", cas="1_0")

        assert result["success"] is False
        collection.remove.assert_not_called()

    def test_mutate_subdocument(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx,
                "b",
                "s",
                "c",
                "doc1",
                upsert_specs=[{"path": "a", "value": 1}],
                cas="abc",
            )

        assert "cas" in result["error"]
        collection.mutate_in.assert_not_called()
