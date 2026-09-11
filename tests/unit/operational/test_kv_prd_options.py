"""Unit tests for the PRD write options: CAS in write responses, preserve_expiry,
durability/expiry on mutate_subdocument, and the stable error codes."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from couchbase.exceptions import (
    DurabilityImpossibleException,
    DurabilitySyncWriteAmbiguousException,
)
from couchbase.options import MutateInOptions, UpsertOptions

from cb_mcp.tools.operational.kv import (
    delete_document_by_id,
    insert_document_by_id,
    mutate_subdocument,
    replace_document_by_id,
    upsert_document_by_id,
)
from cb_mcp.utils.operational.kv_utils import kv_error, sdk_kv_options

_BIG_CAS = 18446744073709551000  # above 2**53: must survive as a string


def _ctx() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    cluster, bucket, collection = MagicMock(), MagicMock(), MagicMock()
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


def _run(fn, *args, **kwargs):
    ctx, cluster, collection = _ctx()
    configure = kwargs.pop("configure", None)
    if configure:
        configure(collection)
    with patch(
        "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
    ):
        return fn(ctx, "b", "s", "c", "doc1", *args, **kwargs), collection


class TestCasInWriteResponses:
    @pytest.mark.parametrize(
        ("fn", "op", "args"),
        [
            (upsert_document_by_id, "upsert", ({"a": 1},)),
            (insert_document_by_id, "insert", ({"a": 1},)),
            (replace_document_by_id, "replace", ({"a": 1},)),
            (delete_document_by_id, "remove", ()),
        ],
    )
    def test_success_carries_the_new_cas_as_a_string(self, fn, op, args) -> None:
        result, _ = _run(
            fn,
            *args,
            configure=lambda c: getattr(c, op).__setattr__(
                "return_value", SimpleNamespace(cas=_BIG_CAS)
            ),
        )
        assert result == {"success": True, "cas": str(_BIG_CAS)}

    def test_cas_is_omitted_when_the_server_gives_none(self) -> None:
        result, _ = _run(
            upsert_document_by_id,
            {"a": 1},
            configure=lambda c: setattr(
                c.upsert, "return_value", SimpleNamespace(cas=0)
            ),
        )
        assert result == {"success": True}

    def test_mutate_subdocument_carries_the_new_cas(self) -> None:
        result, _ = _run(
            mutate_subdocument,
            upsert_specs=[{"path": "a", "value": 1}],
            configure=lambda c: setattr(
                c.mutate_in, "return_value", SimpleNamespace(cas=_BIG_CAS)
            ),
        )
        assert result["cas"] == str(_BIG_CAS)
        assert result["upsert"] == {"a": {"success": True}}


class TestPreserveExpiry:
    def test_reaches_the_sdk_on_upsert(self) -> None:
        opts = sdk_kv_options(UpsertOptions, preserve_expiry=True)
        assert dict(opts) == {"preserve_expiry": True}

    def test_defaults_off_and_leaves_the_call_unchanged(self) -> None:
        assert sdk_kv_options(UpsertOptions, None, None, False) is None

    def test_with_expiry_is_rejected_with_a_stable_code(self) -> None:
        result, collection = _run(
            upsert_document_by_id,
            {"a": 1},
            expiry_seconds=60,
            preserve_expiry=True,
        )
        assert result["success"] is False
        assert result["code"] == "invalid_expiry"
        collection.upsert.assert_not_called()

    def test_replace_accepts_it(self) -> None:
        _, collection = _run(replace_document_by_id, {"a": 1}, preserve_expiry=True)
        assert dict(collection.replace.call_args.args[-1]) == {"preserve_expiry": True}


class TestMutateSubdocumentOptions:
    def test_no_options_keeps_the_original_call_shape(self) -> None:
        _, collection = _run(
            mutate_subdocument, upsert_specs=[{"path": "a", "value": 1}]
        )
        assert len(collection.mutate_in.call_args.args) == 2

    def test_durability_expiry_reach_the_sdk(self) -> None:
        _, collection = _run(
            mutate_subdocument,
            upsert_specs=[{"path": "a", "value": 1}],
            durability="MAJORITY",
            expiry_seconds=90,
        )
        options = collection.mutate_in.call_args.args[-1]
        assert isinstance(options, MutateInOptions)
        assert dict(options)["expiry"] == timedelta(seconds=90)
        assert "durability" in dict(options)

    def test_preserve_expiry_reaches_the_sdk(self) -> None:
        _, collection = _run(
            mutate_subdocument,
            upsert_specs=[{"path": "a", "value": 1}],
            preserve_expiry=True,
        )
        assert dict(collection.mutate_in.call_args.args[-1]) == {
            "preserve_expiry": True
        }

    def test_bad_durability_is_rejected_before_any_call(self) -> None:
        result, collection = _run(
            mutate_subdocument,
            upsert_specs=[{"path": "a", "value": 1}],
            durability="SOMETIMES",
        )
        assert result["code"] == "invalid_durability_level"
        collection.mutate_in.assert_not_called()

    def test_expiry_with_preserve_is_rejected(self) -> None:
        result, collection = _run(
            mutate_subdocument,
            upsert_specs=[{"path": "a", "value": 1}],
            expiry_seconds=5,
            preserve_expiry=True,
        )
        assert result["code"] == "invalid_expiry"
        collection.mutate_in.assert_not_called()


class TestErrorCodes:
    def test_negative_expiry_is_invalid_expiry(self) -> None:
        result, _ = _run(upsert_document_by_id, {"a": 1}, expiry_seconds=-1)
        assert result["code"] == "invalid_expiry"

    def test_unknown_level_is_invalid_durability_level(self) -> None:
        result, _ = _run(upsert_document_by_id, {"a": 1}, durability="x")
        assert result["code"] == "invalid_durability_level"

    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (DurabilityImpossibleException(), "durability_impossible"),
            (DurabilitySyncWriteAmbiguousException(), "sync_write_ambiguous"),
        ],
    )
    def test_sdk_durability_failures_get_stable_codes(self, exc, code) -> None:
        result, _ = _run(
            upsert_document_by_id,
            {"a": 1},
            durability="MAJORITY",
            configure=lambda c: setattr(c.upsert, "side_effect", exc),
        )
        assert result["success"] is False
        assert result["code"] == code
        assert "error" in result

    def test_other_failures_have_no_code(self) -> None:
        assert "code" not in kv_error(RuntimeError("boom"))
