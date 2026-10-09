"""Tests for the audit sinks.

The file sink deliberately does not use ``logging.handlers.RotatingFileHandler``.
A single stdio deployment runs one server process per MCP client, and they all
read the same ``CB_MCP_AUDIT_LOG_FILE_PATH``; sharing one rotating file between
them produces interleaved partial lines and rotation races. Each process
therefore writes its own file, with the host and pid inserted before the
extension. Owning the rotation is also what lets the size and interval triggers
both be live at once, which the stdlib handlers cannot do.

Coverage map:
- per-process filename derivation, with and without a suffix
- lines are written and flushed by the background thread
- rotation on size, on interval, on both, and on neither
- the interval clock is anchored to the live file, not to startup
- an idle server still rotates on the interval
- backups are timestamped, gzipped and pruned to max_backups
- a compression failure keeps the uncompressed backup
- max_backups=0 truncates rather than growing without limit
- an oversized single line is written rather than lost
- constructor validation: zero is accepted, negatives are not
- drop counter when the queue is full
- close() is idempotent and drains
- two concurrent sinks never share a file
- the console sink writes to stderr, and the composite fans out
"""

from __future__ import annotations

import functools
import gzip
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cb_mcp.audit.sink import (
    _SENTINEL,
    AuditSink,
    CompositeAuditSink,
    ConsoleAuditSink,
    process_scoped_path,
)


def _drain(sink: AuditSink) -> None:
    """Close the sink so the writer thread finishes and flushes."""
    sink.close(timeout=10.0)


