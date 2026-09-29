"""The audit sink: a per-process JSON-Lines file with size-based rotation.

Two properties drive this design.

**No shared-file rotation.** ``logging.handlers.RotatingFileHandler`` is not
multi-process safe, and the default stdio deployment is inherently
multi-process: every MCP client spawns its own server process, and they all
read the same ``CB_MCP_AUDIT_FILE`` from the environment. Sharing one rotating
file across them produces interleaved partial lines and rotation races that
silently destroy records. So each writer gets its **own** file, named
``audit.<host>.<pid>.log``. No locking is needed because no two processes ever
touch the same file, and it behaves identically on every platform. This matches
the PRD's own position that auditing is per-node and consolidation is the
operator's responsibility.

The host is part of the name, not just the pid, because the pid alone is not
unique across containers. This image's ``ENTRYPOINT`` is exec form, so the
server is **PID 1 in every container**: several containers sharing one mounted
volume would every one of them write ``audit.1.log`` — precisely the
interleaving this design exists to prevent, in the configuration the Docker
documentation recommends. Docker assigns each container a distinct hostname by
default, which restores uniqueness. Two containers explicitly given the same
hostname and the same volume will still collide; that is documented rather than
defended against.

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
import re
import socket
import threading
import time
from pathlib import Path

from ..utils.constants import LOGGER_NAMESPACE

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.audit.sink")

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

#: How long the writer blocks on an empty queue before re-checking whether the
#: sink has been closed. The sentinel normally wakes it immediately; this is the
#: fallback for the one case the sentinel cannot cover — a queue already full
#: when ``close`` runs, so the sentinel could not be posted.
_POLL_SECONDS = 0.2

_SENTINEL = object()


#: Characters kept from a hostname. Anything else — dots that would confuse the
#: suffix split, path separators, whitespace — collapses to a single dash.
_HOST_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _host_token(host: str | None = None) -> str:
    """A filename-safe short hostname, or ``unknown`` if it cannot be read."""
    try:
        raw = socket.gethostname() if host is None else host
    except Exception:  # pragma: no cover - defensive
        return "unknown"
    # Short form only: an FQDN would put dots in the middle of the filename.
    token = _HOST_UNSAFE.sub("-", (raw or "").split(".")[0]).strip("-")
    return token[:64] or "unknown"


def process_scoped_path(
    path: str | os.PathLike[str],
    pid: int | None = None,
    host: str | None = None,
) -> Path:
    """Insert the host and process id before the file extension.

    ``audit.log`` becomes ``audit.<host>.<pid>.log``; a path with no suffix
    becomes ``audit.<host>.<pid>``. Exposed separately so startup can log, and
    tests can assert, the exact file that will be written.

    The host is included because the pid is not unique across containers: this
    image's ENTRYPOINT is exec form, so the server is PID 1 in every container
    and a shared volume would otherwise give every container ``audit.1.log``.
    """
    resolved = Path(path)
    actual_pid = os.getpid() if pid is None else pid
    scope = f"{_host_token(host)}.{actual_pid}"
    if resolved.suffix:
        return resolved.with_name(f"{resolved.stem}.{scope}{resolved.suffix}")
    return resolved.with_name(f"{resolved.name}.{scope}")


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
            # Best-effort: on a full queue the sentinel cannot be posted, and
            # dropping a queued record to make room for it would lose an audit
            # line purely to shut down faster. The writer polls the closed flag
            # instead, so it still exits once it has drained what it has.
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
            try:
                item = self._queue.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                # Nothing left to write. Exit only once the sink is closed,
                # which is what lets the thread finish when ``close`` could not
                # post the sentinel into a full queue.
                if self._closed.is_set():
                    return
                continue
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

    def _reopen_if_closed(self) -> None:
        """Re-establish the stream if it is not open.

        Rotation closes the live file before reopening it, so a failure in
        between — a full disk, a revoked permission, an unmounted volume —
        leaves the sink holding a closed handle. Without this the sink would
        never write again even after the condition cleared, which is the worst
        possible failure mode for an audit log: silent and permanent. Recovery
        is attempted per record, so the first write after the disk frees up
        succeeds.
        """
        if self._stream is not None and not self._stream.closed:
            return
        if self._closed.is_set():
            # ``close`` joins the writer with a timeout and then closes the
            # stream, so a writer still draining past that timeout — the wedged
            # filesystem this timeout exists for — would otherwise reopen a file
            # nobody will close again, and append records dated after "server
            # stopped". Refuse, and let the caller count the record as dropped.
            raise ValueError("audit sink is closed")
        self._stream = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self._size = self.path.stat().st_size

    def _write_batch(self, lines: list[str]) -> None:
        for line in lines:
            try:
                self._reopen_if_closed()
                self._rotate_if_needed(len(line.encode("utf-8")))
                self._stream.write(line)
                self._size += len(line.encode("utf-8"))
                self._written += 1
            except (OSError, ValueError) as exc:
                # ValueError is "I/O operation on closed file": reachable when
                # ``close`` closes the stream while this thread is still
                # draining, and not an OSError subclass.
                self._errors += 1
                self._dropped += 1
                self._log_failure(f"failed to write audit record: {exc}")
        try:
            if not self._stream.closed:
                self._stream.flush()
        except (OSError, ValueError) as exc:
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
