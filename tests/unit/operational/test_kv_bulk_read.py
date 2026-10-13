"""Unit tests for the optional batch parameter on get_document_by_id.

CBSE-23739, PR #274, reshaped. Adds no tools.

The tests are built around the four things that make this a different
capability from `SELECT * FROM b USE KEYS [...]` rather than a duplicate of it:
a bound on the result, per-key outcomes, no dependence on the Query service,
and no dependence on a query privilege the KV principal may not hold.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from couchbase.exceptions import DocumentNotFoundException

from cb_mcp.tools.operational.kv import (
    BULK_READ_BYTES_ENV_VAR,
    DEFAULT_BULK_READ_BYTES,
    MAX_BULK_READ_BYTES_CEILING,
    MAX_BULK_READ_DOCS,
    _shape_multi_get,
    bulk_read_byte_limit,
    get_document_by_id,
)


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


def _doc(content: dict) -> SimpleNamespace:
    return SimpleNamespace(content_as={dict: content})


def _multi(found: dict[str, dict], failed: dict[str, Exception]) -> SimpleNamespace:
    return SimpleNamespace(
        results={k: _doc(v) for k, v in found.items()},
        exceptions=dict(failed),
    )


class TestSingleDocumentContractIsUnchanged:
    def test_positional_call_returns_the_bare_document(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get.return_value = _doc({"field": "value"})
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1")
        assert result == {"field": "value"}

    def test_not_found_still_raises(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get.side_effect = DocumentNotFoundException()
        with _conn(cluster), pytest.raises(DocumentNotFoundException):
            get_document_by_id(ctx, "b", "s", "c", "doc1")

    def test_the_batch_sdk_call_is_not_reached(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get.return_value = _doc({})
        with _conn(cluster):
            get_document_by_id(ctx, "b", "s", "c", "doc1")
        collection.get_multi.assert_not_called()


class TestBatchIsOneCall:
    def test_three_documents_go_in_one_sdk_call(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {"a": {"n": 1}, "b": {"n": 2}, "c": {"n": 3}}, {}
        )
        with _conn(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", document_ids=["a", "b", "c"]
            )
        assert result["documents"] == {"a": {"n": 1}, "b": {"n": 2}, "c": {"n": 3}}
        assert result["errors"] == {}
        assert collection.get_multi.call_count == 1
        collection.get.assert_not_called()

    def test_exceptions_are_requested_from_the_sdk(self) -> None:
        """Without return_exceptions the first missing document raises and the
        rest of the batch's outcomes are lost."""
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi({"a": {}}, {})
        with _conn(cluster):
            get_document_by_id(ctx, "b", "s", "c", document_ids=["a"])
        _, kwargs = collection.get_multi.call_args
        assert kwargs["return_exceptions"] is True

    def test_a_repeated_id_is_read_once(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi({"a": {}, "b": {}}, {})
        with _conn(cluster):
            get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "b", "a"])
        args, _ = collection.get_multi.call_args
        assert args[0] == ["a", "b"]


class TestPerKeyOutcomes:
    """The capability SQL++ USE KEYS cannot offer: a key that is absent simply
    produces no row there, so the caller is left diffing and still cannot tell
    'absent' from 'I mistyped it'."""

    def test_every_requested_id_lands_in_exactly_one_bucket(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {"a": {"n": 1}, "c": {"n": 3}}, {"b": DocumentNotFoundException()}
        )
        with _conn(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", document_ids=["a", "b", "c"]
            )
        asked = {"a", "b", "c"}
        buckets = [
            set(result["documents"]),
            set(result["errors"]),
            set(result["oversize"]),
            set(result["not_returned"]),
        ]
        assert set().union(*buckets) == asked
        assert sum(len(b) for b in buckets) == len(asked)

    def test_a_missing_document_is_named_and_explained(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {}, {"gone": DocumentNotFoundException()}
        )
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["gone"])
        assert "DocumentNotFoundException" in result["errors"]["gone"]

    def test_a_batch_never_raises_for_one_document(self) -> None:
        """The single form raises on not-found, which is right for one document
        and wrong for fifty: one absent key must not discard 49 answers."""
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {"a": {"n": 1}}, {"b": DocumentNotFoundException()}
        )
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "b"])
        assert result["documents"] == {"a": {"n": 1}}
        assert "b" in result["errors"]

    def test_an_id_with_no_reported_outcome_is_an_error_not_an_absence(self) -> None:
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi({"a": {}}, {})
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "ghost"])
        assert "ghost" in result["errors"]
        assert "does not establish that it is absent" in result["errors"]["ghost"]

    def test_an_undecodable_document_is_an_error_not_a_crash(self) -> None:
        ctx, cluster, collection = _ctx()
        bad = SimpleNamespace()
        collection.get_multi.return_value = SimpleNamespace(
            results={"a": bad}, exceptions={}
        )
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a"])
        assert result["success"] is True
        assert "a" in result["errors"]


