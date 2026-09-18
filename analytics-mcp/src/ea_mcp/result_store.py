"""LRU-bounded on-disk store for large query results.

Why
---
When a result is too big to hand back inline, the tool returns a truncated
slice plus a ``result_id``. The full result lives here, and the client reads
it back through the ``ea://results/{result_id}`` MCP resource.

Two kinds of entry share one id namespace
-----------------------------------------
``DiskEntry``   -- a *sync* query's rows, written to disk as JSON Lines. These
                   consume the disk budget and are evicted LRU.
``HandleEntry`` -- an *async* query. Nothing is written: EA still holds the
                   result buffers, so we record only that the id refers to a
                   live async query and re-fetch from EA on read. Costs no
                   local disk, so it is exempt from the budget and from
                   eviction; its lifetime is EA's (it dies on discard/cancel).

For async, ``result_id`` *is* the ``query_handle`` token, so the model has one
id to track rather than two.

Scope / limitations
-------------------
Same single-process caveat as ``HandleRegistry``: the index is in memory, so a
restart forgets every entry. Files orphaned by a crash are reclaimed by
``sweep_orphans()`` at startup -- that sweep is *only* for orphans, never part
of steady-state eviction, which deletes files synchronously.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("ea-mcp-server.result_store")

RESULT_SUFFIX = ".jsonl"
META_SUFFIX = ".meta.json"


class UnknownResultError(KeyError):
    """Raised when a result_id is not in the store.

    Means the id is wrong, was evicted to make room, belonged to an async
    query whose results were discarded/cancelled, or was minted by a
    different server process (another replica, or before a restart).
    """


@dataclass
class DiskEntry:
    """A sync query's full result, saved as JSON Lines on disk."""

    result_id: str
    path: Path
    meta_path: Path
    size_bytes: int
    row_count: int
    statement: str

    kind: str = "disk"


@dataclass
class HandleEntry:
    """An async query's result, still held by EA. Nothing stored locally."""

    result_id: str  # identical to the query_handle token
    statement: str

    kind: str = "handle"