def _settle(sink: AuditSink, timeout: float = 2.0) -> None:
    """Wait for the writer to drain the queue, without closing the sink."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not sink._queue.empty():
        time.sleep(0.02)
    time.sleep(0.1)


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def test_process_scoped_path_inserts_host_and_pid_before_the_suffix():
    result = process_scoped_path("/var/log/audit.log", pid=4321, host="node-a")
    assert result == Path("/var/log/audit.node-a.4321.log")


def test_process_scoped_path_appends_when_there_is_no_suffix():
    result = process_scoped_path("/var/log/audit", pid=4321, host="node-a")
    assert result == Path("/var/log/audit.node-a.4321")


def test_process_scoped_path_uses_the_live_host_and_pid_by_default():
    assert str(os.getpid()) in process_scoped_path("audit.log").name


def test_containers_sharing_a_volume_do_not_share_a_file():
    """The reason the host is in the name at all.

    This image's ENTRYPOINT is exec form, so the server is PID 1 in *every*
    container. Two containers mounting the same volume would both have written
    ``audit.1.log`` — the interleaving and rotation races that per-writer files
    exist to prevent, in the configuration DOCKER.md recommends. Docker gives
    each container a distinct hostname by default, which restores uniqueness.
    """
    first = process_scoped_path("/audit/audit.log", pid=1, host="3f2a91c4b7de")
    second = process_scoped_path("/audit/audit.log", pid=1, host="a81ce4470f19")
    assert first != second
    assert first.name == "audit.3f2a91c4b7de.1.log"


def test_host_token_is_filename_safe():
    """An FQDN or an odd hostname must not put dots or separators in the name."""
    assert (
        process_scoped_path("a.log", pid=7, host="node1.example.com").name
        == "a.node1.7.log"
    )
    assert (
        process_scoped_path("a.log", pid=7, host="we ird/name").name
        == "a.we-ird-name.7.log"
    )
    assert process_scoped_path("a.log", pid=7, host="").name == "a.unknown.7.log"


def test_sink_writes_to_the_process_scoped_path(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, max_backups=1)
    try:
        assert sink.path == process_scoped_path(tmp_path / "audit.log")
        assert sink.path.exists()
    finally:
        _drain(sink)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def test_lines_are_written_and_flushed(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=1_000_000, max_backups=1)
    sink.start()
    for index in range(50):
        sink.emit(json.dumps({"n": index}) + "\n")
    _drain(sink)

    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    assert [json.loads(line)["n"] for line in lines] == list(range(50))
    assert sink.stats["written"] == 50
    assert sink.stats["dropped"] == 0
    assert sink.stats["write_errors"] == 0


def test_existing_file_is_appended_not_truncated(tmp_path):
    path = process_scoped_path(tmp_path / "audit.log")
    path.write_text('{"pre":true}\n', encoding="utf-8")

    sink = AuditSink(tmp_path / "audit.log", max_bytes=1_000_000, max_backups=1)
    sink.start()
    sink.emit('{"post":true}\n')
    _drain(sink)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"pre": True}
    assert json.loads(lines[1]) == {"post": True}


# ---------------------------------------------------------------------------
# rotation and retention
# ---------------------------------------------------------------------------


def _backups(sink: AuditSink) -> list[Path]:
    """Rotated files beside the live one, oldest first."""
    return sorted(
        path
        for path in sink.path.parent.iterdir()
        if path.name.startswith(sink.path.name + ".")
    )


def _all_records(sink: AuditSink) -> str:
    """Everything the sink has written, live file and backups, decompressed."""
    text = sink.path.read_text(encoding="utf-8")
    for path in _backups(sink):
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                text += handle.read()
        else:
            text += path.read_text(encoding="utf-8")
    return text


def test_rotation_names_backups_by_time_and_honours_retention(tmp_path):
    line = json.dumps({"payload": "x" * 80}) + "\n"
    # Room for roughly two lines before rotating.
    sink = AuditSink(tmp_path / "audit.log", max_bytes=len(line) * 2, max_backups=2)
    sink.start()
    for _ in range(12):
        sink.emit(line)
    _drain(sink)

    backups = _backups(sink)
    assert sink.path.exists()
    # Retention is two backups, so a third must never survive.
    assert len(backups) == 2, [p.name for p in backups]
    for path in backups:
        # <live name>.<UTC timestamp>[-N].gz — readable without opening the file,
        # and sortable because the timestamp is fixed-width.
        assert re.fullmatch(
            re.escape(sink.path.name) + r"\.\d{8}T\d{12}Z(-\d+)?\.gz", path.name
        ), path.name
    assert sink.path.stat().st_size <= len(line) * 2


def test_rotated_backups_are_gzipped_and_still_readable(tmp_path):
    line = json.dumps({"payload": "compress me"}) + "\n"
    sink = AuditSink(tmp_path / "audit.log", max_bytes=len(line) * 2, max_backups=5)
    sink.start()
    for _ in range(6):
        sink.emit(line)
    _drain(sink)

    backups = _backups(sink)
    assert backups, "nothing rotated"
    for path in backups:
        assert path.suffix == ".gz"
        # Real gzip, not a renamed plain file: decompression must round-trip.
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            assert "compress me" in handle.read()
    # The live file stays plain text. tail -f and grep on the current file are
    # how an incident is actually investigated.
    assert "compress me" in sink.path.read_text(encoding="utf-8")


def test_compression_failure_keeps_the_uncompressed_backup(tmp_path):
    """Losing an audit file to save space would invert the whole point."""
    line = json.dumps({"payload": "k" * 60}) + "\n"
    sink = AuditSink(tmp_path / "audit.log", max_bytes=len(line) * 2, max_backups=3)
    sink.start()
    with patch(
        "cb_mcp.audit.sink.gzip.open", side_effect=OSError("no space for the gzip")
    ):
        for _ in range(4):
            sink.emit(line)
        _settle(sink)
    _drain(sink)

    backups = _backups(sink)
    assert backups, "the rotation did not keep the retired file"
    assert all(path.suffix != ".gz" for path in backups)
    assert "k" * 60 in _all_records(sink)


def test_max_backups_zero_truncates_the_live_file(tmp_path):
    line = json.dumps({"payload": "y" * 80}) + "\n"
    sink = AuditSink(tmp_path / "audit.log", max_bytes=len(line) * 2, max_backups=0)
    sink.start()
    for _ in range(20):
        sink.emit(line)
    _drain(sink)

    # Retention of zero still means the rotation size caps the live file.
    assert sink.path.stat().st_size <= len(line) * 2
    assert _backups(sink) == []


def test_a_single_oversized_line_is_written_not_dropped(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=64, max_backups=1)
    sink.start()
    huge = json.dumps({"payload": "z" * 500}) + "\n"
    sink.emit(huge)
    _drain(sink)

    # Losing the record would be worse than briefly exceeding the size cap.
    assert "z" * 500 in _all_records(sink)
    assert sink.stats["dropped"] == 0


# ---------------------------------------------------------------------------
# rotation triggers: size, interval, both, neither
# ---------------------------------------------------------------------------


def test_zero_max_bytes_never_rotates_on_size(tmp_path):
    """PRD case 2: 'I don't care about the size, store them all.'"""
    line = json.dumps({"payload": "q" * 200}) + "\n"
    sink = AuditSink(tmp_path / "audit.log", max_bytes=0, max_backups=5)
    sink.start()
    for _ in range(50):
        sink.emit(line)
    _drain(sink)

    assert _backups(sink) == [], "size rotation fired with the size trigger off"
    assert len(sink.path.read_text(encoding="utf-8").splitlines()) == 50


