"""The audit sink: a per-process JSON-Lines file with size-based rotation.

Two properties drive this design.

**No shared-file rotation.** ``logging.handlers.RotatingFileHandler`` is not
multi-process safe, and the default stdio deployment is inherently
multi-process: every MCP client spawns its own server process, and they all
read the same ``CB_MCP_AUDIT_FILE`` from the environment. Sharing one rotating
file across them produces interleaved partial lines and rotation races that
silently destroy records. So each process writes its **own** file, with the
PID inserted before the extension (``audit.log`` → ``audit.12345.log``). No
locking is needed because no two processes ever touch the same file, and it
behaves identically on every platform. This matches the PRD's own position that
auditing is per-node and consolidation is the operator's responsibility.

**Never block the event loop.** ``emit`` only puts a formatted line on a
bounded queue; a dedicated daemon thread does the writing and flushing. A
synchronous write from async middleware would stall the whole server on every
tool call. When the queue is full, records are dropped and counted rather than
applying back-pressure to tool execution.

Runtime failures — disk full, permissions revoked, the file unmounted — are
reported loudly to the operational log as errors and counted, and the server
keeps serving. That is a deliberate product decision: audit unavailability
should not become an outage.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
import time
from pathlib import Path

from ..utils.constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.audit.sink")

#: Bounded queue depth. Deep enough to absorb a burst of tool calls while the
#: writer thread is mid-flush, shallow enough that a wedged disk cannot grow
#: memory without limit.
DEFAULT_QUEUE_SIZE = 10_000

#: Lines coalesced into a single write+flush by the writer thread.
_WRITE_BATCH = 64

#: Minimum seconds between repeated write-failure error records, so a full disk
#: cannot flood the operational log.
_ERROR_LOG_INTERVAL_SECONDS = 60.0

#: Seconds the writer thread is given to drain on close.
DEFAULT_CLOSE_TIMEOUT = 5.0

_SENTINEL = object()


def process_scoped_path(path: str | os.PathLike[str], pid: int | None = None) -> Path:
    """Insert the process id before the file extension.

    ``audit.log`` becomes ``audit.<pid>.log``; a path with no suffix becomes
    ``audit.<pid>``. Exposed separately so startup can log, and tests can
    assert, the exact file that will be written.
    """
    resolved = Path(path)
    actual_pid = os.getpid() if pid is None else pid
    if resolved.suffix:
        return resolved.with_name(f"{resolved.stem}.{actual_pid}{resolved.suffix}")
    return resolved.with_name(f"{resolved.name}.{actual_pid}")


class AuditSink:
    """Writes audit lines to a per-process file on a background thread."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int,
        backup_count: int,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        """Open the sink's file, creating parent directories as needed.

        Raises:
            OSError: if the file cannot be created or opened. The caller is
                expected to report this and continue without auditing rather
                than abort startup.
        """
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}.")
        if backup_count < 0:
            raise ValueError(f"backup_count must not be negative, got {backup_count}.")

        self.path = process_scoped_path(path)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

        # Counters are only mutated by the writer thread, except ``dropped``
        # which is incremented by the caller. Both are plain ints guarded by
        # the GIL for a single increment; exactness under contention is not
        # required for a diagnostic counter.
        self._written = 0
        self._dropped = 0
        self._errors = 0
        self._last_error_log = 0.0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered text append. Opened eagerly so a misconfigured path
        # fails at startup, where it can be reported, rather than on the first
        # audited operation.
        # Deliberately not a context manager: the stream is owned for the
        # lifetime of the sink and written by the background thread. Closing it
        # per record would defeat both the batching and the append semantics.
        self._stream = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self._size = self.path.stat().st_size

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the writer thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="cb-mcp-audit-writer", daemon=True
        )
        self._thread.start()

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT) -> None:
        """Drain the queue, stop the writer thread and close the file.

        Safe to call more than once. Best-effort: a wedged filesystem cannot be
        allowed to hang shutdown, so the drain is bounded by ``timeout``.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        if self._thread is not None:
            # A full queue means the writer is already behind; it will reach
            # the sentinel-free shutdown path via the join timeout below.
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(_SENTINEL)
            self._thread.join(timeout=timeout)
            self._thread = None
        try:
            if not self._stream.closed:
                self._stream.flush()
                self._stream.close()
        except OSError:  # pragma: no cover - nothing useful left to do
            pass

    # -- producer side ----------------------------------------------------

    def emit(self, line: str) -> None:
        """Queue one already-formatted line. Never raises, never blocks."""
        if self._closed.is_set():
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self._dropped += 1
            self._log_failure("audit queue is full")

    @property
    def stats(self) -> dict[str, int]:
        """Counters for ``get_server_configuration_status``.

        ``dropped`` is the number that matters: it is the only way an operator
        learns that audit records were lost.
        """
        return {
            "written": self._written,
            "dropped": self._dropped,
            "write_errors": self._errors,
        }

    # -- writer thread ----------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            batch = [item]
            # Coalesce whatever else is already waiting into one write+flush.
            while len(batch) < _WRITE_BATCH:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _SENTINEL:
                    self._write_batch(batch)
                    return
                batch.append(nxt)
            self._write_batch(batch)

    def _write_batch(self, lines: list[str]) -> None:
        for line in lines:
            try:
                self._rotate_if_needed(len(line.encode("utf-8")))
                self._stream.write(line)
                self._size += len(line.encode("utf-8"))
                self._written += 1
            except OSError as exc:
                self._errors += 1
                self._dropped += 1
                self._log_failure(f"failed to write audit record: {exc}")
        try:
            self._stream.flush()
        except OSError as exc:
            self._errors += 1
            self._log_failure(f"failed to flush audit file: {exc}")

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        """Roll the file over when the next line would exceed ``max_bytes``.

        A single line larger than ``max_bytes`` is still written rather than
        discarded — losing the record would be worse than briefly exceeding the
        cap — but it lands in a file of its own on the next rotation.
        """
        if self._size == 0 or self._size + incoming_bytes <= self._max_bytes:
            return
        self._stream.flush()
        self._stream.close()

        if self._backup_count == 0:
            # Retention of zero still means the live file stays bounded, so it
            # is truncated rather than allowed to grow without limit.
            self._stream = open(self.path, "w", encoding="utf-8")  # noqa: SIM115
            self._size = 0
            return

        # Shift existing backups down, oldest first, then move the live file
        # into slot 1. ``Path.replace`` overwrites atomically on POSIX and
        # Windows alike.
        oldest = self.path.with_name(f"{self.path.name}.{self._backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._stream = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self._size = 0

    def _log_failure(self, message: str) -> None:
        """Report a sink failure as an error, rate-limited.

        The first failure is always reported; subsequent ones at most once per
        interval, carrying the running totals so the operational log shows the
        scale of the gap without being flooded by it.
        """
        now = time.monotonic()
        if self._last_error_log and now - self._last_error_log < (
            _ERROR_LOG_INTERVAL_SECONDS
        ):
            return
        self._last_error_log = now
        logger.error(
            "Audit sink failure: %s (file=%s, dropped=%d, write_errors=%d). "
            "Audit records are being lost; the server continues to serve.",
            message,
            self.path,
            self._dropped,
            self._errors,
        )


__all__ = [
    "DEFAULT_CLOSE_TIMEOUT",
    "DEFAULT_QUEUE_SIZE",
    "AuditSink",
    "process_scoped_path",
]
