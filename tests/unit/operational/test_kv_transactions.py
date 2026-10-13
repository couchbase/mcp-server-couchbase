"""Unit tests for run_kv_transaction.

CBSE-23805. Ported from the implementation running in our CRUD server.

The tests are built around the one guarantee the tool exists to give: all of it
commits or none of it does, and a caller can tell which happened without
guessing.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from couchbase.exceptions import DocumentNotFoundException

from cb_mcp.tools.operational.kv import (
    MAX_TRANSACTION_OPS,
    _apply_transaction_operation,
    run_kv_transaction,
)


def _ctx() -> tuple[SimpleNamespace, MagicMock]:
    cluster = MagicMock()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster_provider=SimpleNamespace(get_cluster=lambda c: cluster),
            )
        )
    )
    return ctx, cluster


def _conn(cluster: MagicMock):
    return patch(
        "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
    )


def _op(kind: str = "upsert", **over):
    op = {
        "op": kind,
        "bucket_name": "b",
        "scope_name": "s",
        "collection_name": "c",
        "document_id": "doc1",
    }
    if kind in ("insert", "upsert", "replace"):
        op["content"] = {"field": "value"}
    op.update(over)
    return op


def _commit(cluster: MagicMock, **fields):
    result = SimpleNamespace(
        transaction_id=fields.get("transaction_id", "txn-1"),
        unstaging_complete=fields.get("unstaging_complete", True),
    )
    cluster.transactions.run.return_value = result
    return result


class TestValidationHappensBeforeAnythingConnects:
    """A malformed batch should cost nothing: no connection, no rollback."""

    @pytest.mark.parametrize(
        "operations",
        [[], None, "not a list", [{"op": "upsert"}], [{"not": "an op"}]],
    )
    def test_malformed_input_is_refused(self, operations) -> None:
        ctx, cluster = _ctx()
        with _conn(cluster):
            result = run_kv_transaction(ctx, operations)
        assert result["success"] is False
        assert result["committed"] is False
        cluster.transactions.run.assert_not_called()

    def test_an_unsupported_op_names_the_index_and_the_valid_ops(self) -> None:
        ctx, cluster = _ctx()
        with _conn(cluster):
            result = run_kv_transaction(ctx, [_op(), _op("frobnicate")])
        assert "operations[1]" in result["error"]
        assert "insert, upsert, replace, remove" in result["error"]
        cluster.transactions.run.assert_not_called()

    def test_a_missing_keyspace_field_is_named(self) -> None:
        ctx, cluster = _ctx()
        with _conn(cluster):
            result = run_kv_transaction(ctx, [_op(scope_name="")])
        assert "scope_name" in result["error"]
        cluster.transactions.run.assert_not_called()

    def test_content_is_required_for_a_write(self) -> None:
        ctx, cluster = _ctx()
        bad = _op("replace")
        del bad["content"]
        with _conn(cluster):
            result = run_kv_transaction(ctx, [bad])
        assert "content" in result["error"]
        cluster.transactions.run.assert_not_called()

    def test_remove_does_not_need_content(self) -> None:
        ctx, cluster = _ctx()
        _commit(cluster)
        with _conn(cluster):
            result = run_kv_transaction(ctx, [_op("remove")])
        assert result["success"] is True

    def test_over_the_limit_is_refused_before_execution(self) -> None:
        ctx, cluster = _ctx()
        ops = [_op(document_id=f"d{i}") for i in range(MAX_TRANSACTION_OPS + 1)]
        with _conn(cluster):
            result = run_kv_transaction(ctx, ops)
        assert result["success"] is False
        assert str(MAX_TRANSACTION_OPS) in result["error"]
        cluster.transactions.run.assert_not_called()

    def test_exactly_the_limit_is_allowed(self) -> None:
        ctx, cluster = _ctx()
        _commit(cluster)
        ops = [_op(document_id=f"d{i}") for i in range(MAX_TRANSACTION_OPS)]
        with _conn(cluster):
            result = run_kv_transaction(ctx, ops)
        assert result["success"] is True


class TestCommit:
    def test_a_commit_is_reported_as_committed(self) -> None:
        ctx, cluster = _ctx()
        _commit(cluster, transaction_id="txn-42")
        with _conn(cluster):
            result = run_kv_transaction(ctx, [_op(), _op("remove", document_id="d2")])
        assert result["success"] is True
        assert result["committed"] is True
        assert result["operations_committed"] == 2
        assert result["transaction_id"] == "txn-42"

    def test_every_operation_runs_inside_one_transaction(self) -> None:
        """Not one transaction per operation, which would defeat the point."""
        ctx, cluster = _ctx()
        _commit(cluster)
        with _conn(cluster):
            run_kv_transaction(
                ctx, [_op(document_id="a"), _op(document_id="b"), _op(document_id="c")]
            )
        assert cluster.transactions.run.call_count == 1

    def test_the_operations_are_applied_to_the_transaction_context(self) -> None:
        ctx, cluster = _ctx()
        _commit(cluster)
        captured = {}

        def run(body, *args):
            txn = MagicMock()
            body(txn)
            captured["txn"] = txn
            return SimpleNamespace(transaction_id="t", unstaging_complete=True)

        cluster.transactions.run.side_effect = run
        with _conn(cluster):
            run_kv_transaction(ctx, [_op("insert", document_id="a")])
        captured["txn"].insert.assert_called_once()

    def test_replace_and_remove_get_inside_the_transaction_first(self) -> None:
        """The SDK's transaction API takes a TransactionGetResult, not a key.
        Doing the get for the caller is what makes a missing document fail the
        whole transaction rather than surface halfway through."""
        ctx, cluster = _ctx()
        captured = {}

        def run(body, *args):
            txn = MagicMock()
            body(txn)
            captured["txn"] = txn
            return SimpleNamespace(transaction_id="t", unstaging_complete=True)

        cluster.transactions.run.side_effect = run
        with _conn(cluster):
            run_kv_transaction(
                ctx, [_op("replace", document_id="a"), _op("remove", document_id="b")]
            )
        txn = captured["txn"]
        assert txn.get.call_count == 2
        txn.replace.assert_called_once()
        txn.remove.assert_called_once()

    def test_operations_may_span_collections(self) -> None:
        """The reason the keyspace travels per operation rather than per call."""
        ctx, cluster = _ctx()
        _commit(cluster)

        def run(body, *args):
            body(MagicMock())
            return SimpleNamespace(transaction_id="t", unstaging_complete=True)

        cluster.transactions.run.side_effect = run
        with _conn(cluster):
            result = run_kv_transaction(
                ctx,
                [
                    _op(bucket_name="b1", collection_name="orders"),
                    _op(bucket_name="b2", collection_name="audit", document_id="a1"),
                ],
            )
        assert result["success"] is True
        assert {c.args[0] for c in cluster.bucket.call_args_list} == {"b1", "b2"}


class TestRollback:
    def test_a_failure_says_nothing_was_committed(self) -> None:
        """The field a caller must not have to infer. Reading a rollback as
        'some of it may have landed' sends someone looking for damage that is
        not there."""
        ctx, cluster = _ctx()
        cluster.transactions.run.side_effect = RuntimeError("conflict")
        with _conn(cluster):
            result = run_kv_transaction(ctx, [_op(), _op(document_id="d2")])
        assert result["success"] is False
        assert result["committed"] is False
        assert "No operation was committed" in result["detail"]

    def test_the_underlying_error_type_is_preserved(self) -> None:
        ctx, cluster = _ctx()
        cluster.transactions.run.side_effect = RuntimeError("conflict")
        with _conn(cluster):
            result = run_kv_transaction(ctx, [_op()])
        assert "RuntimeError" in result["error"]


class TestTimeout:
    """TransactionOptions is an opaque SDK object, not a mapping, so these
    assert on how it was CONSTRUCTED rather than reaching inside it."""

    def test_a_timeout_is_passed_to_the_sdk(self) -> None:
        ctx, cluster = _ctx()
        _commit(cluster)
        with (
            _conn(cluster),
            patch("cb_mcp.tools.operational.kv.TransactionOptions") as options,
        ):
            run_kv_transaction(ctx, [_op()], timeout_seconds=30)
        options.assert_called_once_with(timeout=timedelta(seconds=30))
        assert len(cluster.transactions.run.call_args.args) == 2

    def test_no_timeout_leaves_the_sdk_default_alone(self) -> None:
        """No options object at all, rather than one carrying the SDK's own
        default restated here where it could drift."""
        ctx, cluster = _ctx()
        _commit(cluster)
        with (
            _conn(cluster),
            patch("cb_mcp.tools.operational.kv.TransactionOptions") as options,
        ):
            run_kv_transaction(ctx, [_op()])
        options.assert_not_called()
        assert len(cluster.transactions.run.call_args.args) == 1

    def test_a_nonsense_timeout_is_refused_before_execution(self) -> None:
        ctx, cluster = _ctx()
        with _conn(cluster):
            result = run_kv_transaction(ctx, [_op()], timeout_seconds=0)
        assert result["success"] is False
        cluster.transactions.run.assert_not_called()


class TestDocstringSteersTheModel:
    @staticmethod
    def _doc() -> str:
        return " ".join((run_kv_transaction.__doc__ or "").split())

    def test_it_says_when_to_prefer_the_single_document_tools(self) -> None:
        assert "prefer the ordinary single-document write tools" in self._doc()

    def test_it_says_a_rollback_wrote_nothing(self) -> None:
        assert "NOTHING was written" in self._doc()


class TestTransactionalUpsert:
    """The SDK's transaction context has no upsert, so the tool builds one.

    The context here is limited to the four methods the real one has. A plain
    MagicMock accepts any attribute, which is how a call to a method that does
    not exist once passed these tests and failed on a real cluster.
    """

    @staticmethod
    def _txn() -> MagicMock:
        return MagicMock(spec=["get", "insert", "replace", "remove"])

    def test_upsert_of_an_existing_document_replaces_it(self):
        txn = self._txn()
        existing = object()
        txn.get.return_value = existing
        _apply_transaction_operation(txn, MagicMock(), _op("upsert"))
        txn.replace.assert_called_once_with(existing, {"field": "value"})
        txn.insert.assert_not_called()

    def test_upsert_of_a_missing_document_inserts_it(self):
        txn = self._txn()
        txn.get.side_effect = DocumentNotFoundException()
        _apply_transaction_operation(txn, MagicMock(), _op("upsert"))
        txn.insert.assert_called_once()
        assert txn.insert.call_args.args[1:] == ("doc1", {"field": "value"})
        txn.replace.assert_not_called()

    def test_other_errors_on_the_get_are_not_swallowed(self):
        txn = self._txn()
        txn.get.side_effect = RuntimeError("cluster unreachable")
        with pytest.raises(RuntimeError):
            _apply_transaction_operation(txn, MagicMock(), _op("upsert"))
        txn.insert.assert_not_called()