def test_interval_rotation_fires_when_the_file_is_old_enough(tmp_path):
    sink = AuditSink(
        tmp_path / "audit.log", max_bytes=0, max_backups=3, interval_seconds=86_400
    )
    sink.start()
    sink.emit('{"before":true}\n')
    _settle(sink)
    assert _backups(sink) == [], "rotated before the interval fell due"

    # Bring the deadline into the past rather than waiting a day for it.
    sink._rotate_at = time.time() - 1
    sink.emit('{"after":true}\n')
    _settle(sink)
    _drain(sink)

    backups = _backups(sink)
    assert len(backups) == 1, [p.name for p in backups]
    with gzip.open(backups[0], "rt", encoding="utf-8") as handle:
        assert "before" in handle.read()
    assert "after" in sink.path.read_text(encoding="utf-8")


def test_an_idle_server_still_rotates_on_the_interval(tmp_path):
    """The reason the writer checks the clock on an empty queue.

    A quiet deployment writes nothing for hours. Rotating only when the next
    record arrives would put a day's worth of records in a file stamped for the
    previous period, which is precisely what a daily-rotation operator is
    trying to avoid.
    """
    sink = AuditSink(
        tmp_path / "audit.log", max_bytes=0, max_backups=3, interval_seconds=86_400
    )
    sink.start()
    sink.emit('{"only":true}\n')
    _settle(sink)

    sink._rotate_at = time.time() - 1
    # No further records: the rotation must come from the idle poll alone.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not _backups(sink):
        time.sleep(0.05)
    _drain(sink)

    assert len(_backups(sink)) == 1, "an idle sink never rotated"
    assert sink.path.stat().st_size == 0


def test_both_triggers_are_live_at_once(tmp_path):
    """Whichever falls due first rotates: stdlib cannot do this, so we do."""
    line = json.dumps({"payload": "b" * 80}) + "\n"
    sink = AuditSink(
        tmp_path / "audit.log",
        max_bytes=len(line) * 2,
        max_backups=10,
        interval_seconds=86_400,
    )
    sink.start()
    # Size alone, with the interval deadline far in the future.
    for _ in range(6):
        sink.emit(line)
    _settle(sink)
    after_size = len(_backups(sink))
    assert after_size >= 2, "the size trigger did not fire while an interval was set"

    # Now the interval, with the file nowhere near the size cap.
    sink._rotate_at = time.time() - 1
    sink.emit(line)
    _settle(sink)
    _drain(sink)
    assert len(_backups(sink)) > after_size, "the interval trigger never fired"


def test_neither_trigger_means_one_unbounded_file(tmp_path):
    sink = AuditSink(
        tmp_path / "audit.log", max_bytes=0, max_backups=0, interval_seconds=0
    )
    sink.start()
    sink._rotate_at = time.time() - 1  # would rotate if the interval were live
    for index in range(100):
        sink.emit(json.dumps({"n": index}) + "\n")
    _drain(sink)

    assert _backups(sink) == []
    assert len(sink.path.read_text(encoding="utf-8").splitlines()) == 100


def test_the_interval_clock_is_anchored_to_the_live_file_not_to_startup(tmp_path):
    """A restart must not hand a stale file a fresh interval it has not earned.

    An operator asking for daily files, whose server was down for a week, wants
    the week-old file rolled away — not another day of records appended to it.
    """
    path = process_scoped_path(tmp_path / "audit.log")
    path.write_text('{"old":true}\n', encoding="utf-8")
    week_ago = time.time() - 7 * 86_400
    os.utime(path, (week_ago, week_ago))

    sink = AuditSink(
        tmp_path / "audit.log", max_bytes=0, max_backups=2, interval_seconds=86_400
    )
    try:
        assert sink._rotate_at < time.time(), (
            "a file older than the interval was given a fresh deadline"
        )
    finally:
        _drain(sink)

    # ... while a file that did not exist a moment ago gets its full interval.
    fresh = AuditSink(
        tmp_path / "fresh.log", max_bytes=0, max_backups=2, interval_seconds=86_400
    )
    try:
        assert fresh._rotate_at > time.time() + 86_000
    finally:
        _drain(fresh)


