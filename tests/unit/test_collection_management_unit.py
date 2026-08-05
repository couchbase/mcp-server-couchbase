"""Unit tests for the scope/collection management tools.

Covers create_scope, create_collection, delete_scope, delete_collection —
happy paths (SDK collection-manager call forwarding + success envelope) and
log-and-return error handling.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cb_mcp.tools.collection_management import (
    create_collection,
    create_scope,
    delete_collection,
    delete_scope,
)

_GET_CLUSTER = "cb_mcp.tools.collection_management.get_cluster_connection"


def _make_ctx() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    """Build a ctx plus its underlying cluster + collection-manager mock.

    connect_to_bucket() is a thin wrapper over ``cluster.bucket(...)``, so wiring
    the cluster mock is enough — no separate patch needed. Returns
    (ctx, cluster, collection_manager) so tests can program manager ops via
    ``collection_manager.<op>.side_effect``.
    """
    cluster = MagicMock()
    bucket = MagicMock()
    collection_manager = MagicMock()
    bucket.collections.return_value = collection_manager
    cluster.bucket.return_value = bucket
    return SimpleNamespace(), cluster, collection_manager


class TestCreateScope:
    def test_creates_scope(self) -> None:
        ctx, cluster, cm = _make_ctx()
        with patch(_GET_CLUSTER, return_value=cluster):
            result = create_scope(ctx, "b", "s")

        cm.create_scope.assert_called_once_with("s")
        assert result["success"] is True
        assert result["bucket_name"] == "b"
        assert result["scope_name"] == "s"

    def test_sdk_error_returns_error_envelope_not_raised(self) -> None:
        ctx, cluster, cm = _make_ctx()
        cm.create_scope.side_effect = Exception("scope already exists")
        with patch(_GET_CLUSTER, return_value=cluster):
            result = create_scope(ctx, "b", "s")

        assert result["success"] is False
        assert result["error"] == "scope already exists"


class TestCreateCollection:
    def test_creates_collection(self) -> None:
        ctx, cluster, cm = _make_ctx()
        with patch(_GET_CLUSTER, return_value=cluster):
            result = create_collection(ctx, "b", "s", "c")

        cm.create_collection.assert_called_once_with("s", "c")
        assert result["success"] is True
        assert result["collection_name"] == "c"

    def test_sdk_error_returns_error_envelope_not_raised(self) -> None:
        ctx, cluster, cm = _make_ctx()
        cm.create_collection.side_effect = Exception("collection already exists")
        with patch(_GET_CLUSTER, return_value=cluster):
            result = create_collection(ctx, "b", "s", "c")

        assert result["success"] is False
        assert result["error"] == "collection already exists"


class TestDeleteScope:
    def test_deletes_scope(self) -> None:
        ctx, cluster, cm = _make_ctx()
        with patch(_GET_CLUSTER, return_value=cluster):
            result = delete_scope(ctx, "b", "s")

        cm.drop_scope.assert_called_once_with("s")
        assert result["success"] is True
        assert result["scope_name"] == "s"

    def test_sdk_error_returns_error_envelope_not_raised(self) -> None:
        ctx, cluster, cm = _make_ctx()
        cm.drop_scope.side_effect = Exception("scope not found")
        with patch(_GET_CLUSTER, return_value=cluster):
            result = delete_scope(ctx, "b", "s")

        assert result["success"] is False
        assert result["error"] == "scope not found"


class TestDeleteCollection:
    def test_deletes_collection(self) -> None:
        ctx, cluster, cm = _make_ctx()
        with patch(_GET_CLUSTER, return_value=cluster):
            result = delete_collection(ctx, "b", "s", "c")

        cm.drop_collection.assert_called_once_with("s", "c")
        assert result["success"] is True
        assert result["collection_name"] == "c"

    def test_sdk_error_returns_error_envelope_not_raised(self) -> None:
        ctx, cluster, cm = _make_ctx()
        cm.drop_collection.side_effect = Exception("collection not found")
        with patch(_GET_CLUSTER, return_value=cluster):
            result = delete_collection(ctx, "b", "s", "c")

        assert result["success"] is False
        assert result["error"] == "collection not found"