class ResultStore:
    """Thread-safe, LRU-bounded index of saved results.

    Tool handlers run on FastMCP's thread pool, so every mutation of the
    index and the byte counter happens under one lock.
    """

    def __init__(self, storage_path: Path, max_bytes: int) -> None:
        self._entries: OrderedDict[str, DiskEntry | HandleEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._storage_path = storage_path
        self._max_bytes = max_bytes
        self._disk_bytes = 0
        self._storage_path.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- writes

    def save_rows(
        self,
        rows: list[dict[str, Any]],
        statement: str,
        metadata: dict[str, Any] | None = None,
    ) -> DiskEntry | None:
        """Write rows to disk as JSON Lines and index them. Returns the entry.

        Returns None when the result cannot be stored -- currently only when a
        single result is larger than the whole budget. Evicting every other
        result to make room for one that still would not fit is worse than
        declining, so the caller falls back to truncate-only.
        """
        result_id = uuid.uuid4().hex
        path = self._storage_path / f"{result_id}{RESULT_SUFFIX}"
        meta_path = self._storage_path / f"{result_id}{META_SUFFIX}"

        # Stream row-by-row: never materializes a second copy of the payload.
        size_bytes = 0
        try:
            with path.open("w", encoding="utf-8") as fh:
                for row in rows:
                    line = json.dumps(row, default=str) + "\n"
                    fh.write(line)
                    size_bytes += len(line.encode("utf-8"))
        except Exception:
            path.unlink(missing_ok=True)
            raise

        if size_bytes > self._max_bytes:
            logger.warning(
                f"Result of {size_bytes} bytes exceeds the entire storage "
                f"budget of {self._max_bytes}; not saving."
            )
            path.unlink(missing_ok=True)
            return None

        meta = {
            "result_id": result_id,
            "statement": statement,
            "row_count": len(rows),
            "size_bytes": size_bytes,
            "metadata": metadata or {},
        }
        meta_path.write_text(json.dumps(meta, default=str), encoding="utf-8")

        entry = DiskEntry(
            result_id=result_id,
            path=path,
            meta_path=meta_path,
            size_bytes=size_bytes,
            row_count=len(rows),
            statement=statement,
        )
        with self._lock:
            self._evict_to_fit_locked(size_bytes)
            self._entries[result_id] = entry
            self._disk_bytes += size_bytes
        logger.info(
            f"Saved result {result_id} ({len(rows)} rows, {size_bytes} bytes); "
            f"store now {self._disk_bytes}/{self._max_bytes} bytes"
        )
        return entry

    def save_handle(self, query_handle: str, statement: str) -> HandleEntry:
        """Index an async query's result under its own query_handle token.

        Nothing is written to disk -- EA holds the buffers -- so this neither
        consumes nor triggers the disk budget.
        """
        entry = HandleEntry(result_id=query_handle, statement=statement)
        with self._lock:
            self._entries[query_handle] = entry
        logger.debug(f"Indexed async result {query_handle} (handle-backed)")
        return entry

    # ---------------------------------------------------------------- reads

    def get(self, result_id: str) -> DiskEntry | HandleEntry:
        """Return an entry and mark it most-recently-used."""
        with self._lock:
            entry = self._entries.get(result_id)
            if entry is None:
                raise UnknownResultError(
                    f"Unknown result_id '{result_id}'. It may be invalid, "
                    "evicted to make room for newer results, discarded on the "
                    "server, or created by a different server process."
                )
            self._entries.move_to_end(result_id)
            return entry

    def read_rows(
        self, result_id: str, offset: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Read a slice of a *disk* entry's rows.

        Handle-backed (async) entries are not read here: they need the live
        SDK handle, which lives in ``HandleRegistry``. The resource layer
        dispatches on ``entry.kind``.
        """
        entry = self.get(result_id)
        if not isinstance(entry, DiskEntry):
            raise UnknownResultError(
                f"Result '{result_id}' is handle-backed; read it via the "
                "handle registry, not from disk."
            )

        rows: list[dict[str, Any]] = []
        end = None if limit is None else offset + limit
        # Line-by-line so a huge file is never fully loaded to serve a slice.
        with entry.path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i < offset:
                    continue
                if end is not None and i >= end:
                    break
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def read_metadata(self, result_id: str) -> dict[str, Any]:
        """Return the saved sidecar metadata for a disk entry."""
        entry = self.get(result_id)
        if not isinstance(entry, DiskEntry):
            return {"result_id": result_id, "statement": entry.statement}
        return json.loads(entry.meta_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- eviction

    def _evict_to_fit_locked(self, incoming_bytes: int) -> None:
        """Delete least-recently-used disk entries until ``incoming_bytes`` fit.

        Caller must hold the lock. The file is unlinked *before* its index
        entry is dropped and the byte counter adjusted, so the store never
        claims space it has not actually reclaimed.

        Handle entries are skipped: they occupy no disk, so evicting them
        would free nothing while breaking a live async result.
        """
        while self._disk_bytes + incoming_bytes > self._max_bytes:
            victim_id = next(
                (
                    rid
                    for rid, e in self._entries.items()
                    if isinstance(e, DiskEntry)
                ),
                None,
            )
            if victim_id is None:
                return  # nothing left that frees disk
            victim = self._entries[victim_id]
            assert isinstance(victim, DiskEntry)
            victim.path.unlink(missing_ok=True)
            victim.meta_path.unlink(missing_ok=True)
            del self._entries[victim_id]
            self._disk_bytes -= victim.size_bytes
            logger.info(
                f"Evicted result {victim_id} ({victim.size_bytes} bytes) to "
                "stay within the storage budget"
            )

    def remove(self, result_id: str) -> None:
        """Drop an entry, deleting its files first. Idempotent."""
        with self._lock:
            entry = self._entries.pop(result_id, None)
            if isinstance(entry, DiskEntry):
                entry.path.unlink(missing_ok=True)
                entry.meta_path.unlink(missing_ok=True)
                self._disk_bytes -= entry.size_bytes

    def sweep_orphans(self) -> int:
        """Delete result files not present in the index. Returns the count.

        Only meaningful at startup: the index is in memory, so files left by a
        previous process are invisible to it and would otherwise leak forever.
        """
        removed = 0
        with self._lock:
            known = {
                p
                for e in self._entries.values()
                if isinstance(e, DiskEntry)
                for p in (e.path, e.meta_path)
            }
        for path in self._storage_path.iterdir():
            if not path.is_file():
                continue
            if path.name.endswith((RESULT_SUFFIX, META_SUFFIX)) and path not in known:
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info(f"Swept {removed} orphaned result file(s) at startup")
        return removed

    # ----------------------------------------------------------- diagnostics

    def stats(self) -> dict[str, Any]:
        """Current occupancy (diagnostic)."""
        with self._lock:
            return {
                "entries": len(self._entries),
                "disk_bytes": self._disk_bytes,
                "max_bytes": self._max_bytes,
                "storage_path": str(self._storage_path),
            }