# ---------------------------------------------------------------------------
# validation and failure handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_bytes": -1, "max_backups": 1}, "max_bytes must not be negative"),
        ({"max_bytes": 10, "max_backups": -1}, "max_backups must not be negative"),
        (
            {"max_bytes": 10, "max_backups": 1, "interval_seconds": -1},
            "interval_seconds must not be negative",
        ),
    ],
)
def test_constructor_rejects_negatives_but_not_zero(tmp_path, kwargs, match):
    """Zero is an instruction — 'this trigger is off' — and a typo is not."""
    with pytest.raises(ValueError, match=match):
        AuditSink(tmp_path / "audit.log", **kwargs)

    # The zero the PRD requires to be accepted, in the same test so a future
    # tightening of the validation cannot pass by rejecting both.
    _drain(
        AuditSink(
            tmp_path / "audit.log", max_bytes=0, max_backups=0, interval_seconds=0
        )
    )


def test_parent_directories_are_created(tmp_path):
    nested = tmp_path / "deep" / "deeper" / "audit.log"
    sink = AuditSink(nested, max_bytes=4096, max_backups=1)
    try:
        assert sink.path.parent.is_dir()
    finally:
        _drain(sink)


def test_unwritable_path_raises_so_startup_can_report_it(tmp_path):
    # A directory where the file should be is the portable way to make open()
    # fail without depending on running as an unprivileged user.
    collision = process_scoped_path(tmp_path / "audit.log")
    collision.mkdir()
    with pytest.raises(OSError):
        AuditSink(tmp_path / "audit.log", max_bytes=4096, max_backups=1)


def test_full_queue_drops_and_counts_rather_than_blocking(tmp_path):
    # No writer thread started, so nothing drains the queue.
    sink = AuditSink(
        tmp_path / "audit.log", max_bytes=1_000_000, max_backups=1, queue_size=4
    )
    for _ in range(10):
        sink.emit('{"x":1}\n')
    assert sink.stats["dropped"] == 6
    _drain(sink)


def test_emit_after_close_is_counted_not_raised(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, max_backups=1)
    sink.start()
    _drain(sink)
    sink.emit('{"late":true}\n')
    assert sink.stats["dropped"] == 1


def test_close_is_idempotent(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, max_backups=1)
    sink.start()
    sink.close()
    sink.close()  # must not raise


def test_start_is_idempotent(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, max_backups=1)
    sink.start()
    sink.start()
    try:
        threads = [t for t in threading.enumerate() if t.name == "cb-mcp-audit-writer"]
        assert len(threads) == 1
    finally:
        _drain(sink)


def test_emit_from_many_threads_loses_nothing(tmp_path):
    """The producer side must be safe to call from any thread."""
    sink = AuditSink(tmp_path / "audit.log", max_bytes=10_000_000, max_backups=1)
    sink.start()

    def worker(worker_id: int) -> None:
        for index in range(100):
            sink.emit(json.dumps({"w": worker_id, "i": index}) + "\n")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _drain(sink)

    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 800
    # Every line must be complete JSON: no interleaved partial writes.
    parsed = [json.loads(line) for line in lines]
    assert len({(r["w"], r["i"]) for r in parsed}) == 800


def test_sink_recovers_after_a_failed_rotation(tmp_path):
    """A failed reopen must not stop the sink for good.

    Rotation closes the live file before reopening it. If that reopen fails —
    full disk, revoked permission, unmounted volume — the sink was left holding
    a closed handle and never wrote again, even after the condition cleared.
    Silent and permanent is the worst failure mode an audit log can have.
    """
    sink = AuditSink(tmp_path / "a.log", max_bytes=200, max_backups=0)
    sink.start()
    line = '{"pad":"' + "x" * 80 + '"}\n'

    for _ in range(3):
        sink.emit(line)
    _settle(sink)
    before = sink.stats["written"]

    real_open = open
    failed = {"done": False}
    # The sink writes a process-scoped path (a.<pid>.log), so match on the
    # resolved filename rather than the configured one.
    target = str(sink.path)

    def flaky(*args, **kwargs):
        # Fail the first reopen attempted during rotation, then behave.
        if not failed["done"] and args and str(args[0]) == target:
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            if "w" in mode or "a" in mode:
                failed["done"] = True
                raise OSError("disk full")
        return real_open(*args, **kwargs)

    with patch("builtins.open", side_effect=flaky):
        for _ in range(3):
            sink.emit(line)
        _settle(sink)

    assert failed["done"], "the probe never exercised the failing reopen"
    assert sink.stats["write_errors"] >= 1, "the failed reopen was not recorded"

    # The measurement that matters: writes attempted *after* the failure. The
    # count is taken here, once the stream is known to be closed, so a record
    # that happened to land before the failing rotation cannot mask a sink that
    # never recovered.
    stalled_at = sink.stats["written"]
    for _ in range(3):
        sink.emit(line)
    _settle(sink)
    sink.close()

    assert sink.stats["written"] > stalled_at, (
        "sink never wrote again after the failed rotation: "
        f"written stayed at {stalled_at}"
    )
    assert before <= stalled_at


