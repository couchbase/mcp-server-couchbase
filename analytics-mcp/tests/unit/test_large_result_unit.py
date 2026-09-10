"""Unit tests for byte-budgeted truncation and the shared large-result tools.

Mocks the cluster but uses a real ``ResultStore``: the point of the feature is
what happens to rows after the query, so stubbing the store would test nothing.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ea_mcp.result_store import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ResultStore,
    UnknownResultError,
    fit_rows,
    measure,
)
from ea_mcp.tools.large_result import (
    deliver_rows,
    get_large_result,
    release_buffered_rows,
    release_large_result,
)


def _rows(n: int, pad: int = 0) -> list[dict]:
    return [{"i": i, "pad": "x" * pad} for i in range(n)]


def _ctx() -> tuple[SimpleNamespace, ResultStore, MagicMock]:
    store, registry = ResultStore(), MagicMock()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster=MagicMock(), result_store=store, handle_registry=registry
            )
        )
    )
    return ctx, store, registry


def _run(fn, ctx, store, registry, *a, **kw):
    with (
        patch("ea_mcp.tools.large_result.get_result_store", return_value=store),
        patch("ea_mcp.tools.large_result.get_handle_registry", return_value=registry),
    ):
        return fn(ctx, *a, **kw)


class TestByteBudget:
    def test_measure_matches_serialized_size(self) -> None:
        rows = _rows(3)
        assert measure(rows) == len(json.dumps(rows).encode())

    def test_fit_rows_respects_budget(self) -> None:
        rows = _rows(100, pad=100)
        taken, used = fit_rows(rows, 0, 1000)
        assert 0 < len(taken) < 100
        assert used <= 1000

    def test_fit_rows_always_returns_at_least_one(self) -> None:
        # A single row larger than the whole budget must still come back:
        # returning nothing would leave the caller unable to progress.
        taken, _ = fit_rows(_rows(1, pad=5000), 0, 10)
        assert len(taken) == 1

    def test_fit_rows_honours_offset(self) -> None:
        taken, _ = fit_rows(_rows(50), 10, DEFAULT_MAX_RESPONSE_BYTES)
        assert taken[0]["i"] == 10

    def test_wide_rows_yield_fewer_than_narrow(self) -> None:
        narrow, _ = fit_rows(_rows(500, pad=0), 0, 5000)
        wide, _ = fit_rows(_rows(500, pad=500), 0, 5000)
        assert len(narrow) > len(wide)


class TestDeliverRows:
    def test_small_result_is_untouched_and_unstored(self) -> None:
        ctx, store, reg = _ctx()
        out = _run(deliver_rows, ctx, store, reg, _rows(3), "SELECT 1")
        assert out["truncated"] is False
        assert out["row_count"] == 3
        assert "result_id" not in out
        assert store.list_entries() == []  # nothing to clean up later

    def test_large_result_is_truncated_and_stored(self) -> None:
        ctx, store, reg = _ctx()
        out = _run(deliver_rows, ctx, store, reg, _rows(5000), "SELECT *")
        assert out["truncated"] is True
        assert out["row_count"] == 5000
        assert out["returned_row_count"] < 5000
        assert measure(out["rows"]) <= DEFAULT_MAX_RESPONSE_BYTES
        assert out["next_offset"] == out["returned_row_count"]
        assert "get_large_result" in out["message"]
        assert len(store.list_entries()) == 1

    def test_extra_fields_are_merged(self) -> None:
        ctx, store, reg = _ctx()
        out = _run(
            deliver_rows, ctx, store, reg, _rows(3), "S", ready=True, query_handle="h"
        )
        assert out["ready"] is True and out["query_handle"] == "h"

    def test_async_reuses_the_handle_id(self) -> None:
        ctx, store, reg = _ctx()
        out = _run(
            deliver_rows,
            ctx,
            store,
            reg,
            _rows(5000),
            "S",
            result_id="handle-123",
            is_async=True,
        )
        assert out["result_id"] == "handle-123"
        assert store.get("handle-123").is_async is True


class TestGetLargeResult:
    def _stored(self, n=5000, pad=0):
        ctx, store, reg = _ctx()
        out = _run(deliver_rows, ctx, store, reg, _rows(n, pad), "SELECT *")
        return ctx, store, reg, out["result_id"]

    def test_serves_requested_window(self) -> None:
        ctx, store, reg, rid = self._stored()
        out = _run(get_large_result, ctx, store, reg, rid, 100, 5)
        assert out["offset"] == 100
        assert out["returned_row_count"] == 5
        assert out["rows"][0]["i"] == 100
        assert out["has_more"] is True
        assert out["next_offset"] == 105

    def test_oversized_num_rows_is_size_capped(self) -> None:
        ctx, store, reg, rid = self._stored()
        out = _run(get_large_result, ctx, store, reg, rid, 0, 99999)
        assert out["truncated"] is True
        assert out["returned_row_count"] < 99999
        assert measure(out["rows"]) <= DEFAULT_MAX_RESPONSE_BYTES

    def test_negative_offset_clamped_not_wrapped(self) -> None:
        ctx, store, reg, rid = self._stored()
        out = _run(get_large_result, ctx, store, reg, rid, -5, 3)
        assert out["offset"] == 0
        assert out["rows"][0]["i"] == 0

    def test_last_page_and_past_end(self) -> None:
        ctx, store, reg, rid = self._stored(n=5000)
        last = _run(get_large_result, ctx, store, reg, rid, 4998, 50)
        past = _run(get_large_result, ctx, store, reg, rid, 99999, 5)
        assert last["returned_row_count"] == 2
        assert last["has_more"] is False and last["next_offset"] is None
        assert past["rows"] == [] and past["has_more"] is False

    def test_unknown_id_returns_error_envelope(self) -> None:
        ctx, store, reg = _ctx()
        out = _run(get_large_result, ctx, store, reg, "nope", 0, 5)
        assert out["success"] is False and out["result_id"] == "nope"

    def test_walking_pages_reconstructs_full_result(self) -> None:
        ctx, store, reg, rid = self._stored(n=2000)
        collected, offset = [], 0
        while offset is not None:
            out = _run(get_large_result, ctx, store, reg, rid, offset, 500)
            collected.extend(out["rows"])
            offset = out["next_offset"]
        assert collected == _rows(2000)


class TestRelease:
    def test_release_sync_result(self) -> None:
        ctx, store, reg = _ctx()
        rid = _run(deliver_rows, ctx, store, reg, _rows(5000), "S")["result_id"]
        out = _run(release_large_result, ctx, store, reg, rid)
        assert out["released"] is True
        assert out["async_query_discarded"] is False
        with pytest.raises(UnknownResultError):
            store.get(rid)

    def test_release_is_idempotent(self) -> None:
        ctx, store, reg = _ctx()
        out = _run(release_large_result, ctx, store, reg, "nope")
        assert out["success"] is True and out["released"] is False

    def test_release_async_also_discards_ea_side(self) -> None:
        ctx, store, reg = _ctx()
        handle = MagicMock()
        handle.fetch_status.return_value.results_ready.return_value = True
        reg.get.return_value = SimpleNamespace(handle=handle, statement="S")
        _run(
            deliver_rows,
            ctx,
            store,
            reg,
            _rows(5000),
            "S",
            result_id="h1",
            is_async=True,
        )

        out = _run(release_large_result, ctx, store, reg, "h1")

        assert out["released"] is True and out["async_query_discarded"] is True
        handle.fetch_status.return_value.result_handle.return_value.discard_results.assert_called_once()
        reg.remove.assert_called_once_with("h1")

    def test_release_async_cancels_if_still_running(self) -> None:
        ctx, store, reg = _ctx()
        handle = MagicMock()
        handle.fetch_status.return_value.results_ready.return_value = False
        reg.get.return_value = SimpleNamespace(handle=handle, statement="S")
        _run(
            deliver_rows,
            ctx,
            store,
            reg,
            _rows(5000),
            "S",
            result_id="h2",
            is_async=True,
        )

        out = _run(release_large_result, ctx, store, reg, "h2")

        assert out["async_query_discarded"] is True
        handle.cancel.assert_called_once()

    def test_release_still_frees_locally_when_ea_fails(self) -> None:
        ctx, store, reg = _ctx()
        handle = MagicMock()
        handle.fetch_status.side_effect = Exception("network down")
        reg.get.return_value = SimpleNamespace(handle=handle, statement="S")
        _run(
            deliver_rows,
            ctx,
            store,
            reg,
            _rows(5000),
            "S",
            result_id="h3",
            is_async=True,
        )

        out = _run(release_large_result, ctx, store, reg, "h3")

        assert out["released"] is True
        assert out["async_query_discarded"] is False
        with pytest.raises(UnknownResultError):
            store.get("h3")

    def test_release_buffered_rows_is_the_reverse_direction(self) -> None:
        ctx, store, reg = _ctx()
        _run(
            deliver_rows,
            ctx,
            store,
            reg,
            _rows(5000),
            "S",
            result_id="h4",
            is_async=True,
        )
        with patch("ea_mcp.tools.large_result.get_result_store", return_value=store):
            assert release_buffered_rows(ctx, "h4") is True
            assert release_buffered_rows(ctx, "h4") is False


class TestStoreCleanup:
    def test_ttl_expires_entries(self) -> None:
        store = ResultStore(ttl_seconds=60)
        e = store.store("q", _rows(1))
        e.created_at -= 120  # age it past the TTL
        with pytest.raises(UnknownResultError):
            store.get(e.result_id)

    def test_ttl_of_zero_disables_expiry(self) -> None:
        store = ResultStore(ttl_seconds=0)
        e = store.store("q", _rows(1))
        e.created_at -= 10_000
        assert store.get(e.result_id).statement == "q"

    def test_lru_entry_cap(self) -> None:
        store = ResultStore(max_entries=2)
        a = store.store("q1", _rows(1))
        store.store("q2", _rows(1))
        store.store("q3", _rows(1))
        with pytest.raises(UnknownResultError):
            store.get(a.result_id)

    def test_reading_protects_from_eviction(self) -> None:
        store = ResultStore(max_entries=2)
        a = store.store("q1", _rows(1))
        store.store("q2", _rows(1))
        store.get(a.result_id)  # refresh as most-recently-used
        store.store("q3", _rows(1))
        assert store.get(a.result_id).statement == "q1"

    def test_row_cap_evicts_until_under_budget(self) -> None:
        store = ResultStore(max_entries=10, max_total_rows=100)
        old = store.store("q1", _rows(80))
        store.store("q2", _rows(80))
        with pytest.raises(UnknownResultError):
            store.get(old.result_id)

    def test_storing_same_id_replaces_not_duplicates(self) -> None:
        store = ResultStore()
        store.store("q1", _rows(1), result_id="same")
        store.store("q2", _rows(2), result_id="same")
        assert len(store.list_entries()) == 1
        assert store.get("same").row_count == 2

    def test_peek_does_not_raise_or_refresh(self) -> None:
        store = ResultStore()
        assert store.peek("missing") is None
        e = store.store("q", _rows(1))
        assert store.peek(e.result_id).statement == "q"

    def test_expiry_is_lazy_not_scheduled(self) -> None:
        # Documents real behaviour: nothing reclaims memory until the store is
        # touched again. A background sweeper would change this.
        store = ResultStore(ttl_seconds=0.01)
        e = store.store("q", _rows(1))
        time.sleep(0.05)
        assert store._entries  # still resident, no sweeper ran
        with pytest.raises(UnknownResultError):
            store.get(e.result_id)  # only now is it dropped
        assert store._entries == {}


class TestEagerExpiry:
    """The reaper must reclaim without anything touching the store."""

    def test_sweep_returns_count_removed(self) -> None:
        store = ResultStore(ttl_seconds=60)
        a = store.store("q1", _rows(1))
        store.store("q2", _rows(1))
        a.created_at -= 120  # only this one is stale
        assert store.sweep() == 1
        assert len(store.list_entries()) == 1

    def test_sweep_is_a_noop_when_nothing_expired(self) -> None:
        store = ResultStore(ttl_seconds=60)
        store.store("q", _rows(1))
        assert store.sweep() == 0

    @pytest.mark.asyncio
    async def test_reaper_expires_without_store_access(self) -> None:
        import asyncio

        from ea_mcp.result_store import start_reaper

        store = ResultStore(ttl_seconds=0.2)
        async with start_reaper(store, interval=0.1):
            store.store("q", _rows(10))
            assert len(store._entries) == 1
            await asyncio.sleep(0.5)  # nothing touches the store here
            assert len(store._entries) == 0

    @pytest.mark.asyncio
    async def test_reaper_is_cancelled_on_exit(self) -> None:
        from ea_mcp.result_store import start_reaper

        store = ResultStore()
        async with start_reaper(store, interval=0.05) as task:
            pass
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_reaper_survives_a_sweep_failure(self) -> None:
        # A reaper that dies on one bad sweep would silently stop reclaiming.
        import asyncio

        from ea_mcp.result_store import start_reaper

        store = ResultStore(ttl_seconds=0.1)
        calls = {"n": 0}
        real_sweep = store.sweep

        def flaky() -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real_sweep()

        store.sweep = flaky  # type: ignore[method-assign]
        async with start_reaper(store, interval=0.05):
            store.store("q", _rows(5))
            await asyncio.sleep(0.35)
        assert calls["n"] > 1  # kept going after the failure
        assert len(store._entries) == 0
