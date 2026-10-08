"""The audit sinks: a per-process JSON-Lines file, and stderr.

Three properties drive the file sink's design.

**No shared-file rotation.** ``logging.handlers.RotatingFileHandler`` is not
multi-process safe, and the default stdio deployment is inherently
multi-process: every MCP client spawns its own server process, and they all
read the same ``CB_MCP_AUDIT_LOG_FILE_PATH`` from the environment. Sharing one
rotating file across them produces interleaved partial lines and rotation races
that silently destroy records. So each writer gets its **own** file, named
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

**Two independent rotation triggers.** Size and age are separate switches,
either of which may be off (``0``). The stdlib cannot do both — ``Rotating-``
and ``TimedRotatingFileHandler`` are alternatives, which is exactly what the
PRD discussion concluded — but this sink owns its own rotation, so honouring
both is a two-line condition rather than a library swap. Whichever trigger fires
first rotates the file; with both off the file simply grows, which is the PRD's
"store them all" case.

**Never block the event loop.** ``emit`` only puts a formatted line on a
bounded queue; a dedicated daemon thread does the writing, rotating, flushing
and compressing. A synchronous write from async middleware would stall the whole
server on every tool call. When the queue is full, records are dropped and
counted rather than applying back-pressure to tool execution.

Runtime failures — disk full, permissions revoked, the file unmounted — are
reported loudly to the operational log as errors and counted, and the server
keeps serving. That is a deliberate product decision: audit unavailability
should not become an outage.
"""

from __future__ import annotations

import contextlib
import glob
import gzip
import logging
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Protocol

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
#: sink has been closed, and whether an interval rotation has fallen due. The
#: sentinel normally wakes it immediately; this is the fallback for the one case
#: the sentinel cannot cover — a queue already full when ``close`` runs, so the
#: sentinel could not be posted — and it is also what lets a completely idle
#: server still roll its file over at the configured interval.
_POLL_SECONDS = 0.2

_SENTINEL = object()

#: Timestamp appended to a rotated file. Compact ISO 8601 basic format in UTC,
#: to the microsecond: filename-safe on every platform and fixed-width. The
#: sub-second part is not decoration — a small size cap rotates many times a
#: second, and a second-resolution stamp would make those backups
#: indistinguishable.
_BACKUP_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"

#: Matches the suffix this sink appends to a rotated file: the timestamp, an
#: optional ``-N`` disambiguator, and an optional ``.gz``. Used both to find
#: *this* sink's backups when pruning — so an unrelated file sitting beside
#: them is never deleted — and to read their age back out, which is what orders
#: them for retention.
_BACKUP_SUFFIX = re.compile(r"\.(?P<stamp>\d{8}T\d{12}Z)(?:-(?P<index>\d+))?(?:\.gz)?$")


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


class AuditSinkProtocol(Protocol):
    """What the emitter needs from a sink, whatever it writes to.

    Each method below is bodied by its docstring alone. An ellipsis would be
    an expression statement with no effect, which static analysis flags — and
    rightly, since nothing distinguishes a deliberate protocol stub from a
    line someone left unfinished. The docstring is a real body, says what an
    implementation owes the emitter, and leaves no empty statement behind.

    Not ``runtime_checkable``, matching :mod:`cb_mcp.core.contracts`: such a
    protocol checks method *names* only, never signatures, so an ``isinstance``
    against it would be more misleading than useful. Nothing branches on a
    sink's type — ``init_audit`` builds the set it was configured to build.
    """

    def start(self) -> None:
        """Begin accepting records. Idempotent; may be a no-op."""

    def emit(self, line: str) -> None:
        """Write one already-formatted JSON line. Must never raise or block."""

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT) -> None:
        """Flush and stop, within ``timeout`` seconds. Safe to call twice."""

    @property
    def stats(self) -> dict[str, int]:
        """``written`` / ``dropped`` / ``write_errors`` for the status tool."""


