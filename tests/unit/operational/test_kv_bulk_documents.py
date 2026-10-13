"""Unit tests for the optional batch parameter on the KV write tools.

CBSE-23737. The shape under test adds no tools: each of the four existing write
tools accepts either one document or several.

The tests are built around the two things that make the batch form worth having
at all, and around the one hazard it introduces:

  * every requested id comes back in exactly one of succeeded and errors, so a
    partial failure is an answer rather than something to reconstruct;
  * a refused batch writes nothing;
  * and the single-document contract does not move, because every existing
    caller depends on it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.tools.operational.kv import (
    MAX_BULK_DOCS,
    delete_document_by_id,
    insert_document_by_id,
    replace_document_by_id,
    upsert_document_by_id,
)

CONTENT_TOOLS = [
    (upsert_document_by_id, "upsert"),
    (insert_document_by_id, "insert"),
    (replace_document_by_id, "replace"),
]


def _ctx() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
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


def _conn(cluster: MagicMock):
    return patch(
        "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
    )


def _multi(ok: list[str], failed: dict[str, Exception]) -> SimpleNamespace:
    return SimpleNamespace(
        results={k: SimpleNamespace() for k in ok},
        exceptions=dict(failed),
    )


class TestSingleDocumentContractIsUnchanged:
    """Every existing caller passes positionally and expects {"success": True}."""

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_positional_call_still_works(self, tool, sdk) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", "doc1", {"field": "value"})
        assert result == {"success": True}
        getattr(collection, sdk).assert_called_once_with("doc1", {"field": "value"})

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_the_batch_sdk_call_is_not_reached(self, tool, sdk) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            tool(ctx, "b", "s", "c", "doc1", {"field": "value"})
        getattr(collection, f"{sdk}_multi").assert_not_called()

    def test_delete_positional_call_still_works(self) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = delete_document_by_id(ctx, "b", "s", "c", "doc1")
        assert result == {"success": True}
        collection.remove.assert_called_once_with("doc1")


class TestBatchIsOneCall:
    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_three_documents_go_in_one_sdk_call(self, tool, sdk) -> None:
        """The whole economic argument: one tool call, not one per document."""
        ctx, cluster, collection = _ctx()
        docs = {"a": {"n": 1}, "b": {"n": 2}, "c": {"n": 3}}
        getattr(collection, f"{sdk}_multi").return_value = _multi(list(docs), {})

        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", documents=docs)

        assert result["succeeded"] == ["a", "b", "c"]
        assert result["errors"] == {}
        assert getattr(collection, f"{sdk}_multi").call_count == 1
        getattr(collection, sdk).assert_not_called()

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_exceptions_are_requested_from_the_sdk(self, tool, sdk) -> None:
        """Without return_exceptions the first failure raises and the rest of the
        batch's outcomes are lost, which is the opposite of the point."""
        ctx, cluster, collection = _ctx()
        docs = {"a": {"n": 1}}
        getattr(collection, f"{sdk}_multi").return_value = _multi(["a"], {})
        with _conn(cluster):
            tool(ctx, "b", "s", "c", documents=docs)
        _, kwargs = getattr(collection, f"{sdk}_multi").call_args
        assert kwargs["return_exceptions"] is True

    def test_delete_batch_goes_in_one_call(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.remove_multi.return_value = _multi(["a", "b"], {})
        with _conn(cluster):
            result = delete_document_by_id(ctx, "b", "s", "c", document_ids=["a", "b"])
        assert result["succeeded"] == ["a", "b"]
        assert collection.remove_multi.call_count == 1
        collection.remove.assert_not_called()


class TestPartialFailureIsACompleteAccount:
    """The property the single-document path cannot offer across several calls."""

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_every_requested_id_lands_in_exactly_one_bucket(self, tool, sdk) -> None:
        ctx, cluster, collection = _ctx()
        docs = {"a": {}, "b": {}, "c": {}}
        getattr(collection, f"{sdk}_multi").return_value = _multi(
            ["a", "c"], {"b": ValueError("nope")}
        )

        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", documents=docs)

        assert set(result["succeeded"]) | set(result["errors"]) == set(docs)
        assert not set(result["succeeded"]) & set(result["errors"])

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_the_failure_reason_carries_the_exception_type(self, tool, sdk) -> None:
        """A bare SDK exception stringifies to nothing; the type is the content."""
        ctx, cluster, collection = _ctx()
        getattr(collection, f"{sdk}_multi").return_value = _multi([], {"b": KeyError()})
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", documents={"b": {}})
        assert "KeyError" in result["errors"]["b"]

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_an_id_with_no_reported_outcome_is_an_error_not_a_success(
        self, tool, sdk
    ) -> None:
        """A missing outcome is not a success. Counting it as one would tell the
        caller a document was written when nothing says it was."""
        ctx, cluster, collection = _ctx()
        getattr(collection, f"{sdk}_multi").return_value = _multi(["a"], {})
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", documents={"a": {}, "ghost": {}})
        assert result["succeeded"] == ["a"]
        assert "ghost" in result["errors"]
        assert "retry" in result["errors"]["ghost"]


class TestARefusedBatchWritesNothing:
    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_over_the_limit_is_refused_before_any_write(self, tool, sdk) -> None:
        ctx, cluster, collection = _ctx()
        docs = {f"doc{i}": {} for i in range(MAX_BULK_DOCS + 1)}
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", documents=docs)
        assert result["success"] is False
        assert str(MAX_BULK_DOCS) in result["error"]
        getattr(collection, f"{sdk}_multi").assert_not_called()
        getattr(collection, sdk).assert_not_called()

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_exactly_the_limit_is_allowed(self, tool, sdk) -> None:
        """Off-by-one in the safe direction is still a bug."""
        ctx, cluster, collection = _ctx()
        docs = {f"doc{i}": {} for i in range(MAX_BULK_DOCS)}
        getattr(collection, f"{sdk}_multi").return_value = _multi(list(docs), {})
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", documents=docs)
        assert result["success"] is True

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_an_empty_batch_is_refused(self, tool, sdk) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", documents={})
        assert result["success"] is False
        getattr(collection, f"{sdk}_multi").assert_not_called()

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_both_modes_at_once_is_refused(self, tool, sdk) -> None:
        """A model that produced both arguments has not decided what it is doing.
        Silently preferring one is how a document gets written unintentionally."""
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = tool(
                ctx, "b", "s", "c", "doc1", {"field": "v"}, documents={"a": {}}
            )
        assert result["success"] is False
        assert "not both" in result["error"]
        getattr(collection, sdk).assert_not_called()
        getattr(collection, f"{sdk}_multi").assert_not_called()

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_neither_mode_is_refused(self, tool, sdk) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c")
        assert result["success"] is False
        getattr(collection, sdk).assert_not_called()
        getattr(collection, f"{sdk}_multi").assert_not_called()

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_single_mode_without_content_is_refused(self, tool, sdk) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = tool(ctx, "b", "s", "c", "doc1")
        assert result["success"] is False
        assert "document_content" in result["error"]
        getattr(collection, sdk).assert_not_called()

    def test_delete_over_the_limit_is_refused(self) -> None:
        ctx, cluster, collection = _ctx()
        ids = [f"doc{i}" for i in range(MAX_BULK_DOCS + 1)]
        with _conn(cluster):
            result = delete_document_by_id(ctx, "b", "s", "c", document_ids=ids)
        assert result["success"] is False
        collection.remove_multi.assert_not_called()


class TestDeleteSpecifics:
    def test_a_repeated_id_is_deleted_once(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.remove_multi.return_value = _multi(["a", "b"], {})
        with _conn(cluster):
            result = delete_document_by_id(
                ctx, "b", "s", "c", document_ids=["a", "b", "a"]
            )
        args, _ = collection.remove_multi.call_args
        assert args[0] == ["a", "b"]
        assert result["succeeded"] == ["a", "b"]

    def test_an_absent_document_is_reported_rather_than_counted_as_deleted(
        self,
    ) -> None:
        ctx, cluster, collection = _ctx()
        collection.remove_multi.return_value = _multi(
            ["a"], {"gone": KeyError("not found")}
        )
        with _conn(cluster):
            result = delete_document_by_id(
                ctx, "b", "s", "c", document_ids=["a", "gone"]
            )
        assert result["succeeded"] == ["a"]
        assert "gone" in result["errors"]


class TestDocstringsSteerTheModel:
    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_the_batch_form_is_advertised(self, tool, sdk) -> None:
        doc = " ".join((tool.__doc__ or "").split())
        assert "Prefer the batch form" in doc

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_the_documented_limit_matches_the_enforced_one(self, tool, sdk) -> None:
        """The docstring carries the number because the model cannot read a
        constant. This pins the two together so they cannot drift."""
        doc = " ".join((tool.__doc__ or "").split())
        assert f"up to {MAX_BULK_DOCS} documents" in doc

    def test_delete_documents_the_same_limit(self) -> None:
        doc = " ".join((delete_document_by_id.__doc__ or "").split())
        assert f"up to {MAX_BULK_DOCS} documents" in doc

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_the_return_shapes_are_written_out_literally(self, tool, sdk) -> None:
        """Asserts the exact text rather than scanning for stray braces: the
        batch shape legitimately ends in }} and a brace scan flags it."""
        doc = " ".join((tool.__doc__ or "").split())
        assert '{"success": True}' in doc
        assert '"errors": {...}}' in doc

    @pytest.mark.parametrize("tool,sdk", CONTENT_TOOLS)
    def test_no_template_placeholder_survived(self, tool, sdk) -> None:
        doc = tool.__doc__ or ""
        assert "<<" not in doc
        assert "MAX_BULK_DOCS" not in doc

    def test_insert_warns_against_escalating_a_partial_failure(self) -> None:
        """The failure mode that does silent damage: an insert batch partially
        fails, and the model 'fixes' it by upserting the whole batch."""
        doc = " ".join((insert_document_by_id.__doc__ or "").split())
        assert "must NOT be followed by an upsert" in doc