def test_writer_exits_when_the_queue_is_full_at_close(tmp_path):
    """close() cannot post the sentinel into a full queue.

    Dropping a queued record to make room would lose an audit line purely to
    shut down faster, so the writer polls the closed flag instead. Before that,
    it blocked on an empty get() forever and the thread leaked.
    """
    sink = AuditSink(tmp_path / "b.log", max_bytes=10_000, max_backups=1, queue_size=4)
    sink.start()
    time.sleep(0.05)

    with patch.object(sink, "_write_batch", side_effect=lambda lines: time.sleep(0.5)):
        for index in range(20):
            sink.emit(f'{{"i":{index}}}\n')
        sink.close(timeout=3.0)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not any(
            t.name == "cb-mcp-audit-writer" and t.is_alive()
            for t in threading.enumerate()
        ):
            break
        time.sleep(0.05)
    assert not any(
        t.name == "cb-mcp-audit-writer" and t.is_alive() for t in threading.enumerate()
    ), "writer thread did not exit"


def test_a_draining_writer_does_not_reopen_the_file_after_close(tmp_path):
    """``close`` joins with a timeout, then closes the stream.

    A writer still draining past that timeout — the wedged filesystem the
    timeout exists for — would otherwise reopen a handle nobody closes again,
    and append records dated after "server stopped". It must drop them instead.
    """
    sink = AuditSink(tmp_path / "c.log", max_bytes=100_000, max_backups=1)
    sink.start()
    released = threading.Event()
    real_write = sink._write_batch

    def slow(lines):
        released.wait(timeout=5.0)
        return real_write(lines)

    with patch.object(sink, "_write_batch", side_effect=slow):
        sink.emit('{"first":1}\n')
        time.sleep(0.2)
        sink.emit('{"second":2}\n')
        sink.close(timeout=0.2)  # returns while the batch is still in flight
        released.set()
        time.sleep(0.4)

    assert sink._stream.closed, "the writer reopened the stream after close"


# ---------------------------------------------------------------------------
# console and composite sinks
# ---------------------------------------------------------------------------


class _BrokenWrite(io.StringIO):
    """Raises on write, optionally after accepting a few lines first."""

    def __init__(self, fail_after: int = 0) -> None:
        super().__init__()
        self._fail_after = fail_after
        self._accepted = 0

    def write(self, text):
        if self._accepted >= self._fail_after:
            raise OSError("stream is gone")
        self._accepted += 1
        return super().write(text)


class _BrokenFlush(io.StringIO):
    """Buffers writes happily, then fails to flush, like a full disk."""

    def flush(self):
        raise OSError("No space left on device")


def test_console_sink_writes_lines_to_its_stream():
    stream = io.StringIO()
    sink = ConsoleAuditSink(stream)
    sink.start()
    sink.emit('{"a":1}\n')
    sink.emit('{"b":2}\n')
    sink.close(timeout=5.0)

    assert stream.getvalue() == '{"a":1}\n{"b":2}\n'
    assert sink.stats == {"written": 2, "dropped": 0, "write_errors": 0}


def test_console_sink_defaults_to_stderr_never_stdout(capsys):
    """stdout carries the JSON-RPC protocol under the stdio transport.

    One audit line written there corrupts the client's stream and takes the
    session down, so the default stream is not merely a preference.
    """
    sink = ConsoleAuditSink()
    sink.start()
    sink.emit('{"audited":true}\n')
    sink.close(timeout=5.0)

    captured = capsys.readouterr()
    assert captured.err == '{"audited":true}\n'
    assert captured.out == ""