class _QueuedSink:
    """Producer/consumer plumbing for a sink that must never block its caller.

    ``emit`` only puts a line on a bounded queue; a dedicated daemon thread does
    the writing. **Every** destination needs this, not just the file: the caller
    is the asyncio event loop, and a synchronous write there — to a disk, a pipe
    or a terminal — stalls every in-flight tool call and the JSON-RPC reader
    with it. When the queue is full, records are dropped and counted rather than
    applying back-pressure to tool execution. That is the deliberate trade:
    audit unavailability must not become an outage.

    Subclasses implement :meth:`_write_batch`, and may override :meth:`_on_idle`
    (work to do while no records are arriving) and :meth:`_on_close` (releasing
    whatever the subclass owns).
    """

    #: Writer thread name, per sink, so a stack dump says which destination is
    #: stuck.
    _THREAD_NAME = "cb-mcp-audit-writer"

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
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
        self._last_producer_error_log = 0.0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the writer thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=self._THREAD_NAME, daemon=True
        )
        self._thread.start()

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT) -> None:
        """Drain the queue, stop the writer thread, release the destination.

        Safe to call more than once. Best-effort: a wedged destination cannot be
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
        else:
            # Never started. Records queued before ``start`` would otherwise be
            # discarded in silence, with neither ``written`` nor ``dropped``
            # accounting for them.
            self._drain_remaining()

        abandoned = self._queue.qsize()
        if abandoned:
            # The join timed out on a wedged destination. Those records are
            # gone; say so, because an audit gap nobody can see is the one
            # outcome this sink exists to avoid.
            self._dropped += abandoned
            self._log_failure(
                f"{abandoned} audit record(s) still queued when the writer was "
                f"given up on after {timeout:g}s"
            )
        self._on_close()

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
            self._log_failure("audit queue is full", producer=True)

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
                self._on_idle()
                continue
            if item is _SENTINEL:
                self._drain_remaining()
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
                    self._drain_remaining()
                    return
                batch.append(nxt)
            self._write_batch(batch)

    def _drain_remaining(self) -> None:
        """Write anything queued behind the sentinel before the writer exits.

        ``emit`` checks the closed flag and then queues; ``close`` sets the flag
        and then posts the sentinel. A record that passes the check just before
        the flag is set lands *behind* the sentinel, and without this drain the
        writer would return without it — unwritten and uncounted, which is the
        one outcome an audit sink must not produce quietly.

        It narrows that window rather than closing it: a record queued after the
        final ``get_nowait`` below is still lost, and still uncounted. Closing it
        completely would mean holding a lock across ``emit``, which is what the
        whole queue design exists to avoid.
        """
        leftovers: list[str] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _SENTINEL:
                leftovers.append(item)
        if leftovers:
            self._write_batch(leftovers)

    # -- subclass hooks ---------------------------------------------------

    def _write_batch(self, lines: list[str]) -> None:
        raise NotImplementedError

    def _on_idle(self) -> None:
        """Called on the writer thread whenever the queue is empty."""

    def _on_close(self) -> None:
        """Release whatever the subclass owns, once the writer has stopped."""

    def _describe(self) -> str:
        """Short identifier for failure reports."""
        return type(self).__name__

    def _log_failure(self, message: str, *, producer: bool = False) -> None:
        """Report a sink failure as an error, rate-limited.

        The first failure is always reported; subsequent ones at most once per
        interval, carrying the running totals so the operational log shows the
        scale of the gap without being flooded by it.

        The producer side (a full queue) and the writer side (a failed write,
        rotation or compression) are rate-limited **separately**. They are
        different faults with different fixes, and sharing one timer meant a
        burst of queue-full reports could hide a disk failure for a minute.
        """
        now = time.monotonic()
        last = self._last_producer_error_log if producer else self._last_error_log
        if last and now - last < _ERROR_LOG_INTERVAL_SECONDS:
            return
        if producer:
            self._last_producer_error_log = now
        else:
            self._last_error_log = now
        logger.error(
            "Audit sink failure: %s (sink=%s, dropped=%d, write_errors=%d). "
            "Audit records are being lost; the server continues to serve.",
            message,
            self._describe(),
            self._dropped,
            self._errors,
        )


class ConsoleAuditSink(_QueuedSink):
    """Writes audit lines to stderr, on a background thread.

    **stderr, never stdout.** Under the stdio transport stdout *is* the JSON-RPC
    channel: one audit line written there corrupts the client's stream and takes
    the session down. The PRD calls this sink "console"; the stream is stderr,
    and that is not configurable.

    **Queued, like the file sink.** This wrote inline once, reasoning that a
    console is a pipe or a terminal rather than a disk that can fill. A pipe is
    exactly what fills: under stdio, stderr belongs to the MCP client, and a
    client that does not drain it blocks the writer at the 64 KiB pipe buffer —
    which, written inline, is the whole event loop. Since ``console`` is also
    the *default* sink, that made enabling auditing alone enough to hang the
    server. The queue bounds the damage to dropped records, which are counted.
    """

    _THREAD_NAME = "cb-mcp-audit-console"

    def __init__(
        self, stream: IO[str] | None = None, *, queue_size: int = DEFAULT_QUEUE_SIZE
    ) -> None:
        super().__init__(queue_size=queue_size)
        self._stream = stream

    def _describe(self) -> str:
        return "console"

    def _write_batch(self, lines: list[str]) -> None:
        # Resolved per batch rather than held: pytest's capsys, and a host that
        # redirects output, both replace ``sys.stderr`` after construction.
        stream = self._stream if self._stream is not None else sys.stderr
        buffered = 0
        try:
            for line in lines:
                stream.write(line)
                buffered += 1
            stream.flush()
        except (OSError, ValueError) as exc:
            # Every line in this batch is lost, not just the ones that had not
            # been handed to ``write`` yet: a buffered line that is never
            # flushed never reaches the console. ``dropped`` is documented as
            # the only way an operator learns of a gap, so it must account for
            # all of them.
            self._errors += 1
            self._dropped += len(lines)
            self._log_failure(f"failed to write to the console: {exc}")
            return
        self._written += buffered


class CompositeAuditSink:
    """Fans every record out to several sinks.

    One failing sink must not stop the others: each is given the line
    independently, and no sink's ``emit`` is allowed to raise anyway. Stats are
    summed, so an operator reading ``get_server_configuration_status`` sees the
    total records lost across destinations rather than having to add up a list.
    """

    def __init__(self, sinks: list[AuditSinkProtocol]) -> None:
        self._sinks = list(sinks)

    @property
    def sinks(self) -> tuple[AuditSinkProtocol, ...]:
        return tuple(self._sinks)

    def start(self) -> None:
        for sink in self._sinks:
            sink.start()

    def emit(self, line: str) -> None:
        for sink in self._sinks:
            sink.emit(line)

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT) -> None:
        for sink in self._sinks:
            sink.close(timeout=timeout)

    @property
    def stats(self) -> dict[str, int]:
        totals = {"written": 0, "dropped": 0, "write_errors": 0}
        for sink in self._sinks:
            for key, value in sink.stats.items():
                totals[key] = totals.get(key, 0) + value
        return totals


class AuditSink(_QueuedSink):
    """Writes audit lines to a per-process file on a background thread."""

    _THREAD_NAME = "cb-mcp-audit-writer"

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int,
        max_backups: int,
        interval_seconds: int = 0,
        compress: bool = True,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        """Open the sink's file, creating parent directories as needed.

        ``max_bytes`` of 0 turns size-based rotation off, ``interval_seconds``
        of 0 turns age-based rotation off, and both off means one file that
        grows for as long as the process runs. Only negative values are
        rejected — they are typos, not instructions.

        Raises:
            OSError: if the file cannot be created or opened. The caller is
                expected to report this and continue without auditing rather
                than abort startup.
        """
        if max_bytes < 0:
            raise ValueError(f"max_bytes must not be negative, got {max_bytes}.")
        if max_backups < 0:
            raise ValueError(f"max_backups must not be negative, got {max_backups}.")
        if interval_seconds < 0:
            raise ValueError(
                f"interval_seconds must not be negative, got {interval_seconds}."
            )

        super().__init__(queue_size=queue_size)
        self.path = process_scoped_path(path)
        self._max_bytes = max_bytes
        self._max_backups = max_backups
        self._interval = interval_seconds
        self._compress = compress

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Block-buffered text append: durability comes from the explicit flush
        # at the end of each batch, not from the open mode. Opened eagerly so a
        # misconfigured path fails at startup, where it can be reported, rather
        # than on the first audited operation.
        # Deliberately not a context manager: the stream is owned for the
        # lifetime of the sink and written by the background thread. Closing it
        # per record would defeat both the batching and the append semantics.
        self._stream = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self._size = self.path.stat().st_size
        self._rotate_at = self._next_rotation_deadline(from_existing_file=True)

    def _next_rotation_deadline(self, *, from_existing_file: bool = False) -> float:
        """When the interval trigger next falls due, or ``inf`` when it is off.

        On open the clock is anchored to the file's **last modification time**
        rather than to now, so a restart does not hand the live file a fresh
        interval it has not earned. A file last written longer ago than the
        interval is therefore already due and rolls over on the first record —
        which is what an operator asking for daily files expects after the
        server was down for a week. Once running, each rotation sets the next
        deadline from the clock, so a busy file cannot push its own deadline
        forward by being written to.
        """
        if self._interval <= 0:
            return float("inf")
        anchor = time.time()
        if from_existing_file:
            try:
                stat = self.path.stat()
                if stat.st_size > 0:
                    anchor = stat.st_mtime
            except OSError:  # pragma: no cover - file was just opened
                pass
        return anchor + self._interval

    # -- file-specific hooks ----------------------------------------------

    def _describe(self) -> str:
        return str(self.path)

    def _on_idle(self) -> None:
        """An idle server still ages its file.

        Without this, a daily rotation on a quiet deployment would not happen
        until the next record arrived — possibly days late, with a day's worth
        of records already in the wrong file.
        """
        self._rotate_on_idle()

    def _on_close(self) -> None:
        """Flush and close the live file once the writer has stopped."""
        try:
            if not self._stream.closed:
                self._stream.flush()
                self._stream.close()
        except (OSError, ValueError):  # pragma: no cover - nothing left to do
            # ValueError is "I/O operation on closed file". The writer may still
            # be draining past the join timeout and may close the stream between
            # the check above and the flush; letting that escape would abort
            # shutdown — including the sibling sinks a composite is still
            # closing, and the atexit hook.
            pass

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
        buffered = 0
        for line in lines:
            try:
                encoded_length = len(line.encode("utf-8"))
                self._reopen_if_closed()
                self._rotate_if_needed(encoded_length)
                self._stream.write(line)
                self._size += encoded_length
                buffered += 1
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
            # The records counted as buffered above never reached the disk. A
            # full disk fails here, not at ``write`` — so counting them as
            # written at write time reported a clean audit trail for records
            # that were lost, and ``dropped`` is documented as the only way an
            # operator discovers a gap.
            self._errors += 1
            self._dropped += buffered
            buffered = 0
            self._log_failure(f"failed to flush audit file: {exc}")
        self._written += buffered

    def _interval_is_due(self) -> bool:
        """Whether the interval has elapsed, skipping the period if nothing was written.

        An empty live file is never rotated — there is nothing to keep, and a
        zero-byte backup would spend a retention slot — but the deadline must
        still move on. Leaving it in the past would mean the *first* record
        after a quiet period arrives already overdue and is immediately split
        off into a backup of its own: with ``max_backups=0`` that record is
        truncated away by the very next one, counted as written and never seen
        again.
        """
        if self._interval <= 0 or time.time() < self._rotate_at:
            return False
        if self._size == 0:
            self._rotate_at = self._next_rotation_deadline()
            return False
        return True

    def _rotate_on_idle(self) -> None:
        """Roll over on the interval alone, with no record waiting to be written."""
        if not self._interval_is_due():
            return
        try:
            self._reopen_if_closed()
            self._rotate()
        except (OSError, ValueError) as exc:
            self._errors += 1
            self._log_failure(f"failed to rotate the audit file: {exc}")

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        """Roll the file over when either trigger has fallen due.

        The two triggers are independent and either may be off. For size, a
        single line larger than ``max_bytes`` is still written rather than
        discarded — losing the record would be worse than briefly exceeding the
        cap — but it lands in a file of its own on the next rotation.
        """
        # Asked first, and unconditionally: it is what advances the deadline
        # past a period in which nothing was written.
        due_on_interval = self._interval_is_due()
        if self._size == 0:
            return
        due_on_size = (
            self._max_bytes > 0 and self._size + incoming_bytes > self._max_bytes
        )
        if not (due_on_size or due_on_interval):
            return
        self._rotate()

    def _rotate(self) -> None:
        """Close the live file, preserve it per the retention policy, reopen."""
        self._stream.flush()
        self._stream.close()

        if self._max_backups == 0:
            # Retention of zero means no backups at all: the live file is
            # truncated, which is the PRD's "limited space, single file" case —
            # at the cap, writing starts again from zero and what was there is
            # gone.
            self._stream = open(self.path, "w", encoding="utf-8")  # noqa: SIM115
            self._size = 0
            self._rotate_at = self._next_rotation_deadline()
            return

        backup = self._reserve_backup_path()
        self.path.replace(backup)
        self._size = 0
        self._rotate_at = self._next_rotation_deadline()
        try:
            self._stream = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        finally:
            # Compress and prune in a ``finally`` so a failed reopen — a disk
            # that filled at exactly the wrong moment — still leaves the
            # retired file compressed and the retention count honoured. The
            # caller sees the OSError, counts it and recovers on the next
            # record; what it must not find later is an uncompressed orphan
            # holding a retention slot for good.
            compressed = self._compress_backup(backup) if self._compress else backup
            self._prune_backups(keep=compressed)

    def _reserve_backup_path(self) -> Path:
        """A free ``<live name>.<timestamp>`` path for the file being retired.

        The timestamp is when the rotation happened, in UTC to the microsecond.
        Two rotations inside one microsecond — which a small size cap makes
        conceivable — would otherwise collide, so a ``-N`` disambiguator is
        appended rather than overwriting a backup that was just made. The
        search starts above the highest index already present rather than at
        the first free one: reusing an index freed by pruning would give a
        brand-new backup the sort position of the file it replaced.
        """
        stamp = datetime.now(timezone.utc).strftime(_BACKUP_TIMESTAMP_FORMAT)
        candidate = self.path.with_name(f"{self.path.name}.{stamp}")
        if not candidate.exists() and not _with_gz(candidate).exists():
            return candidate
        index = max(
            (
                taken
                for existing in self._existing_backups()
                for taken in (_backup_sort_key(existing)[1],)
                if _backup_sort_key(existing)[0] == stamp
            ),
            default=0,
        )
        while True:
            index += 1
            candidate = self.path.with_name(f"{self.path.name}.{stamp}-{index}")
            if not candidate.exists() and not _with_gz(candidate).exists():
                return candidate

    def _compress_backup(self, backup: Path) -> Path:
        """Gzip a rotated file, returning the path that now holds it.

        Audit retention is measured in months, so the backups are the bulk of
        what an operator stores; JSON Lines compresses roughly ten to one. The
        **live** file is deliberately left uncompressed — ``tail -f`` and
        ``grep`` on the current file are how an incident actually gets
        investigated.

        A compression failure is reported and the uncompressed backup is kept.
        Losing an audit file to save space would invert the entire point.
        """
        target = _with_gz(backup)
        try:
            with (
                open(backup, "rb") as source,
                gzip.open(target, "wb") as destination,
            ):
                shutil.copyfileobj(source, destination)
            backup.unlink()
            return target
        except OSError as exc:
            self._errors += 1
            self._log_failure(f"failed to compress the rotated audit file: {exc}")
            with contextlib.suppress(OSError):
                if target.exists():
                    target.unlink()
            return backup

    def _existing_backups(self) -> list[Path]:
        """This file's own rotated backups, oldest first.

        Matching is deliberately narrow: the glob is anchored to *this*
        process's live filename and the suffix must be one this sink wrote.
        Per-process filenames mean another server's backups never match, and
        the suffix check means an operator's own ``audit.log.notes`` beside them
        is never a deletion candidate.

        Ordering is by the *parsed* timestamp and index, not by the raw
        filename. Sorting the names would be wrong, and silently so: ``-``
        sorts before ``.``, so ``…Z-1.gz`` precedes ``…Z.gz`` even though it is
        the newer file, and ``-10`` precedes ``-2``. Retention would then delete
        the most recent history and keep the oldest — the exact inverse of what
        every one of the PRD's retention cases asks for.
        """
        parent = self.path.parent
        try:
            # The live filename is escaped before it becomes a glob pattern.
            # Unescaped, a path like ``audit[1].log`` reads ``[1]`` as a
            # character class, matches nothing, and retention silently stops
            # pruning — the directory grows without limit while max_backups
            # claims to bound it.
            candidates = list(parent.glob(f"{glob.escape(self.path.name)}.*"))
        except OSError:  # pragma: no cover - directory vanished mid-rotation
            return []
        return sorted(
            (path for path in candidates if _BACKUP_SUFFIX.search(path.name)),
            key=_backup_sort_key,
        )

    def _prune_backups(self, *, keep: Path) -> None:
        """Delete the oldest backups beyond ``max_backups``.

        ``keep`` is the backup just created; it is never a deletion candidate
        even if the retention count is somehow already exceeded, because
        deleting the records that were just rotated out would lose the most
        recent history rather than the oldest.
        """
        backups = [path for path in self._existing_backups() if path != keep]
        excess = len(backups) - (self._max_backups - 1)
        for path in backups[:excess] if excess > 0 else []:
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - racing external deletion
                self._log_failure(
                    f"failed to delete the old audit backup {path}: {exc}"
                )


def _with_gz(path: Path) -> Path:
    """The compressed name for a backup path."""
    return path.with_name(f"{path.name}.gz")


def _backup_sort_key(path: Path) -> tuple[str, int]:
    """``(timestamp, index)`` for a rotated file — chronological order.

    The index is the same-microsecond disambiguator, read as a number so ``-2``
    orders before ``-10``, and ``0`` for the undisambiguated first file of its
    microsecond so it orders before ``-1``. A path that somehow does not match
    sorts first, which makes it the first candidate for deletion rather than
    the last — the conservative direction for something unrecognised sitting
    among the backups.
    """
    match = _BACKUP_SUFFIX.search(path.name)
    if match is None:  # pragma: no cover - callers filter on the same regex
        return ("", 0)
    return (match.group("stamp"), int(match.group("index") or 0))


__all__ = [
    "DEFAULT_CLOSE_TIMEOUT",
    "DEFAULT_QUEUE_SIZE",
    "AuditSink",
    "AuditSinkProtocol",
    "CompositeAuditSink",
    "ConsoleAuditSink",
    "process_scoped_path",
]