class TestTheResultIsBounded:
    """If result size and context pressure are the concern, the bounded option
    is this tool. SELECT * ... USE KEYS has no equivalent cap."""

    def test_over_the_limit_is_refused_before_the_cluster_is_touched(self) -> None:
        ctx, cluster, collection = _ctx()
        ids = [f"doc{i}" for i in range(MAX_BULK_READ_DOCS + 1)]
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=ids)
        assert result["success"] is False
        assert str(MAX_BULK_READ_DOCS) in result["error"]
        collection.get_multi.assert_not_called()

    def test_exactly_the_limit_is_allowed(self) -> None:
        ctx, cluster, collection = _ctx()
        ids = [f"doc{i}" for i in range(MAX_BULK_READ_DOCS)]
        collection.get_multi.return_value = _multi({i: {} for i in ids}, {})
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=ids)
        assert result["success"] is True

    def test_an_empty_batch_is_refused(self) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=[])
        assert result["success"] is False
        collection.get_multi.assert_not_called()

    def test_both_modes_at_once_is_refused(self) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1", document_ids=["a"])
        assert result["success"] is False
        assert "not both" in result["error"]
        collection.get.assert_not_called()
        collection.get_multi.assert_not_called()

    def test_neither_mode_is_refused(self) -> None:
        ctx, cluster, collection = _ctx()
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c")
        assert result["success"] is False
        collection.get.assert_not_called()
        collection.get_multi.assert_not_called()


class TestDocstringSteersTheModel:
    @staticmethod
    def _doc() -> str:
        return " ".join((get_document_by_id.__doc__ or "").split())

    def test_the_batch_form_is_advertised(self) -> None:
        assert "Prefer the batch form" in self._doc()

    def test_the_documented_limit_matches_the_enforced_one(self) -> None:
        assert f"up to {MAX_BULK_READ_DOCS} documents" in self._doc()

    def test_the_two_return_shapes_are_distinguished(self) -> None:
        doc = self._doc()
        assert "Single form returns the document" in doc
        assert '"documents": {id: content}' in doc

    def test_no_template_placeholder_survived(self) -> None:
        raw = get_document_by_id.__doc__ or ""
        assert "<<" not in raw