def test_console_sink_never_blocks_its_caller_on_a_stalled_stream():
    """The blocker this sink's queue exists for.

    ``console`` is the *default* sink, and under stdio its stderr is a pipe
    owned by the MCP client. Written inline, a client that stops draining that
    pipe blocks the audit write at the 64 KiB buffer — and the caller is the
    asyncio event loop, so every in-flight tool call and the JSON-RPC reader
    stall with it. Enabling auditing was enough to hang the server.

    The stall is a blocking ``write`` the test controls, rather than a real
    pipe: the emitting runs on a worker thread and the stall is released in
    teardown, so a regression *fails* here instead of hanging the suite —
    which matters when the property under test is "does not block forever".
    """
    release = threading.Event()
    entered = threading.Event()

    class StalledStream(io.StringIO):
        def write(self, text):
            entered.set()
            release.wait(timeout=30)
            return super().write(text)

    sink = ConsoleAuditSink(StalledStream(), queue_size=16)
    sink.start()
    finished = threading.Event()

    def emit_many() -> None:
        for index in range(2000):
            sink.emit(json.dumps({"n": index}) + "\n")
        finished.set()

    worker = threading.Thread(target=emit_many, daemon=True)
    try:
        worker.start()
        assert entered.wait(timeout=5.0), "the writer never reached the stream"
        assert finished.wait(timeout=5.0), (
            "emit() blocked on a stalled stream; the caller is the event loop"
        )
        # The records that could not be written are dropped and *counted* —
        # that is the whole bargain, and the counter is how an operator finds
        # out. Silence would be the unacceptable outcome.
        assert sink.stats["dropped"] > 0
    finally:
        release.set()
        sink.close(timeout=2.0)


@pytest.mark.parametrize(
    ("stream_factory", "label"),
    [
        (_BrokenWrite, "write fails"),
        (_BrokenFlush, "flush fails"),
        (functools.partial(_BrokenWrite, fail_after=2), "write fails mid-batch"),
    ],
)
def test_console_sink_counts_every_lost_record(stream_factory, label):
    """A buffered line that is never flushed never reached the console.

    Crediting only the lines not yet handed to ``write`` left the rest
    unaccounted — ``written`` at zero and ``dropped`` at zero while records
    vanished. ``dropped`` is documented as the only way an operator learns of
    a gap, so it has to cover the whole batch.
    """
    sink = ConsoleAuditSink(stream_factory())
    sink.start()
    for index in range(5):
        sink.emit(json.dumps({"n": index}) + "\n")
    sink.close(timeout=5.0)

    stats = sink.stats
    assert stats["write_errors"] >= 1, label
    assert stats["written"] + stats["dropped"] == 5, f"{label}: {stats}"
    assert stats["written"] == 0, f"{label}: {stats}"


def test_console_sink_emits_whole_lines_under_concurrency():
    stream = io.StringIO()
    sink = ConsoleAuditSink(stream)
    sink.start()

    def worker(worker_id: int) -> None:
        for index in range(50):
            sink.emit(json.dumps({"w": worker_id, "i": index}) + "\n")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sink.close(timeout=5.0)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 300
    # Every line must be complete JSON: no record split by another thread.
    assert (
        len({(json.loads(line)["w"], json.loads(line)["i"]) for line in lines}) == 300
    )


def test_composite_writes_to_every_sink_and_sums_their_stats(tmp_path):
    stream = io.StringIO()
    console = ConsoleAuditSink(stream)
    file_sink = AuditSink(tmp_path / "audit.log", max_bytes=0, max_backups=0)
    composite = CompositeAuditSink([console, file_sink])
    composite.start()
    composite.emit('{"both":true}\n')
    composite.close(timeout=10.0)

    assert stream.getvalue() == '{"both":true}\n'
    assert file_sink.path.read_text(encoding="utf-8") == '{"both":true}\n'
    assert composite.stats["written"] == 2


def test_one_failing_sink_does_not_stop_the_others(tmp_path):
    """A full disk must not also cost the operator their console records."""

    class Broken(io.StringIO):
        def write(self, _text):
            raise OSError("stream is gone")

    file_sink = AuditSink(tmp_path / "audit.log", max_bytes=0, max_backups=0)
    composite = CompositeAuditSink([ConsoleAuditSink(Broken()), file_sink])
    composite.start()
    composite.emit('{"survives":true}\n')
    composite.close(timeout=10.0)

    assert file_sink.path.read_text(encoding="utf-8") == '{"survives":true}\n'
    assert composite.stats["written"] == 1
    assert composite.stats["dropped"] == 1


# ---------------------------------------------------------------------------
# retention keeps the newest, end to end
# ---------------------------------------------------------------------------


def _numbered(sink: AuditSink) -> list[int]:
    """Every surviving record's ``n``, across backups and the live file."""
    numbers: list[int] = []
    for path in [*_backups(sink), sink.path]:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                text = handle.read()
        else:
            text = path.read_text(encoding="utf-8")
        numbers += [json.loads(line)["n"] for line in text.splitlines() if line.strip()]
    return sorted(numbers)


