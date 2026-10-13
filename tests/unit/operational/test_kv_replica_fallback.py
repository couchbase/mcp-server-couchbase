"""Unit tests for the replica fallback inside get_document_by_id.

CBSE-23736. The shape under test adds no tools: the replica read folds into
the existing read tool as an opt-in parameter.

The tests are written around the failure this exists to prevent, which is an
agent reading an ambiguous failure as absence and then inserting over a
document that was merely unreachable.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from couchbase.exceptions import (
    DocumentNotFoundException,
    DocumentUnretrievableException,
    UnAmbiguousTimeoutException,
)

from cb_mcp.tools.operational.kv import (
    REPLICA_FALLBACK_TIMEOUT_SECONDS,
    get_document_by_id,
)


def _make_ctx_with_collection() -> tuple[SimpleNamespace, MagicMock, MagicMock]:
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


def _patch_connection(cluster: MagicMock):
    return patch(
        "cb_mcp.tools.operational.kv.get_cluster_connection", return_value=cluster
    )


class TestDefaultBehaviourIsUnchanged:
    """With the parameter left alone, this tool behaves exactly as it does today.

    This is the backwards-compatibility requirement in CONTRIBUTING principle 7,
    asserted rather than asserted-in-prose.
    """

    def test_returns_the_bare_document(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.return_value = SimpleNamespace(
            content_as={dict: {"field": "value"}}
        )

        with _patch_connection(cluster):
            result = get_document_by_id(ctx, "b", "s", "c", "doc1")

        assert result == {"field": "value"}
        assert "is_replica" not in result

    def test_not_found_still_raises(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = DocumentNotFoundException()

        with _patch_connection(cluster), pytest.raises(DocumentNotFoundException):
            get_document_by_id(ctx, "b", "s", "c", "doc1")

    def test_a_timeout_still_raises_and_no_replica_is_touched(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = UnAmbiguousTimeoutException()

        with _patch_connection(cluster), pytest.raises(UnAmbiguousTimeoutException):
            get_document_by_id(ctx, "b", "s", "c", "doc1")

        collection.get_any_replica.assert_not_called()


class TestFallbackDoesNotFireOnNotFound:
    """The single most important behaviour in this change.

    A DocumentNotFoundException from the active node is an authoritative answer.
    Falling back on it would turn a correct "no" into a replica round trip and,
    worse, into a possibly stale "yes".
    """

    def test_not_found_raises_even_with_fallback_enabled(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = DocumentNotFoundException()

        with _patch_connection(cluster), pytest.raises(DocumentNotFoundException):
            get_document_by_id(ctx, "b", "s", "c", "doc1", allow_replica_fallback=True)

        collection.get_any_replica.assert_not_called()


class TestFallbackOnSuccessfulActiveRead:
    def test_active_read_is_marked_as_not_a_replica(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.return_value = SimpleNamespace(
            content_as={dict: {"field": "value"}}
        )

        with _patch_connection(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", "doc1", allow_replica_fallback=True
            )

        assert result["content"] == {"field": "value"}
        assert result["is_replica"] is False

    def test_a_healthy_read_never_reaches_a_replica(self) -> None:
        """No new steady-state load on the cluster. This is the whole answer to
        the concern about flooding a cluster that is already sensitive."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.return_value = SimpleNamespace(content_as={dict: {}})

        with _patch_connection(cluster):
            get_document_by_id(ctx, "b", "s", "c", "doc1", allow_replica_fallback=True)

        collection.get_any_replica.assert_not_called()


