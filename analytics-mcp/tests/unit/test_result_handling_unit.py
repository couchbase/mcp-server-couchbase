"""Unit tests for the shared large-result decision logic.

Covers every row of the decision table in ``result_handling``, since that table
is the contract every query tool depends on.
"""

from __future__ import annotations

from pathlib import Path

from ea_mcp.result_config import ResultConfig
from ea_mcp.result_handling import (
    build_result_payload,
    measure_rows,
    result_uri,
    truncate_rows,
)
from ea_mcp.result_store import DiskEntry, HandleEntry, ResultStore

# ~7 KB encoded: comfortably over the small limits used below.
ROWS = [{"i": i, "pad": "x" * 50} for i in range(100)]
SMALL_LIMIT = 1_000
BIG_LIMIT = 10 * 1024 * 1024


def _store(tmp_path: Path) -> ResultStore:
    return ResultStore(tmp_path, 10 * 1024 * 1024)


def _payload(tmp_path: Path, *, save_flag: bool, opt_in: bool, limit: int, **kw):
    config = ResultConfig(save_large_results=save_flag, truncate_bytes=limit)
    return build_result_payload(
        ROWS,
        config=config,
        store=kw.pop("store", None) or _store(tmp_path),
        save_if_large=opt_in,
        statement="SELECT 1",
        **kw,
    )


class TestDecisionTable:
    def test_small_result_is_returned_whole_when_saving_is_off(
        self, tmp_path: Path
    ) -> None:
        p = _payload(tmp_path, save_flag=False, opt_in=False, limit=BIG_LIMIT)

        assert p == {"rows": ROWS, "row_count": 100, "truncated": False}

    def test_large_result_truncates_when_saving_is_off(self, tmp_path: Path) -> None:
        """The opt-in cannot override the server switch."""
        p = _payload(tmp_path, save_flag=False, opt_in=True, limit=SMALL_LIMIT)

        assert p["truncated"] is True
        assert p["total_row_count"] == 100
        assert p["row_count"] < 100
        assert "result_id" not in p

    def test_large_result_truncates_when_caller_does_not_opt_in(
        self, tmp_path: Path
    ) -> None:
        p = _payload(tmp_path, save_flag=True, opt_in=False, limit=SMALL_LIMIT)

        assert p["truncated"] is True
        assert "result_id" not in p

    def test_small_result_is_not_saved_even_when_opted_in(
        self, tmp_path: Path
    ) -> None:
        """Saving a result the caller already holds in full wastes disk."""
        store = _store(tmp_path)
        p = _payload(
            tmp_path, save_flag=True, opt_in=True, limit=BIG_LIMIT, store=store
        )

        assert p["truncated"] is False
        assert "result_id" not in p
        assert store.stats()["entries"] == 0

    def test_large_result_is_saved_when_enabled_and_opted_in(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        p = _payload(
            tmp_path, save_flag=True, opt_in=True, limit=SMALL_LIMIT, store=store
        )

        assert p["truncated"] is True
        assert p["total_row_count"] == 100
        assert p["resource_uri"] == result_uri(p["result_id"])
        # The whole result is recoverable, not just the truncated slice.
        assert store.read_rows(p["result_id"]) == ROWS


class TestAsyncSharesTheQueryHandleId:
    def test_result_id_is_the_supplied_query_handle(self, tmp_path: Path) -> None:
        """Async reuses its query_handle so the model tracks a single id."""
        store = _store(tmp_path)
        p = _payload(
            tmp_path,
            save_flag=True,
            opt_in=True,
            limit=SMALL_LIMIT,
            store=store,
            result_id="handle-abc",
        )

        assert p["result_id"] == "handle-abc"
        assert p["resource_uri"] == "ea://results/handle-abc"

    def test_async_is_indexed_as_a_handle_entry_and_writes_no_files(
        self, tmp_path: Path
    ) -> None:
        """EA still holds the rows, so nothing is written locally."""
        store = _store(tmp_path)
        _payload(
            tmp_path,
            save_flag=True,
            opt_in=True,
            limit=SMALL_LIMIT,
            store=store,
            result_id="handle-abc",
        )

        assert isinstance(store.get("handle-abc"), HandleEntry)
        assert list(tmp_path.iterdir()) == []

    def test_sync_is_indexed_as_a_disk_entry(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        p = _payload(
            tmp_path, save_flag=True, opt_in=True, limit=SMALL_LIMIT, store=store
        )

        assert isinstance(store.get(p["result_id"]), DiskEntry)


class TestTruncation:
    def test_keeps_whole_rows_within_the_byte_budget(self) -> None:
        """Cutting mid-row would hand back an unparseable JSON fragment."""
        kept, size = truncate_rows(ROWS, SMALL_LIMIT)

        assert size <= SMALL_LIMIT
        assert 0 < len(kept) < len(ROWS)
        assert all(set(row) == {"i", "pad"} for row in kept)
        assert kept == ROWS[: len(kept)]  # a prefix, not a sample

    def test_keeps_at_least_one_row_even_if_it_exceeds_the_budget(self) -> None:
        """Zero rows would say nothing about the shape of the data."""
        oversized = [{"pad": "y" * 5_000}]

        kept, _ = truncate_rows(oversized, 100)

        assert kept == oversized

    def test_empty_result_is_not_treated_as_truncated(self, tmp_path: Path) -> None:
        config = ResultConfig(save_large_results=True, truncate_bytes=SMALL_LIMIT)
        p = build_result_payload(
            [], config=config, store=_store(tmp_path), save_if_large=True, statement="q"
        )

        assert p == {"rows": [], "row_count": 0, "truncated": False}

    def test_measure_rows_matches_json_encoding(self) -> None:
        import json

        assert measure_rows(ROWS) == len(json.dumps(ROWS).encode("utf-8"))


class TestFailuresDegradeGracefully:
    def test_missing_store_falls_back_to_truncation(self, tmp_path: Path) -> None:
        config = ResultConfig(save_large_results=True, truncate_bytes=SMALL_LIMIT)

        p = build_result_payload(
            ROWS, config=config, store=None, save_if_large=True, statement="q"
        )

        assert p["truncated"] is True
        assert "result_id" not in p

    def test_a_storage_error_still_returns_the_truncated_rows(
        self, tmp_path: Path
    ) -> None:
        """A cache failure must not fail an otherwise successful query."""

        class BrokenStore(ResultStore):
            def save_rows(self, *a, **kw):  # type: ignore[override]
                raise OSError("disk full")

        config = ResultConfig(save_large_results=True, truncate_bytes=SMALL_LIMIT)
        p = build_result_payload(
            ROWS,
            config=config,
            store=BrokenStore(tmp_path, 1024),
            save_if_large=True,
            statement="q",
        )

        assert p["truncated"] is True
        assert "result_id" not in p
        assert "disk full" in p["message"]

    def test_a_result_too_big_for_the_budget_explains_itself(
        self, tmp_path: Path
    ) -> None:
        config = ResultConfig(save_large_results=True, truncate_bytes=SMALL_LIMIT)
        p = build_result_payload(
            ROWS,
            config=config,
            store=ResultStore(tmp_path, 100),  # smaller than the result
            save_if_large=True,
            statement="q",
        )

        assert p["truncated"] is True
        assert "result_id" not in p
        assert "storage budget" in p["message"]
