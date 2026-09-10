"""Server-side store for large query results, with TTL + LRU cleanup.

Why this exists
---------------
``run_query_sync`` and ``get_async_query_results`` return every row they
fetched straight to the model. For a query matching 24k rows that is a
context-window problem, not a transport problem: the rows are already in
server memory, they just must not all be spoken aloud.

So both tools hand back only as much as fits a byte budget, park the whole
result set here, and let the model pull further windows through
``get_large_result``.

Budgeting by bytes, not rows
----------------------------
The cap is a *serialized byte* budget rather than a row count, because row
size varies enormously: measured against travel-sample, one airline row is
~148 bytes while one hotel row is ~3,565 — a 24x spread. A fixed row count
would send 1.5 KB of airlines or 36 KB of hotels under the same "10 rows"
label. A byte budget makes the cost predictable and lets narrow results send
far more rows for free.

Scope / limitations
-------------------
Entries live in one process's memory, attached to ``AppContext`` (never a
module global), so they do not survive a restart and are not visible to
another replica. Unlike the live SDK handles in ``HandleRegistry``, what is
stored here is plain JSON-safe data, so a real implementation could move it to
Redis or disk without changing the tool surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# Roughly the serialized size of 10 travel-sample `route` rows. Deliberately
# small for the POC so truncation is easy to demonstrate; a production value
# would be tuned to the model's context budget.
DEFAULT_MAX_RESPONSE_BYTES = 12_000

# Cleanup caps. Both are enforced lazily (see ResultStore._expire_locked): the
# store has no background sweeper, so an abandoned result is only reclaimed
# once another store operation happens.
DEFAULT_MAX_ENTRIES = 32
DEFAULT_MAX_TOTAL_ROWS = 500_000
DEFAULT_TTL_SECONDS = 30 * 60

# How often the background reaper wakes. Frequent enough that an abandoned
# result is reclaimed promptly, rare enough to be invisible: a sweep over a
# few dozen entries is microseconds.
DEFAULT_REAP_INTERVAL_SECONDS = 60


logger = logging.getLogger("ea-mcp-server.result_store")


class UnknownResultError(KeyError):
    """Raised when a result_id is not found in the store.

    Happens when the id is wrong, already released, expired, evicted to make
    room for newer results, or minted by a different server process.
    """


def measure(value: Any) -> int:
    """Serialized size of a value in bytes, as the client would receive it."""
    return len(json.dumps(value, default=str).encode())


def fit_rows(rows: list[Any], offset: int, max_bytes: int) -> tuple[list[Any], int]:
    """Take rows from ``offset`` while they fit in ``max_bytes``.

    Returns ``(rows_taken, bytes_used)``. Always returns at least one row when
    one exists, even if that row alone exceeds the budget: returning nothing
    would strand the caller with no way to make progress, and a single
    oversized row is better delivered with a warning than not at all.
    """
    taken: list[Any] = []
    used = 2  # the enclosing "[" and "]"
    for row in rows[offset:]:
        # +2 for the ", " separator that will precede this row in the array.
        size = measure(row) + (2 if taken else 0)
        if taken and used + size > max_bytes:
            break
        taken.append(row)
        used += size
    return taken, used


@dataclass
class StoredResult:
    """One buffered result set.

    ``result_id`` is the caller-facing token. For async queries it is
    deliberately the *same string* as the EA ``query_handle``, so a caller
    juggles one id rather than two; ``is_async`` records that, since releasing
    such an entry must also discard the EA-side query.
    """

    result_id: str
    statement: str
    rows: list[Any]
    is_async: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def row_count(self) -> int:
        return len(self.rows)


class ResultStore:
    """Thread-safe LRU map of result_id -> buffered rows, with a TTL.

    Tool handlers run in FastMCP's thread pool, so access is guarded by a
    ``threading.Lock``. Reads count as use: ``get`` moves an entry to the most
    recently used end, so a result the model is actively paging through is the
    last one evicted.
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

    def store(
        self,
        statement: str,
        rows: list[Any],
        result_id: str | None = None,
        is_async: bool = False,
    ) -> StoredResult:
        """Buffer a result set.

        ``result_id`` may be supplied to reuse an existing token — the async
        path passes its EA ``query_handle`` so one id covers both. Storing the
        same id twice replaces the earlier entry rather than duplicating it.
        """
        entry = StoredResult(
            result_id=result_id or uuid.uuid4().hex,
            statement=statement,
            rows=rows,
            is_async=is_async,
        )
        with self._lock:
            self._expire_locked()
            self._entries[entry.result_id] = entry
            self._entries.move_to_end(entry.result_id)
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

    def peek(self, result_id: str) -> StoredResult | None:
        """Return an entry without raising or refreshing its LRU position."""
        with self._lock:
            return self._entries.get(result_id)

    def release(self, result_id: str) -> bool:
        """Drop a buffered result set. Returns whether it was present."""
        with self._lock:
            return self._entries.pop(result_id, None) is not None

    def sweep(self) -> int:
        """Drop every expired entry now. Returns how many were removed.

        The eager counterpart to the lazy expiry that store/get/release
        already perform. Called on a timer by ``start_reaper`` so that memory
        is reclaimed even when no tool is being called — without it, an
        abandoned result stays resident until something next touches the
        store, which for an idle server is never.
        """
        with self._lock:
            before = len(self._entries)
            self._expire_locked()
            removed = before - len(self._entries)
        if removed:
            logger.info(f"Reaper freed {removed} expired result(s)")
        return removed

    def list_entries(self) -> list[StoredResult]:
        """Snapshot of live entries, least-recently-used first (diagnostic)."""
        with self._lock:
            self._expire_locked()
            return list(self._entries.values())

    # -- internals; every caller already holds the lock ---------------------

    def _expire_locked(self) -> None:
        """Drop entries older than the TTL."""
        if self._ttl_seconds <= 0:
            return
        cutoff = time.time() - self._ttl_seconds
        for result_id in [
            rid for rid, e in self._entries.items() if e.created_at < cutoff
        ]:
            del self._entries[result_id]

    def _evict_locked(self) -> None:
        """Evict least-recently-used entries until both caps are satisfied.

        The just-stored entry is most recently used, so it is evicted only if
        it alone exceeds the row cap — in which case keeping it would push out
        everything else for no benefit.
        """
        total_rows = sum(e.row_count for e in self._entries.values())
        while self._entries and (
            len(self._entries) > self._max_entries or total_rows > self._max_total_rows
        ):
            _, evicted = self._entries.popitem(last=False)
            total_rows -= evicted.row_count


@contextlib.asynccontextmanager
async def start_reaper(
    store: ResultStore, interval: float = DEFAULT_REAP_INTERVAL_SECONDS
):
    """Run a background task that expires stale results on a timer.

    Wrap the server lifespan in this so the TTL reclaims memory on its own
    rather than only when a tool happens to touch the store. The task is
    cancelled and awaited on exit, so shutdown does not leak it.

    ``store.sweep()`` is synchronous and lock-guarded; it runs on the event
    loop thread because it is fast (a dict scan over a few dozen entries) and
    never does I/O. If the store ever grows large enough for that to matter,
    move the call to a thread with ``asyncio.to_thread``.
    """

    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                store.sweep()
            except Exception:
                # A reaper that dies silently is worse than one that logs and
                # keeps going: the leak it was preventing would resume.
                logger.exception("Result store reaper failed; continuing")

    task = asyncio.create_task(_loop())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
