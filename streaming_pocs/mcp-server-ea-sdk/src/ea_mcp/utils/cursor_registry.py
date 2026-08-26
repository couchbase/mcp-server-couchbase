"""Server-side registry for live streaming query cursors.

Why this exists
---------------
Each MCP tool call is a separate function invocation, and the MCP client can
only carry a *string* between calls (in the tool JSON args). But streaming a
result set works through a live ``BlockingQueryResult`` / ``BlockingIterator``
pair that wraps an open HTTP response plus a background parse thread. Those
objects cannot be serialized (they hold locks and a live socket) and therefore
cannot be handed to the client or to a different process.

So we keep the *live objects* in this server-side registry, keyed by an opaque
UUID token, and return only the token to the client. Later tool calls send the
token back and we look the live iterator up, resuming exactly where the last
call stopped. The token is a coat-check ticket; the server holds the coat.

Cursors are forward-only
------------------------
``BlockingIterator.__next__`` pulls the next row off the open socket and the
SDK retains no history, so a cursor cannot be rewound. Rows already served are
gone; re-reading them means re-running the query. This is what keeps a cursor's
memory bounded.

What a paused cursor actually holds
-----------------------------------
Not just one batch. The SDK parses ahead on a background thread into a queue
bounded by ``JsonStreamConfig.buffered_row_max`` (100 rows, backpressure at
75%), on top of a 64KB HTTP byte buffer. So an idle cursor costs roughly
"100 rows + 64KB + one socket + one thread-pool slot" regardless of the
``batch_size`` the caller asked for.

Why the reaper is not optional
------------------------------
The SDK frees a stream's socket when iteration ends: the final row raises
``StopIteration``, which sets metadata and runs ``close()``. It also enforces
``query_timeout`` -- but only inside ``get_next_row()``, via a polled state
check rather than a timer. An abandoned cursor never calls ``get_next_row()``
again, so nothing ever trips that check and the socket would be held until the
process exits. :meth:`reap_idle` is what actually reclaims those.

Scope / limitations
-------------------
The registry lives in one process's memory (created once at lifespan startup
and attached to ``AppContext`` -- never a module global). That makes it correct
for stdio (always a single process) and HTTP with a single replica. It is NOT
sufficient for multi-replica HTTP or restart-survival: a token minted on
replica A is unknown to replica B, and a restart wipes the map. Streaming is
inherently connection-bound, so unlike the async-handle POC there is no
stateless backend that could replace it -- a resumable cursor would need the
query re-run with an OFFSET.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.cursor_registry")


class UnknownCursorError(KeyError):
    """Raised when a cursor token is not found in the registry.

    Happens when the token is wrong, the stream was already exhausted or
    closed, it was reaped for being idle, or it was minted by a different
    server process (e.g. another replica, or before a restart).
    """


class TooManyOpenStreamsError(RuntimeError):
    """Raised when opening another cursor would exceed the configured cap."""


@dataclass
class _Entry:
    """One live streaming cursor."""

    result: Any  # BlockingQueryResult -- kept for .cancel() / .metadata()
    iterator: Any  # BlockingIterator over that result
    statement: str
    rows_served: int = 0
    done: bool = False
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)


class CursorRegistry:
    """Thread-safe map of opaque token -> live streaming cursor.

    Tool handlers run in FastMCP's thread pool, so access is guarded by a
    ``threading.Lock``.
    """

    def __init__(
        self,
        max_open_streams: int,
        idle_ttl_seconds: float,
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._max_open_streams = max_open_streams
        self._idle_ttl_seconds = idle_ttl_seconds

    def register(self, result: Any, iterator: Any, statement: str) -> str:
        """Store a live cursor and return a fresh opaque token for it.

        Raises:
            TooManyOpenStreamsError: If the open-cursor cap is already reached.
        """
        token = uuid.uuid4().hex
        with self._lock:
            if len(self._entries) >= self._max_open_streams:
                raise TooManyOpenStreamsError(
                    f"Too many open query streams ({len(self._entries)}/"
                    f"{self._max_open_streams}). Close one with "
                    "close_query_stream, or finish reading it, before opening "
                    "another. Use list_query_streams to see what is open."
                )
            self._entries[token] = _Entry(
                result=result, iterator=iterator, statement=statement
            )
        return token

    def get(self, token: str) -> _Entry:
        """Return the entry for a token, or raise ``UnknownCursorError``."""
        with self._lock:
            entry = self._entries.get(token)
        if entry is None:
            raise UnknownCursorError(
                f"Unknown cursor '{token}'. It may be invalid, already "
                "exhausted or closed, reaped after being idle, or created by "
                "a different server process."
            )
        return entry

    def touch(self, token: str, rows_served: int, done: bool) -> None:
        """Record progress for a cursor after a batch was served."""
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                entry.rows_served = rows_served
                entry.done = done
                entry.last_used_at = time.monotonic()

    def remove(self, token: str) -> None:
        """Evict a token without touching the stream. Idempotent."""
        with self._lock:
            self._entries.pop(token, None)

    def close(self, token: str) -> _Entry:
        """Cancel a cursor's stream and evict it.

        Returns the evicted entry so callers can report rows served. Raises
        ``UnknownCursorError`` if the token is not tracked.
        """
        with self._lock:
            entry = self._entries.pop(token, None)
        if entry is None:
            raise UnknownCursorError(
                f"Unknown cursor '{token}'. It may already be closed."
            )
        _safe_cancel(entry, token)
        return entry

    def count(self) -> int:
        """Number of currently open cursors (diagnostic)."""
        with self._lock:
            return len(self._entries)

    def list_entries(self) -> list[dict[str, Any]]:
        """Return a summary of every open cursor.

        Lets a caller recover a cursor token it has lost (e.g. the LLM dropped
        one out of its context) along with how far that cursor has read.
        """
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "cursor": token,
                    "statement": entry.statement,
                    "rows_served": entry.rows_served,
                    "done": entry.done,
                    "age_seconds": round(now - entry.created_at, 1),
                    "idle_seconds": round(now - entry.last_used_at, 1),
                }
                for token, entry in self._entries.items()
            ]

    def reap_idle(self) -> int:
        """Cancel and evict cursors idle longer than the TTL.

        This is the only mechanism that reclaims a cursor nobody returns to:
        the SDK's own ``query_timeout`` is checked inside ``get_next_row()``,
        which an abandoned cursor never calls again.

        Returns:
            The number of cursors reaped.
        """
        cutoff = time.monotonic() - self._idle_ttl_seconds
        with self._lock:
            stale = [
                (token, entry)
                for token, entry in self._entries.items()
                if entry.last_used_at < cutoff
            ]
            for token, _ in stale:
                self._entries.pop(token, None)

        for token, entry in stale:
            logger.info(
                "Reaping idle cursor (token=%s, rows_served=%d, idle>%ss)",
                token,
                entry.rows_served,
                self._idle_ttl_seconds,
            )
            _safe_cancel(entry, token)
        return len(stale)

    def close_all(self) -> int:
        """Cancel and evict every cursor. Used at lifespan shutdown.

        Without this, server exit would drop open sockets on the floor rather
        than telling Enterprise Analytics the streams are finished.
        """
        with self._lock:
            entries = list(self._entries.items())
            self._entries.clear()
        for token, entry in entries:
            _safe_cancel(entry, token)
        return len(entries)


def _safe_cancel(entry: _Entry, token: str) -> None:
    """Cancel a stream, swallowing errors.

    An exhausted stream is already closed by the SDK, so cancelling it is a
    no-op that may raise; either way the entry is gone from the registry and
    there is nothing the caller can do about a failure here.
    """
    try:
        entry.result.cancel()
    except Exception as e:  # noqa: BLE001
        logger.debug("Error cancelling stream for cursor %s: %s", token, e)
