"""Unit tests for the large-result POC query tools.

Mocks the cluster, but uses a real ``ResultStore``: the whole point of the POC
is what happens to rows after the query, so stubbing the store would test
nothing.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ea_mcp.result_store import ResultStore, UnknownResultError
from ea_mcp.tools.query_poc import (
    DEFAULT_RESOURCE_PAGE_ROWS,
    MAX_PAGE_ROWS,
    get_async_query_results_poc_resource,
    get_query_poc_async_results,
    read_query_poc_result_page_resource,
    read_query_poc_result_resource,
    release_query_poc_result,
    run_query_poc_async,
    run_query_poc_resource,
)


def _rows(n: int) -> list[dict[str, int]]:
    return [{"i": i} for i in range(n)]


def _make_ctx() -> tuple[SimpleNamespace, MagicMock, ResultStore]:
    cluster = MagicMock()
    store = ResultStore()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(cluster=cluster, result_store=store)
        )
    )
    return ctx, cluster, store


def _patched(cluster: MagicMock, store: ResultStore):
    """Patch both connection accessors used by the POC module."""
    return (
        patch("ea_mcp.tools.query_poc.get_cluster_connection", return_value=cluster),
        patch("ea_mcp.tools.query_poc.get_result_store", return_value=store),
    )


def _run(fn, ctx, cluster, store, *args, **kwargs):
    p1, p2 = _patched(cluster, store)
    with p1, p2:
        return fn(ctx, *args, **kwargs)


def _run_with_registry(fn, ctx, cluster, store, registry, *args, **kwargs):
    """Like ``_run``, but also patches the async handle registry (shape A)."""
    p1, p2 = _patched(cluster, store)
    with (
        p1,
        p2,
        patch("ea_mcp.tools.query_poc.get_handle_registry", return_value=registry),
    ):
        return fn(ctx, *args, **kwargs)


def _fake_handle(rows: list | None, ready: bool = True) -> MagicMock:
    """A stand-in EA QueryHandle whose status reports ready/not-ready."""
    handle = MagicMock()
    status = MagicMock()
    status.results_ready.return_value = ready
    if ready and rows is not None:
        status.result_handle.return_value.fetch_results.return_value.get_all_rows.return_value = rows
    handle.fetch_status.return_value = status
    return handle


class TestRunQueryPocResource:
    def test_truncates_and_links_resource(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = _rows(100)

        result = _run(run_query_poc_resource, ctx, cluster, store, "SELECT *")

        assert result["success"] is True
        assert result["truncated"] is True
        assert result["row_count"] == 100
        assert result["preview_row_count"] == 5
        assert result["rows"] == _rows(5)
        assert result["resource_uri"] == f"ea://query-results/{result['result_id']}"
        assert "truncated" in result["message"].lower()

    def test_small_result_is_not_truncated(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = _rows(3)

        result = _run(run_query_poc_resource, ctx, cluster, store, "SELECT *")

        assert result["truncated"] is False
        assert result["rows"] == _rows(3)
        assert result["preview_row_count"] == 3

    def test_resource_body_holds_every_row(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = _rows(100)

        result = _run(run_query_poc_resource, ctx, cluster, store, "SELECT *")
        body = json.loads(read_query_poc_result_resource(result["result_id"], store))

        assert body["row_count"] == 100
        assert body["rows"] == _rows(100)
        assert body["statement"] == "SELECT *"

    def test_resource_read_rejects_unknown_id(self) -> None:
        _, _, store = _make_ctx()
        with pytest.raises(UnknownResultError):
            read_query_poc_result_resource("nope", store)

    def test_returns_error_envelope_on_sdk_error(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.execute_query.side_effect = Exception("syntax error")

        result = _run(run_query_poc_resource, ctx, cluster, store, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }


class TestReleaseQueryPocResult:
    def test_release_frees_and_invalidates(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = _rows(10)
        rid = _run(run_query_poc_resource, ctx, cluster, store, "SELECT *")["result_id"]

        released = _run(release_query_poc_result, ctx, cluster, store, rid)

        assert released == {
            "success": True,
            "result_id": rid,
            "released": True,
            "cancelled": False,
            "message": "Result set freed.",
        }
        with pytest.raises(UnknownResultError):
            read_query_poc_result_resource(rid, store)

    def test_release_is_idempotent(self) -> None:
        ctx, cluster, store = _make_ctx()

        result = _run(release_query_poc_result, ctx, cluster, store, "nope")

        assert result["success"] is True
        assert result["released"] is False


class TestAsyncShapeA:
    """Async POC A: run_query_async unchanged, truncation at fetch time."""

    def _ready_registry(self, rows: list) -> MagicMock:
        registry = MagicMock()
        registry.get.return_value = SimpleNamespace(
            handle=_fake_handle(rows), statement="SELECT * FROM t"
        )
        return registry

    def test_resource_variant_truncates_and_links(self) -> None:
        ctx, cluster, store = _make_ctx()
        registry = self._ready_registry(_rows(100))

        result = _run_with_registry(
            get_async_query_results_poc_resource, ctx, cluster, store, registry, "qh-1"
        )

        assert result["ready"] is True
        assert result["truncated"] is True
        assert result["row_count"] == 100
        assert result["rows"] == _rows(5)
        assert result["query_handle"] == "qh-1"
        assert result["resource_uri"].startswith("ea://query-results/")

    def test_resource_variant_pages_via_the_paged_resource(self) -> None:
        ctx, cluster, store = _make_ctx()
        registry = self._ready_registry(_rows(100))

        started = _run_with_registry(
            get_async_query_results_poc_resource, ctx, cluster, store, registry, "qh-1"
        )
        page = json.loads(
            read_query_poc_result_page_resource(
                started["result_id"], store, offset=5, limit=10
            )
        )

        assert started["ready"] is True
        assert page["rows"] == _rows(15)[5:]

    def test_not_ready_returns_ready_false_without_rows(self) -> None:
        ctx, cluster, store = _make_ctx()
        registry = MagicMock()
        registry.get.return_value = SimpleNamespace(
            handle=_fake_handle(None, ready=False), statement="SELECT * FROM t"
        )

        result = _run_with_registry(
            get_async_query_results_poc_resource, ctx, cluster, store, registry, "qh-1"
        )
        assert result["ready"] is False
        assert "rows" not in result

    def test_uses_statement_from_the_handle_registry(self) -> None:
        ctx, cluster, store = _make_ctx()
        registry = self._ready_registry(_rows(10))

        result = _run_with_registry(
            get_async_query_results_poc_resource, ctx, cluster, store, registry, "qh-1"
        )
        body = json.loads(read_query_poc_result_resource(result["result_id"], store))

        assert body["statement"] == "SELECT * FROM t"

    def test_unknown_handle_returns_error_envelope(self) -> None:
        ctx, cluster, store = _make_ctx()
        registry = MagicMock()
        registry.get.side_effect = Exception("Unknown query_handle 'bogus'")

        result = _run_with_registry(
            get_async_query_results_poc_resource, ctx, cluster, store, registry, "bogus"
        )

        assert result["success"] is False
        assert result["query_handle"] == "bogus"


class TestAsyncShapeB:
    """Async POC B: one result_id spanning the running and ready phases."""

    def test_start_returns_pending_id_with_no_rows(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.start_query.return_value = _fake_handle(None, ready=False)

        result = _run(run_query_poc_async, ctx, cluster, store, "SELECT * FROM t")

        assert result["success"] is True
        assert result["ready"] is False
        assert "rows" not in result
        assert store.get(result["result_id"]).is_ready is False

    def test_status_reports_not_ready_while_running(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.start_query.return_value = _fake_handle(None, ready=False)
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        result = _run(get_query_poc_async_results, ctx, cluster, store, rid)

        assert result["ready"] is False
        assert "rows" not in result

    def test_same_id_serves_both_handoffs_once_ready(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.start_query.return_value = _fake_handle(_rows(100))
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        result = _run(get_query_poc_async_results, ctx, cluster, store, rid)

        assert result["ready"] is True
        assert result["result_id"] == rid  # same id as the start call
        assert result["rows"] == _rows(5)
        assert result["row_count"] == 100
        # Both mechanisms offered off one id.
        assert result["resource_uri"] == f"ea://query-results/{rid}"
        assert result["next_offset"] == 5

        body = json.loads(read_query_poc_result_resource(rid, store))
        page = json.loads(
            read_query_poc_result_page_resource(rid, store, offset=5, limit=10)
        )
        assert body["rows"] == _rows(100)
        assert page["rows"] == _rows(15)[5:]

    def test_handle_is_released_once_rows_are_buffered(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.start_query.return_value = _fake_handle(_rows(10))
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        _run(get_query_poc_async_results, ctx, cluster, store, rid)
        entry = store.get(rid)

        assert entry.is_ready is True
        assert entry.handle is None

    def test_repeat_status_calls_do_not_refetch(self) -> None:
        ctx, cluster, store = _make_ctx()
        handle = _fake_handle(_rows(10))
        cluster.start_query.return_value = handle
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        first = _run(get_query_poc_async_results, ctx, cluster, store, rid)
        second = _run(get_query_poc_async_results, ctx, cluster, store, rid)

        assert first["rows"] == second["rows"]
        # Only the first call touched EA; afterwards the rows are buffered.
        assert handle.fetch_status.call_count == 1

    def test_paged_resource_before_ready_says_still_running(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.start_query.return_value = _fake_handle(None, ready=False)
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        body = json.loads(read_query_poc_result_page_resource(rid, store))

        assert body["ready"] is False
        assert "rows" not in body
        assert "still running" in body["message"]

    def test_resource_before_ready_says_still_running(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.start_query.return_value = _fake_handle(None, ready=False)
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        body = json.loads(read_query_poc_result_resource(rid, store))

        assert body["ready"] is False
        assert "rows" not in body

    def test_release_cancels_a_running_query(self) -> None:
        ctx, cluster, store = _make_ctx()
        handle = _fake_handle(None, ready=False)
        cluster.start_query.return_value = handle
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        result = _run(release_query_poc_result, ctx, cluster, store, rid)

        assert result["released"] is True
        assert result["cancelled"] is True
        handle.cancel.assert_called_once()

    def test_release_discards_if_it_finished_before_cancel(self) -> None:
        ctx, cluster, store = _make_ctx()
        handle = _fake_handle(_rows(5))  # reports ready at release time
        cluster.start_query.return_value = handle
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        result = _run(release_query_poc_result, ctx, cluster, store, rid)

        assert result["cancelled"] is False
        handle.cancel.assert_not_called()
        handle.fetch_status.return_value.result_handle.return_value.discard_results.assert_called_once()

    def test_release_still_frees_locally_when_ea_call_fails(self) -> None:
        ctx, cluster, store = _make_ctx()
        handle = _fake_handle(None, ready=False)
        handle.cancel.side_effect = Exception("network down")
        cluster.start_query.return_value = handle
        rid = _run(run_query_poc_async, ctx, cluster, store, "SELECT *")["result_id"]

        result = _run(release_query_poc_result, ctx, cluster, store, rid)

        assert result["released"] is True
        with pytest.raises(UnknownResultError):
            store.get(rid)

    def test_start_failure_returns_error_envelope(self) -> None:
        ctx, cluster, store = _make_ctx()
        cluster.start_query.side_effect = Exception("syntax error")

        result = _run(run_query_poc_async, ctx, cluster, store, "SELECT bad(")

        assert result == {
            "success": False,
            "error": "syntax error",
            "statement": "SELECT bad(",
        }


class TestPendingEntriesSurviveEviction:
    """A pending entry holds the only reference to a live EA query."""

    def test_pending_entry_is_not_evicted_by_entry_cap(self) -> None:
        store = ResultStore(max_entries=1)
        pending = store.store_pending("slow query", MagicMock())
        store.store("q2", _rows(1))
        store.store("q3", _rows(1))

        assert store.get(pending.result_id).statement == "slow query"

    def test_pending_entry_is_not_expired_by_ttl(self) -> None:
        store = ResultStore(ttl_seconds=60)
        pending = store.store_pending("slow query", MagicMock())
        pending.created_at -= 120  # a query slower than the TTL

        assert store.get(pending.result_id).statement == "slow query"

    def test_pending_entry_becomes_evictable_once_ready(self) -> None:
        store = ResultStore(max_entries=1)
        pending = store.store_pending("q1", MagicMock())
        pending.attach_rows(_rows(1))
        store.store("q2", _rows(1))

        with pytest.raises(UnknownResultError):
            store.get(pending.result_id)


class TestResultStoreEviction:
    def test_entry_cap_evicts_oldest_first(self) -> None:
        store = ResultStore(max_entries=2)
        first = store.store("q1", _rows(1))
        second = store.store("q2", _rows(1))
        third = store.store("q3", _rows(1))

        with pytest.raises(UnknownResultError):
            store.get(first.result_id)
        assert store.get(second.result_id).statement == "q2"
        assert store.get(third.result_id).statement == "q3"

    def test_reading_an_entry_protects_it_from_eviction(self) -> None:
        store = ResultStore(max_entries=2)
        first = store.store("q1", _rows(1))
        store.store("q2", _rows(1))
        store.get(first.result_id)  # refreshes first as most-recently-used
        store.store("q3", _rows(1))

        assert store.get(first.result_id).statement == "q1"

    def test_row_cap_evicts_until_under_budget(self) -> None:
        store = ResultStore(max_entries=10, max_total_rows=100)
        old = store.store("q1", _rows(80))
        store.store("q2", _rows(80))

        with pytest.raises(UnknownResultError):
            store.get(old.result_id)

    def test_expired_entries_are_dropped(self) -> None:
        store = ResultStore(ttl_seconds=60)
        entry = store.store("q1", _rows(1))
        # Age the entry past the TTL rather than sleeping through it.
        entry.created_at -= 120

        with pytest.raises(UnknownResultError):
            store.get(entry.result_id)

    def test_ttl_of_zero_disables_expiry(self) -> None:
        store = ResultStore(ttl_seconds=0)
        entry = store.store("q1", _rows(1))
        entry.created_at -= 10_000

        assert store.get(entry.result_id).statement == "q1"


class TestPagedResultResource:
    """The paged resource must agree with fetch_query_poc_rows exactly."""

    def _stored(self, n: int = 1000):
        ctx, cluster, store = _make_ctx()
        cluster.execute_query.return_value.get_all_rows.return_value = _rows(n)
        rid = _run(run_query_poc_resource, ctx, cluster, store, "SELECT *")["result_id"]
        return ctx, cluster, store, rid

    def _page(self, store, rid, **kw):
        return json.loads(read_query_poc_result_page_resource(rid, store, **kw))

    def test_default_page_is_bounded(self) -> None:
        _, _, store, rid = self._stored()
        body = self._page(store, rid)
        assert body["offset"] == 0
        assert body["returned_row_count"] == DEFAULT_RESOURCE_PAGE_ROWS
        assert body["row_count"] == 1000
        assert body["has_more"] is True
        assert body["next_offset"] == DEFAULT_RESOURCE_PAGE_ROWS

    def test_explicit_window(self) -> None:
        _, _, store, rid = self._stored()
        body = self._page(store, rid, offset=100, limit=10)
        assert body["rows"] == _rows(110)[100:]
        assert body["next_offset"] == 110

    def test_page_matches_the_same_slice_of_the_whole_result(self) -> None:
        _, _, store, rid = self._stored()
        whole = json.loads(read_query_poc_result_resource(rid, store))
        page = self._page(store, rid, offset=42, limit=7)
        assert page["rows"] == whole["rows"][42:49]
        assert page["row_count"] == whole["row_count"]

    def test_negative_offset_clamped_and_limit_capped(self) -> None:
        _, _, store, rid = self._stored(MAX_PAGE_ROWS + 100)
        body = self._page(store, rid, offset=-5, limit=10_000)
        assert body["offset"] == 0
        assert body["returned_row_count"] == MAX_PAGE_ROWS

    def test_last_page_and_past_end(self) -> None:
        _, _, store, rid = self._stored(20)
        last = self._page(store, rid, offset=15, limit=50)
        past = self._page(store, rid, offset=999, limit=10)
        assert last["returned_row_count"] == 5
        assert last["has_more"] is False and last["next_offset"] is None
        assert past["rows"] == [] and past["has_more"] is False

    def test_pending_entry_reports_not_ready(self) -> None:
        store = ResultStore()
        entry = store.store_pending("slow", MagicMock())
        body = self._page(store, entry.result_id)
        assert body["ready"] is False
        assert "rows" not in body

    def test_unknown_id_raises(self) -> None:
        _, _, store = _make_ctx()
        with pytest.raises(UnknownResultError):
            read_query_poc_result_page_resource("nope", store)

    def test_walking_pages_reconstructs_full_result(self) -> None:
        _, _, store, rid = self._stored(1000)
        collected, offset = [], 0
        while offset is not None:
            body = self._page(store, rid, offset=offset, limit=300)
            collected.extend(body["rows"])
            offset = body["next_offset"]
        assert collected == _rows(1000)
