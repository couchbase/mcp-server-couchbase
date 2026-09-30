"""Tests for the audit sink.

The sink deliberately does not use ``logging.handlers.RotatingFileHandler``.
A single stdio deployment runs one server process per MCP client, and they all
read the same ``CB_MCP_AUDIT_FILE``; sharing one rotating file between them
produces interleaved partial lines and rotation races. Each process therefore
writes its own file, with the pid inserted before the extension.

Coverage map:
- per-process filename derivation, with and without a suffix
- lines are written and flushed by the background thread
- rotation at the size threshold, and backup shifting
- backup_count=0 truncates rather than growing without limit
- retention: files beyond backup_count are removed
- an oversized single line is written rather than lost
- constructor validation
- drop counter when the queue is full
- close() is idempotent and drains
- two concurrent sinks never share a file
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cb_mcp.audit.sink import AuditSink, process_scoped_path


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
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, backup_count=1)
    try:
        assert sink.path == process_scoped_path(tmp_path / "audit.log")
        assert sink.path.exists()
    finally:
        _drain(sink)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def test_lines_are_written_and_flushed(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=1_000_000, backup_count=1)
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

    sink = AuditSink(tmp_path / "audit.log", max_bytes=1_000_000, backup_count=1)
    sink.start()
    sink.emit('{"post":true}\n')
    _drain(sink)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"pre": True}
    assert json.loads(lines[1]) == {"post": True}


# ---------------------------------------------------------------------------
# rotation and retention
# ---------------------------------------------------------------------------


def test_rotation_shifts_backups_and_honours_retention(tmp_path):
    line = json.dumps({"payload": "x" * 80}) + "\n"
    # Room for roughly two lines before rotating.
    sink = AuditSink(tmp_path / "audit.log", max_bytes=len(line) * 2, backup_count=2)
    sink.start()
    for _ in range(12):
        sink.emit(line)
    _drain(sink)

    live = sink.path
    first = live.with_name(live.name + ".1")
    second = live.with_name(live.name + ".2")
    third = live.with_name(live.name + ".3")

    assert live.exists()
    assert first.exists()
    assert second.exists()
    # Retention is two backups, so a third must never appear.
    assert not third.exists()
    for candidate in (live, first, second):
        assert candidate.stat().st_size <= len(line) * 2


def test_backup_count_zero_truncates_the_live_file(tmp_path):
    line = json.dumps({"payload": "y" * 80}) + "\n"
    sink = AuditSink(tmp_path / "audit.log", max_bytes=len(line) * 2, backup_count=0)
    sink.start()
    for _ in range(20):
        sink.emit(line)
    _drain(sink)

    # Retention of zero still means the rotation size caps the live file.
    assert sink.path.stat().st_size <= len(line) * 2
    assert not sink.path.with_name(sink.path.name + ".1").exists()


def test_a_single_oversized_line_is_written_not_dropped(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=64, backup_count=1)
    sink.start()
    huge = json.dumps({"payload": "z" * 500}) + "\n"
    sink.emit(huge)
    _drain(sink)

    written = sink.path.read_text(encoding="utf-8") + "".join(
        p.read_text(encoding="utf-8")
        for p in tmp_path.iterdir()
        if p.name.startswith(sink.path.name + ".")
    )
    # Losing the record would be worse than briefly exceeding the size cap.
    assert "z" * 500 in written
    assert sink.stats["dropped"] == 0


# ---------------------------------------------------------------------------
# validation and failure handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("max_bytes", "backup_count", "match"),
    [
        (0, 1, "max_bytes must be positive"),
        (-1, 1, "max_bytes must be positive"),
        (10, -1, "backup_count must not be negative"),
    ],
)
def test_constructor_validates_its_arguments(tmp_path, max_bytes, backup_count, match):
    with pytest.raises(ValueError, match=match):
        AuditSink(
            tmp_path / "audit.log", max_bytes=max_bytes, backup_count=backup_count
        )


def test_parent_directories_are_created(tmp_path):
    nested = tmp_path / "deep" / "deeper" / "audit.log"
    sink = AuditSink(nested, max_bytes=4096, backup_count=1)
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
        AuditSink(tmp_path / "audit.log", max_bytes=4096, backup_count=1)


def test_full_queue_drops_and_counts_rather_than_blocking(tmp_path):
    # No writer thread started, so nothing drains the queue.
    sink = AuditSink(
        tmp_path / "audit.log", max_bytes=1_000_000, backup_count=1, queue_size=4
    )
    for _ in range(10):
        sink.emit('{"x":1}\n')
    assert sink.stats["dropped"] == 6
    _drain(sink)


def test_emit_after_close_is_counted_not_raised(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, backup_count=1)
    sink.start()
    _drain(sink)
    sink.emit('{"late":true}\n')
    assert sink.stats["dropped"] == 1


def test_close_is_idempotent(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, backup_count=1)
    sink.start()
    sink.close()
    sink.close()  # must not raise


def test_start_is_idempotent(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, backup_count=1)
    sink.start()
    sink.start()
    try:
        threads = [t for t in threading.enumerate() if t.name == "cb-mcp-audit-writer"]
        assert len(threads) == 1
    finally:
        _drain(sink)


def test_emit_from_many_threads_loses_nothing(tmp_path):
    """The producer side must be safe to call from any thread."""
    sink = AuditSink(tmp_path / "audit.log", max_bytes=10_000_000, backup_count=1)
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
    sink = AuditSink(tmp_path / "a.log", max_bytes=200, backup_count=0)
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
    sink = AuditSink(tmp_path / "b.log", max_bytes=10_000, backup_count=1, queue_size=4)
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
    sink = AuditSink(tmp_path / "c.log", max_bytes=100_000, backup_count=1)
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
