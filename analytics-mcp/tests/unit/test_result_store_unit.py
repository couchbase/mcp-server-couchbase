"""Unit tests for the LRU-bounded saved-result store.

Uses a real temp directory rather than a mocked filesystem: the eviction logic
is only correct if the files really are deleted and the byte accounting really
matches what is on disk, and a mock would happily agree with a wrong answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ea_mcp.result_store import DiskEntry, ResultStore, UnknownResultError


def _rows(n: int, tag: str = "a") -> list[dict]:
    return [{"i": i, "t": tag} for i in range(n)]


def _store(tmp_path: Path, max_bytes: int = 10 * 1024 * 1024) -> ResultStore:
    return ResultStore(tmp_path, max_bytes)


def _jsonl_bytes_on_disk(tmp_path: Path) -> int:
    return sum(p.stat().st_size for p in tmp_path.iterdir() if p.suffix == ".jsonl")


class TestSaveAndRead:
    def test_saves_rows_and_reads_them_back_unchanged(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        rows = _rows(10)

        entry = store.save_rows(rows, "SELECT 1")

        assert entry is not None
        assert entry.row_count == 10
        assert store.read_rows(entry.result_id) == rows

    def test_reads_a_slice_with_offset_and_limit(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        entry = store.save_rows(_rows(10), "SELECT 1")
        assert entry is not None

        assert store.read_rows(entry.result_id, offset=2, limit=3) == _rows(10)[2:5]
        assert store.read_rows(entry.result_id, offset=7) == _rows(10)[7:]
        assert store.read_rows(entry.result_id, limit=2) == _rows(10)[:2]

    def test_saves_metadata_alongside_the_rows(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        entry = store.save_rows(_rows(3), "SELECT 1", {"warnings": ["w"]})
        assert entry is not None

        meta = store.read_metadata(entry.result_id)

        assert meta["row_count"] == 3
        assert meta["statement"] == "SELECT 1"
        assert meta["metadata"] == {"warnings": ["w"]}

    def test_unknown_result_id_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        with pytest.raises(UnknownResultError):
            store.get("nope")


class TestLruEviction:
    def test_evicts_least_recently_used_not_first_inserted(
        self, tmp_path: Path
    ) -> None:
        """Touching an entry must save it from eviction.

        Budget fits exactly two results, so saving a third forces one out.
        'a' is inserted first but read before the third save, so insertion
        order alone would evict the wrong one.
        """
        store = _store(tmp_path, max_bytes=200)
        a = store.save_rows(_rows(5, "a"), "q a")
        b = store.save_rows(_rows(5, "b"), "q b")
        assert a is not None and b is not None

        store.get(a.result_id)  # 'a' becomes most-recently-used
        c = store.save_rows(_rows(5, "c"), "q c")
        assert c is not None

        assert store.get(a.result_id)
        assert store.get(c.result_id)
        with pytest.raises(UnknownResultError):
            store.get(b.result_id)

    def test_eviction_deletes_the_files_not_just_the_index_entry(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path, max_bytes=200)
        a = store.save_rows(_rows(5, "a"), "q a")
        b = store.save_rows(_rows(5, "b"), "q b")
        assert a is not None and b is not None
        store.save_rows(_rows(5, "c"), "q c")  # forces eviction of 'a'

        assert not a.path.exists()
        assert not a.meta_path.exists()
        assert store.stats()["disk_bytes"] == _jsonl_bytes_on_disk(tmp_path)

    def test_byte_accounting_tracks_actual_disk_usage(self, tmp_path: Path) -> None:
        store = _store(tmp_path, max_bytes=500)
        for i in range(6):  # more than the budget holds, forcing evictions
            store.save_rows(_rows(5, f"t{i}"), f"q {i}")

        assert store.stats()["disk_bytes"] <= 500
        assert store.stats()["disk_bytes"] == _jsonl_bytes_on_disk(tmp_path)

    def test_declines_a_result_larger_than_the_whole_budget(
        self, tmp_path: Path
    ) -> None:
        """Evicting everything for something that still would not fit is worse
        than declining, so the caller falls back to truncate-only."""
        store = _store(tmp_path, max_bytes=100)

        assert store.save_rows(_rows(500), "q big") is None
        assert list(tmp_path.iterdir()) == []  # no stray partial file


class TestHandleEntries:
    def test_handle_entries_consume_no_disk_budget(self, tmp_path: Path) -> None:
        store = _store(tmp_path, max_bytes=200)
        store.save_handle("token123", "SELECT async")

        assert store.stats()["disk_bytes"] == 0
        assert list(tmp_path.iterdir()) == []

    def test_handle_entries_survive_eviction_pressure(self, tmp_path: Path) -> None:
        """They free no disk, so evicting them would break a live async result
        while reclaiming nothing."""
        store = _store(tmp_path, max_bytes=200)
        store.save_handle("token123", "SELECT async")
        for i in range(5):
            store.save_rows(_rows(5, f"t{i}"), f"q {i}")

        assert store.get("token123")

    def test_reading_a_handle_entry_from_disk_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save_handle("token123", "SELECT async")

        with pytest.raises(UnknownResultError):
            store.read_rows("token123")


class TestRemoveAndSweep:
    def test_remove_deletes_files_and_frees_the_budget(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        entry = store.save_rows(_rows(5), "q")
        assert entry is not None

        store.remove(entry.result_id)

        assert not entry.path.exists()
        assert store.stats()["disk_bytes"] == 0
        with pytest.raises(UnknownResultError):
            store.get(entry.result_id)

    def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.remove("never-existed")  # must not raise

    def test_sweep_removes_orphans_but_keeps_indexed_files(
        self, tmp_path: Path
    ) -> None:
        """Orphans are files from a previous process: the in-memory index
        cannot know about them, so they would otherwise leak forever."""
        store = _store(tmp_path)
        entry = store.save_rows(_rows(3), "q")
        assert entry is not None
        (tmp_path / "ghost.jsonl").write_text("{}\n")
        (tmp_path / "ghost.meta.json").write_text("{}")

        removed = store.sweep_orphans()

        assert removed == 2
        assert entry.path.exists()
        assert isinstance(store.get(entry.result_id), DiskEntry)