class TestResultSizeIsBounded:
    """Truncation is never silent, and nothing is ever cut mid-document."""

    def test_a_document_that_does_not_fit_is_listed_as_not_returned(self) -> None:
        big = {"blob": "x" * 600}
        result = _shape_multi_get(
            _multi({"a": big, "b": big, "c": {"k": 1}}, {}),
            ["a", "b", "c"],
            max_bytes=1000,
        )
        assert list(result["documents"]) == ["a"]
        # c would fit on its own, but once the bound is reached everything after
        # it is deferred, so not_returned is exactly what to ask for next.
        assert result["not_returned"] == ["b", "c"]
        assert result["truncated"] is True
        assert "document_ids set to not_returned" in result["not_returned_guidance"]

    def test_bytes_returned_and_the_limit_are_reported(self) -> None:
        doc = {"k": 1}
        result = _shape_multi_get(_multi({"a": doc}, {}), ["a"], max_bytes=1000)
        assert result["bytes_returned"] == len(json.dumps(doc))
        assert result["limit_bytes"] == 1000
        assert result["truncated"] is False
        assert result["not_returned"] == []
        assert "not_returned_guidance" not in result

    def test_every_requested_id_is_accounted_for(self) -> None:
        big = {"blob": "x" * 600}
        ids = ["a", "b", "c", "d", "e"]
        result = _shape_multi_get(
            _multi(
                {"a": big, "b": big, "c": big, "e": {"blob": "x" * 5000}},
                {"d": DocumentNotFoundException()},
            ),
            ids,
            max_bytes=1000,
        )
        listed = [
            *result["documents"],
            *result["errors"],
            *result["oversize"],
            *result["not_returned"],
        ]
        assert sorted(listed) == ids
        assert len(listed) == len(set(listed))

    def test_a_document_larger_than_the_whole_bound_is_oversize_not_cut(self) -> None:
        result = _shape_multi_get(
            _multi({"a": {"blob": "x" * 50}}, {}), ["a"], max_bytes=10
        )
        assert result["documents"] == {}
        assert result["errors"] == {}
        assert result["oversize"]["a"] > 10
        assert result["truncated"] is False
        assert "document_id" in result["oversize_guidance"]

    def test_an_oversize_document_does_not_stop_later_documents(self) -> None:
        result = _shape_multi_get(
            _multi({"a": {"blob": "x" * 5000}, "b": {"k": 1}}, {}),
            ["a", "b"],
            max_bytes=1000,
        )
        assert list(result["oversize"]) == ["a"]
        assert list(result["documents"]) == ["b"]

    def test_the_default_bound_is_applied_through_the_tool(self) -> None:
        ctx, cluster, collection = _ctx()
        huge = {"blob": "x" * (DEFAULT_BULK_READ_BYTES // 2 + 1)}
        collection.get_multi.return_value = _multi({"a": huge, "b": huge}, {})
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "b"])
        assert list(result["documents"]) == ["a"]
        assert result["not_returned"] == ["b"]
        assert result["truncated"] is True

    def test_each_error_message_is_capped(self) -> None:
        class _HugeError(Exception):
            def __str__(self) -> str:
                return "x" * 50_000

        ids = [f"doc{i}" for i in range(MAX_BULK_READ_DOCS)]
        shaped = _shape_multi_get(
            SimpleNamespace(exceptions={i: _HugeError() for i in ids}, results={}),
            ids,
        )
        assert len(json.dumps(shaped)) < 60_000

    def test_the_documented_size_limit_is_stated(self) -> None:
        doc = " ".join((get_document_by_id.__doc__ or "").split())
        assert "limited to 64 KiB" in doc
        assert "up to 50 documents" in doc

    def test_the_return_shape_names_truncation_fields(self) -> None:
        doc = " ".join((get_document_by_id.__doc__ or "").split())
        for field in ('"not_returned": [ids]', '"truncated": bool', "oversize"):
            assert field in doc


class TestTheByteLimitIsConfigurableButNeverUnbounded:
    def test_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(BULK_READ_BYTES_ENV_VAR, raising=False)
        assert bulk_read_byte_limit() == DEFAULT_BULK_READ_BYTES == 64 * 1024

    def test_a_deployment_can_lower_it(self, monkeypatch) -> None:
        monkeypatch.setenv(BULK_READ_BYTES_ENV_VAR, "4096")
        assert bulk_read_byte_limit() == 4096

    def test_a_deployment_can_raise_it_only_to_the_ceiling(self, monkeypatch) -> None:
        monkeypatch.setenv(BULK_READ_BYTES_ENV_VAR, str(10 * 1024 * 1024))
        assert bulk_read_byte_limit() == MAX_BULK_READ_BYTES_CEILING == 128 * 1024

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "1.5"])
    def test_a_bad_value_falls_back_to_the_default(self, monkeypatch, bad) -> None:
        monkeypatch.setenv(BULK_READ_BYTES_ENV_VAR, bad)
        assert bulk_read_byte_limit() == DEFAULT_BULK_READ_BYTES

    def test_the_tool_honours_the_configured_limit(self, monkeypatch) -> None:
        monkeypatch.setenv(BULK_READ_BYTES_ENV_VAR, "200")
        ctx, cluster, collection = _ctx()
        collection.get_multi.return_value = _multi(
            {"a": {"blob": "x" * 150}, "b": {"blob": "x" * 150}}, {}
        )
        with _conn(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", document_ids=["a", "b"])
        assert result["limit_bytes"] == 200
        assert list(result["documents"]) == ["a"]
        assert result["not_returned"] == ["b"]
