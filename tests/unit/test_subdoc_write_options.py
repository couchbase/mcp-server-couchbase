"""Unit tests for durability on mutate_subdocument."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from couchbase.durability import ServerDurability

from cb_mcp.tools.kv import DURABILITY_LEVELS, mutate_subdocument


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


class TestDurability:
    def test_omitting_it_preserves_the_previous_call_shape(self) -> None:
        """The default must remain exactly what it was: no options object."""
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(ctx, "b", "s", "c", "doc1", upsert_specs=SPEC)

        assert len(collection.mutate_in.call_args.args) == 2

    def test_every_documented_level_reaches_the_sdk(self) -> None:
        for name, expected in DURABILITY_LEVELS.items():
            ctx, cluster, collection = _make_ctx_with_collection()

            with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
                result = mutate_subdocument(
                    ctx, "b", "s", "c", "doc1", upsert_specs=SPEC, durability=name
                )

            assert "error" not in result, name
            options = dict(collection.mutate_in.call_args.args[2])
            durability = options["durability"]
            assert isinstance(durability, ServerDurability), name
            assert durability.level is expected, name

    def test_durability_level_key_is_never_sent(self) -> None:
        """The SDK reads ``durability``; ``durability_level`` is silently dropped.

        The Options classes are unvalidated dict subclasses, so sending the
        wrong key is accepted, discarded, and the mutation then reports success
        without ever having requested the guarantee. This asserts the mistake
        is not present.
        """
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            mutate_subdocument(
                ctx, "b", "s", "c", "doc1", upsert_specs=SPEC, durability="MAJORITY"
            )

        options = dict(collection.mutate_in.call_args.args[2])
        assert "durability_level" not in options
        assert "durability" in options

    def test_documented_levels_match_the_docstring(self) -> None:
        """Guard against the mapping and the docstring drifting apart."""
        for name in DURABILITY_LEVELS:
            assert f'"{name}"' in mutate_subdocument.__doc__, name

    def test_invalid_level_is_rejected_and_nothing_is_mutated(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()

        with patch("cb_mcp.tools.kv.get_cluster_connection", return_value=cluster):
            result = mutate_subdocument(
                ctx, "b", "s", "c", "doc1", upsert_specs=SPEC, durability="NOT_A_LEVEL"
            )

        assert "error" in result
        assert "NOT_A_LEVEL" in result["error"]
        for level in DURABILITY_LEVELS:
            assert level in result["error"]
        collection.mutate_in.assert_not_called()


class TestNoExpiryOption:
    def test_mutate_in_options_has_no_expiry_key(self) -> None:
        """MutateInOptions has no expiry; the docstring must not imply one.

        MutateInOptionsBase accepts timeout, cas, durability, store_semantics,
        access_deleted and preserve_expiry. A TTL cannot be set through
        mutate_in, so this tool must not grow an expiry parameter that the SDK
        would silently drop.
        """
        signature = inspect.signature(mutate_subdocument)
        assert "expiry_seconds" not in signature.parameters
        assert "expiry" not in signature.parameters
