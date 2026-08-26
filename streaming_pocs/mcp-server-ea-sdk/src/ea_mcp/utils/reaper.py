"""Background sweeper that reclaims abandoned streaming cursors.

The SDK enforces ``query_timeout`` inside ``get_next_row()`` via a polled
state check, not a timer. A cursor the client never returns to therefore never
trips it, and would hold its socket, thread-pool slot, and parse-ahead buffer
until the process exits. This thread is what actually reclaims those.
"""

from __future__ import annotations

import logging
import threading

from .constants import MCP_SERVER_NAME
from .cursor_registry import CursorRegistry

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.reaper")


class CursorReaper:
    """Daemon thread calling :meth:`CursorRegistry.reap_idle` on an interval."""

    def __init__(
        self,
        registry: CursorRegistry,
        interval_seconds: float,
    ) -> None:
        self._registry = registry
        self._interval = interval_seconds
        # Doubles as the sleep mechanism: waiting on the event means shutdown
        # interrupts the sleep immediately instead of waiting out the interval.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="ea-cursor-reaper", daemon=True
        )
        self._thread.start()
        logger.info("Cursor reaper started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                reaped = self._registry.reap_idle()
                if reaped:
                    logger.info("Reaped %d idle cursor(s)", reaped)
            except Exception as e:  # noqa: BLE001
                # Never let a sweep failure kill the thread -- the next tick
                # should still get a chance to reclaim cursors.
                logger.warning("Cursor reap sweep failed: %s", e)
