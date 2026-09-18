"""Unit tests for the saved-result resource reader.

Fakes the SDK handle chain (status -> result handle -> rows) so the async
re-fetch path is covered without a live EA cluster, the same approach
``test_async_query_tools_unit`` uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ea_mcp.handle_registry import HandleRegistry
from ea_mcp.resources import read_saved_result
from ea_mcp.result_store import ResultStore, UnknownResultError

ROWS = [{"i": i} for i in range(10)]


class _FakeResult:
    def get_all_rows(self) -> list[dict]:
        return ROWS


class _FakeResultHandle:
    def fetch_results(self) -> _FakeResult:
        return _FakeResult()


class _FakeStatus:
    def __init__(self, ready: bool) -> None:
        self._ready = ready

    def results_ready(self) -> bool:
        return self._ready

    def result_handle(self) -> _FakeResultHandle:
        return _FakeResultHandle()


class _FakeHandle:
    """Minimal stand-in for the SDK's BlockingQueryHandle."""

    def __init__(self, ready: bool = True) -> None:
        self._ready = ready

    def fetch_status(self) -> _FakeStatus:
        return _FakeStatus(self._ready)


def _parse(jsonl: str) -> list[dict]:
    return [json.loads(line) for line in jsonl.strip().split("\n") if line]


@pytest.fixture
def store(tmp_path: Path) -> ResultStore:
    return ResultStore(tmp_path, 10 * 1024 * 1024)


@pytest.fixture
def registry() -> HandleRegistry:
    return HandleRegistry()


class TestDiskBackedResults:
    def test_reads_the_whole_result_as_json_lines(
        self, store: ResultStore, registry: HandleRegistry
    ) -> None:
        entry = store.save_rows(ROWS, "SELECT 1")
        assert entry is not None

        out = read_saved_result(store, registry, entry.result_id)

        assert _parse(out) == ROWS

    def test_reads_a_slice(
        self, store: ResultStore, registry: HandleRegistry
    ) -> None:
        entry = store.save_rows(ROWS, "SELECT 1")
        assert entry is not None

        assert _parse(read_saved_result(store, registry, entry.result_id, 2, 3)) == (
            ROWS[2:5]
        )
        assert _parse(read_saved_result(store, registry, entry.result_id, 7)) == (
            ROWS[7:]
        )


class TestHandleBackedResults:
    def test_refetches_rows_from_the_server(
        self, store: ResultStore, registry: HandleRegistry
    ) -> None:
        token = registry.register(_FakeHandle(), "SELECT async")
        store.save_handle(token, "SELECT async")

        assert _parse(read_saved_result(store, registry, token)) == ROWS

    def test_slices_refetched_rows(
        self, store: ResultStore, registry: HandleRegistry
    ) -> None:
        token = registry.register(_FakeHandle(), "SELECT async")
        store.save_handle(token, "SELECT async")

        out = read_saved_result(store, registry, token, offset=7, limit=2)

        assert _parse(out) == ROWS[7:9]

    def test_says_so_when_the_query_is_still_running(
        self, store: ResultStore, registry: HandleRegistry
    ) -> None:
        token = registry.register(_FakeHandle(ready=False), "SELECT async")
        store.save_handle(token, "SELECT async")

        with pytest.raises(UnknownResultError, match="still"):
            read_saved_result(store, registry, token)

    def test_reports_a_discarded_query_distinctly_and_drops_the_entry(
        self, store: ResultStore, registry: HandleRegistry
    ) -> None:
        """After a discard EA has freed the rows, so the URI is permanently
        dead and should not be left looking live."""
        token = registry.register(_FakeHandle(), "SELECT async")
        store.save_handle(token, "SELECT async")
        registry.remove(token)  # what discard/cancel does

        with pytest.raises(UnknownResultError, match="discarded or cancelled"):
            read_saved_result(store, registry, token)

        with pytest.raises(UnknownResultError):
            store.get(token)


class TestInvalidRequests:
    def test_unknown_result_id_raises(
        self, store: ResultStore, registry: HandleRegistry
    ) -> None:
        with pytest.raises(UnknownResultError, match="Unknown result_id"):
            read_saved_result(store, registry, "nope")

    @pytest.mark.parametrize(
        ("offset", "limit"), [(-1, None), (0, 0), (0, -5)]
    )
    def test_rejects_nonsensical_paging(
        self,
        store: ResultStore,
        registry: HandleRegistry,
        offset: int,
        limit: int | None,
    ) -> None:
        entry = store.save_rows(ROWS, "SELECT 1")
        assert entry is not None

        with pytest.raises(ValueError):
            read_saved_result(store, registry, entry.result_id, offset, limit)