def test_retention_discards_the_oldest_records_not_the_newest(tmp_path):
    """Counting the backups is not enough: *which* ones survive is the point.

    Every retention case in the PRD — 100 MB across ten files, 90 days of daily
    logs — means a sliding window over the most recent history. An earlier
    version of this sink ordered backups by filename, and because ``-`` sorts
    before ``.`` the same-second disambiguator inverted the order: it deleted
    the newest backups and kept the oldest, while still reporting exactly
    ``max_backups`` files. The count was right and the contents were useless.
    """
    line = json.dumps({"n": 0}) + "\n"
    # Two records a file, many rotations, several of them inside one second.
    sink = AuditSink(tmp_path / "audit.log", max_bytes=len(line) * 2, max_backups=9)
    sink.start()
    for index in range(60):
        sink.emit(json.dumps({"n": index}) + "\n")
    _drain(sink)

    survivors = _numbered(sink)
    assert len(_backups(sink)) == 9
    # The window is the tail of the sequence: contiguous, and ending at the
    # last record written.
    assert survivors[-1] == 59
    assert survivors == list(range(survivors[0], 60)), survivors
    # Ten files of two records each; nothing older may have been kept.
    assert survivors[0] >= 40, f"stale records survived: {survivors}"


def test_backups_are_ordered_chronologically_not_alphabetically(tmp_path):
    """The ordering itself, isolated from the rotation that produces it."""
    sink = AuditSink(tmp_path / "audit.log", max_bytes=0, max_backups=5)
    try:
        # Written out of order, and spanning the disambiguators that broke a
        # naive name sort: '-' < '.', and '-10' < '-2' as strings.
        stamps = [
            "20261006T120000000000Z-2",
            "20261006T120000000000Z",
            "20261006T120000000000Z-10",
            "20261006T120000000000Z-1",
            "20261006T115959000000Z",
        ]
        for stamp in stamps:
            sink.path.with_name(f"{sink.path.name}.{stamp}.gz").write_bytes(b"")
        ordered = [path.name for path in sink._existing_backups()]
    finally:
        _drain(sink)

    assert ordered == [
        f"{sink.path.name}.20261006T115959000000Z.gz",
        f"{sink.path.name}.20261006T120000000000Z.gz",
        f"{sink.path.name}.20261006T120000000000Z-1.gz",
        f"{sink.path.name}.20261006T120000000000Z-2.gz",
        f"{sink.path.name}.20261006T120000000000Z-10.gz",
    ], ordered


def test_an_idle_period_does_not_cost_a_record_or_a_retention_slot(tmp_path):
    """A deadline that passed while the file was empty must move on.

    Left in the past, it makes the first record after a quiet spell arrive
    already overdue: it is split into a backup of its own, and with
    ``max_backups=0`` the next record truncates it away — written, counted, and
    gone. That is the one failure an audit sink may not have.
    """
    sink = AuditSink(
        tmp_path / "audit.log", max_bytes=0, max_backups=0, interval_seconds=86_400
    )
    sink.start()
    # A deadline that fell due while nothing was being written.
    sink._rotate_at = time.time() - 1
    time.sleep(0.4)  # let the idle poll see it

    for index in range(3):
        sink.emit(json.dumps({"n": index}) + "\n")
    _drain(sink)

    assert _numbered(sink) == [0, 1, 2]
    assert sink.stats["written"] == 3
    assert sink.stats["dropped"] == 0


def test_a_record_queued_as_the_sink_closes_is_still_written(tmp_path):
    """``emit`` can land behind the sentinel; the writer must still drain it."""
    sink = AuditSink(tmp_path / "audit.log", max_bytes=0, max_backups=0)
    sink.start()
    # Post the sentinel first, then a record behind it — the ordering the
    # emit/close race produces, without having to win the race.
    sink._queue.put_nowait(_SENTINEL)
    sink._queue.put_nowait(json.dumps({"n": 7}) + "\n")
    sink.close(timeout=10.0)

    assert _numbered(sink) == [7]


@pytest.mark.parametrize(
    ("name", "max_bytes", "interval", "max_backups", "records", "expected_files"),
    [
        # PRD case 4: 10 MB a file, 9 backups -> 100 MB across ten files.
        ("case4", 2, 0, 9, 40, 10),
        # PRD case 5 / 6 shape: interval only, retention by count.
        ("case5", 0, 86_400, 3, 12, 4),
    ],
)
def test_prd_retention_cases_end_to_end(
    tmp_path, name, max_bytes, interval, max_backups, records, expected_files
):
    """The configuration cases as the sink actually behaves, not as arithmetic.

    ``test_audit_config`` checks that each case resolves to the right numbers;
    this checks that those numbers produce the right files on disk, with the
    most recent records in them.
    """
    line = json.dumps({"n": 0}) + "\n"
    sink = AuditSink(
        tmp_path / f"{name}.log",
        max_bytes=len(line) * max_bytes if max_bytes else 0,
        max_backups=max_backups,
        interval_seconds=interval,
    )
    sink.start()
    for index in range(records):
        sink.emit(json.dumps({"n": index}) + "\n")
        if interval:
            # Force the interval trigger rather than waiting a day for it.
            _settle(sink, timeout=0.5)
            sink._rotate_at = time.time() - 1
    _drain(sink)

    assert len(_backups(sink)) + 1 == expected_files
    survivors = _numbered(sink)
    assert survivors[-1] == records - 1, "the newest record was not retained"
    assert survivors == list(range(survivors[0], records)), survivors