class TestFallbackOnUnavailability:
    def test_a_replica_answer_is_returned_and_flagged(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = UnAmbiguousTimeoutException()
        collection.get_any_replica.return_value = SimpleNamespace(
            content_as={dict: {"field": "older value"}}, is_replica=True
        )

        with _patch_connection(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", "doc1", allow_replica_fallback=True
            )

        assert result["content"] == {"field": "older value"}
        assert result["is_replica"] is True

    def test_the_caller_is_told_not_to_write_from_it(self) -> None:
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = UnAmbiguousTimeoutException()
        collection.get_any_replica.return_value = SimpleNamespace(
            content_as={dict: {}}, is_replica=True
        )

        with _patch_connection(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", "doc1", allow_replica_fallback=True
            )

        assert "not use it as the basis for a write" in result["stale_read_warning"]

    def test_no_cas_is_handed_back_from_a_replica(self) -> None:
        """A replica CAS belongs to the replica's version of the document.
        Returning it as a write guard is worse than returning nothing, because
        it looks authoritative."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = UnAmbiguousTimeoutException()
        collection.get_any_replica.return_value = SimpleNamespace(
            content_as={dict: {}}, is_replica=True, cas=12345
        )

        with _patch_connection(cluster):
            result = get_document_by_id(
                ctx, "b", "s", "c", "doc1", allow_replica_fallback=True
            )

        assert "cas" not in result

    def test_the_replica_read_is_bounded(self) -> None:
        """A failing read must not sit for the full failover window."""
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = UnAmbiguousTimeoutException()
        collection.get_any_replica.return_value = SimpleNamespace(
            content_as={dict: {}}, is_replica=True
        )

        with _patch_connection(cluster):
            get_document_by_id(ctx, "b", "s", "c", "doc1", allow_replica_fallback=True)

        _, kwargs = collection.get_any_replica.call_args
        assert kwargs["timeout"] == timedelta(seconds=REPLICA_FALLBACK_TIMEOUT_SECONDS)


class TestInconclusiveResult:
    """When nothing answers, the result must not be readable as absence."""

    def _run_inconclusive(self, num_replicas: int | None = 2):
        ctx, cluster, collection = _make_ctx_with_collection()
        collection.get.side_effect = UnAmbiguousTimeoutException()
        collection.get_any_replica.side_effect = DocumentUnretrievableException()
        if num_replicas is None:
            cluster.buckets.return_value.get_bucket.side_effect = RuntimeError("down")
        else:
            cluster.buckets.return_value.get_bucket.return_value = SimpleNamespace(
                num_replicas=num_replicas
            )
        with _patch_connection(cluster):
            return get_document_by_id(
                ctx, "b", "s", "c", "doc1", allow_replica_fallback=True
            )

    def test_it_is_reported_as_a_failure_not_an_empty_answer(self) -> None:
        result = self._run_inconclusive()
        assert result["success"] is False

    def test_absence_is_explicitly_not_established(self) -> None:
        """The field a model would have to ignore in order to get this wrong."""
        result = self._run_inconclusive()
        assert result["absence_established"] is False

    def test_the_message_says_so_in_words_as_well(self) -> None:
        result = self._run_inconclusive()
        assert "does NOT establish that the document is absent" in result["error"]
        assert "Do not insert or upsert" in result["error"]

    def test_is_replica_is_none_rather_than_false(self) -> None:
        """False would claim an authoritative read happened. Nothing was read."""
        result = self._run_inconclusive()
        assert result["is_replica"] is None

    def test_the_configured_replica_count_is_reported(self) -> None:
        """The difference between 'nothing came back' and 'nothing came back,
        and two replicas should have been there'."""
        result = self._run_inconclusive(num_replicas=2)
        assert result["configured_replicas"] == 2

    def test_both_underlying_errors_are_preserved_by_type(self) -> None:
        """A bare SDK exception stringifies to nothing, so the type name is the
        part that carries the information. Asserting on str() alone would have
        passed on an empty string."""
        result = self._run_inconclusive()
        assert "UnAmbiguousTimeoutException" in result["active_error"]
        assert "DocumentUnretrievableException" in result["replica_error"]

    def test_an_unreadable_replica_count_degrades_rather_than_failing(self) -> None:
        """On a cluster that is already unwell the management call may fail too.
        That costs one fact, not the whole result."""
        result = self._run_inconclusive(num_replicas=None)
        assert result["success"] is False
        assert result["configured_replicas"] is None
        assert result["absence_established"] is False


class TestDocstringCarriesTheWarning:
    """The docstring is what the model actually reads.

    Whitespace is normalised before matching so that reflowing a line does not
    break a test about meaning.
    """

    @staticmethod
    def _doc() -> str:
        return " ".join((get_document_by_id.__doc__ or "").split())

    def test_it_says_when_not_to_use_the_fallback(self) -> None:
        assert "availability matters more than freshness" in self._doc()

    def test_it_forbids_writing_from_a_replica_read(self) -> None:
        assert "MUST NOT be used as the basis for a write" in self._doc()
