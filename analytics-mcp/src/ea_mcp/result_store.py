"""Server-side store for buffered query result sets (POC).

Why this exists
---------------
``run_query_sync`` returns every row it fetched straight to the model. For a
query that matches a million rows that is a context-window problem, not a
transport problem: the rows are already in server memory, they just must not
all be spoken aloud.

Both POC tools therefore keep the full row list *here*, hand the model a small
preview plus an opaque ``result_id``, and let the model pull more only if it
actually needs it — via an MCP resource (POC 1) or a paging tool (POC 2).

This is the same coat-check idea as ``HandleRegistry`` (see handle_registry.py)
and shares its single-process caveat: entries live in one process's memory,
attached to ``AppContext``, so they do not survive a restart and are not
visible to another replica. Unlike the handle registry, though, what is stored
is plain JSON-safe data, so a real implementation could move it to Redis, a
temp file, or an EA-side handle without changing the tool surface.

Eviction
--------
A pure LRU cap on entry *count* would let a handful of huge result sets pin
gigabytes, so the store bounds both the number of entries and an approximate
total row count, evicting oldest-first. Entries are also given a TTL so an
abandoned result set does not sit in memory for the life of the process.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# Defaults are POC-sized: enough for a demo over a large collection, small
# enough that a forgotten server does not grow without bound.
DEFAULT_MAX_ENTRIES = 32
DEFAULT_MAX_TOTAL_ROWS = 500_000
DEFAULT_TTL_SECONDS = 30 * 60


class UnknownResultError(KeyError):
    """Raised when a result_id is not found in the store.

    Happens when the id is wrong, already released, expired, evicted to make
    room for newer results, or minted by a different server process.
    """


@dataclass
class StoredResult:
    """One tracked result set plus what the paging tools need to describe it.

    An entry has two possible lifecycles:

    * Sync, and the async POC that reuses the existing EA handle tools: created
      already holding its rows, ``handle`` stays None.
    * The unified async POC (``run_query_poc_async``): created *empty* while the
      query is still running, holding the live EA ``QueryHandle`` instead. When
      the query finishes, ``attach_rows`` swaps in the fetched rows and clears
      the handle. Until then ``rows`` is None and ``row_count`` is 0 — the
      distinction that matters to callers is ``is_ready``, not an empty list,
      since a finished query can legitimately return zero rows.
    """

    result_id: str
    statement: str
    rows: list[Any] | None = None
    handle: Any = None  # live BlockingQueryHandle while the query runs
    created_at: float = field(default_factory=time.time)

    @property
    def is_ready(self) -> bool:
        """Whether rows have been fetched and buffered."""
        return self.rows is not None

    @property
    def row_count(self) -> int:
        """Buffered row count; 0 while the query is still running."""
        return len(self.rows) if self.rows is not None else 0

    def attach_rows(self, rows: list[Any]) -> None:
        """Swap a finished query's rows in, releasing the live handle."""
        self.rows = rows
        self.handle = None

    def page(self, offset: int, limit: int) -> list[Any]:
        """Return rows [offset, offset+limit), clamped to the result set.

        A negative offset would silently wrap to the tail of the list in
        Python, which is a confusing thing to hand a model, so it is clamped
        to 0 by the caller-facing tools before it gets here.
        """
        if self.rows is None:
            return []
        return self.rows[offset : offset + limit]


class ResultStore:
    """Thread-safe LRU map of opaque result_id -> buffered rows.

    Tool handlers run in FastMCP's thread pool, so access is guarded by a
    ``threading.Lock``. Reads count as use: ``get`` moves an entry to the most
    recently used end, so a result set the model is actively paging through is
    the last one to be evicted.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_total_rows: int = DEFAULT_MAX_TOTAL_ROWS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._entries: OrderedDict[str, StoredResult] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._max_total_rows = max_total_rows
        self._ttl_seconds = ttl_seconds

    def store(self, statement: str, rows: list[Any]) -> StoredResult:
        """Buffer a result set and return its entry (whose ``result_id`` is fresh)."""
        entry = StoredResult(
            result_id=uuid.uuid4().hex,
            statement=statement,
            rows=rows,
        )
        with self._lock:
            self._expire_locked()
            self._entries[entry.result_id] = entry
            self._evict_locked()
        return entry

    def store_pending(self, statement: str, handle: Any) -> StoredResult:
        """Track a still-running query by its live EA handle, with no rows yet.

        Used by the unified async POC so one id covers both the running and the
        ready phase. Call ``attach_rows`` on the returned entry once the query
        finishes.
        """
        entry = StoredResult(
            result_id=uuid.uuid4().hex,
            statement=statement,
            rows=None,
            handle=handle,
        )
        with self._lock:
            self._expire_locked()
            self._entries[entry.result_id] = entry
            self._evict_locked()
        return entry

    def get(self, result_id: str) -> StoredResult:
        """Return the entry for an id, or raise ``UnknownResultError``."""
        with self._lock:
            self._expire_locked()
            entry = self._entries.get(result_id)
            if entry is not None:
                self._entries.move_to_end(result_id)
        if entry is None:
            raise UnknownResultError(
                f"Unknown result_id '{result_id}'. It may be invalid, already "
                "released, expired, or created by a different server process. "
                "Re-run the query to get a new result_id."
            )
        return entry

    def release(self, result_id: str) -> bool:
        """Drop a buffered result set. Returns whether it was present."""
        with self._lock:
            return self._entries.pop(result_id, None) is not None

    def list_entries(self) -> list[StoredResult]:
        """Snapshot of live entries, oldest use first (diagnostic)."""
        with self._lock:
            self._expire_locked()
            return list(self._entries.values())

    # -- internals; all callers already hold the lock -----------------------

    def _expire_locked(self) -> None:
        """Drop entries older than the TTL.

        Pending entries are exempt: they hold a live EA handle, and a query
        that merely takes longer than the TTL to run must not have its only
        reference silently deleted (see ``_evict_locked``).
        """
        if self._ttl_seconds <= 0:
            return
        cutoff = time.time() - self._ttl_seconds
        for result_id in [
            rid
            for rid, e in self._entries.items()
            if e.created_at < cutoff and e.is_ready
        ]:
            del self._entries[result_id]

    def _evict_locked(self) -> None:
        """Evict least-recently-used entries until both caps are satisfied.

        The just-stored entry is the most recently used, so it is evicted only
        if it alone exceeds the row cap — in which case keeping it would push
        out everything else for no benefit.

        Pending entries (a still-running query, holding a live EA handle) are
        never evicted. Dropping one would strand the query on the EA server
        with no handle left to fetch or cancel it — the same leak
        ``cancel_async_query`` exists to avoid. They hold no rows, so they cost
        nothing against the row cap; only the entry cap can be pushed over by
        them, and exceeding it is the lesser problem.
        """
        total_rows = sum(e.row_count for e in self._entries.values())
        evictable = [rid for rid, e in self._entries.items() if e.is_ready]
        while evictable and (
            len(self._entries) > self._max_entries or total_rows > self._max_total_rows
        ):
            result_id = evictable.pop(0)
            total_rows -= self._entries.pop(result_id).row_count