def test_a_glob_metacharacter_in_the_path_does_not_disable_retention(tmp_path):
    """``max_backups`` must bound the directory whatever the path looks like.

    The live filename becomes a glob pattern when backups are listed. Left
    unescaped, a path like ``audit[1].log`` reads ``[1]`` as a character class,
    matches nothing, and pruning silently stops — the directory grows without
    limit while the setting claims to bound it, and the operator has no signal.
    """
    line = json.dumps({"n": 0}) + "\n"
    sink = AuditSink(tmp_path / "audit[1].log", max_bytes=len(line) * 2, max_backups=2)
    sink.start()
    for index in range(20):
        sink.emit(json.dumps({"n": index}) + "\n")
    _drain(sink)

    assert len(sink._existing_backups()) > 0, "the sink cannot see its own backups"
    assert len(_backups(sink)) == 2, [p.name for p in _backups(sink)]


def test_a_failed_flush_is_counted_as_lost_not_written(tmp_path):
    """``dropped`` is documented as the only way an operator learns of a gap.

    ``write`` only fills the stream's buffer; a full disk fails at ``flush``.
    Counting a record as written when it was buffered meant the most likely
    real failure reported a clean audit trail — ``written`` climbing, ``dropped``
    at zero — for records that never reached the disk.
    """

    class FailingFlush:
        """Buffers writes happily, fails to flush, like ENOSPC."""

        closed = False

        def __init__(self) -> None:
            self.written: list[str] = []

        def write(self, text: str) -> None:
            self.written.append(text)

        def flush(self) -> None:
            raise OSError("No space left on device")

        def close(self) -> None:
            self.closed = True

    sink = AuditSink(tmp_path / "a.log", max_bytes=0, max_backups=0)
    sink._stream.close()
    sink._stream = FailingFlush()  # type: ignore[assignment]
    sink.start()
    for index in range(5):
        sink.emit(json.dumps({"n": index}) + "\n")
    _settle(sink)
    stats = dict(sink.stats)
    sink.close(timeout=5.0)

    assert stats["written"] == 0, f"buffered records reported as written: {stats}"
    assert stats["dropped"] == 5, stats
    assert stats["write_errors"] >= 1, stats


def test_records_abandoned_at_shutdown_are_counted(tmp_path):
    """A wedged destination must not make an audit gap invisible.

    ``close`` gives the writer a bounded time to drain, which a wedged disk or
    an un-drained console pipe will exceed. Whatever is still queued is lost;
    the counters have to say so, because ``dropped`` is the only signal an
    operator has.
    """
    release = threading.Event()

    class StalledStream(io.StringIO):
        def write(self, text):
            release.wait(timeout=30)
            return super().write(text)

    sink = ConsoleAuditSink(StalledStream(), queue_size=256)
    sink.start()
    try:
        for index in range(200):
            sink.emit(json.dumps({"n": index}) + "\n")
        sink.close(timeout=0.3)  # the writer is stuck in write()
        stats = sink.stats
    finally:
        release.set()

    assert stats["dropped"] > 0, f"abandoned records went uncounted: {stats}"
    # The batch the writer is stuck inside is genuinely undecided — it may yet
    # land — so the counters must not claim it either way. Everything still in
    # the queue, which is the bulk of it, is counted.
    assert stats["dropped"] >= 100, stats
    assert stats["written"] + stats["dropped"] <= 200, (
        f"counters over-report what was handled: {stats}"
    )


def test_records_queued_before_start_are_not_silently_discarded(tmp_path):
    """``emit`` before ``start`` used to be a no-op that lost records quietly."""
    stream = io.StringIO()
    sink = ConsoleAuditSink(stream)
    # Deliberately no start().
    sink.emit('{"early":1}\n')
    sink.close(timeout=5.0)

    assert stream.getvalue() == '{"early":1}\n'
    assert sink.stats["written"] == 1
    assert sink.stats["dropped"] == 0
