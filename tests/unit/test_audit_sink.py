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
from pathlib import Path

import pytest

from cb_mcp.audit.sink import AuditSink, process_scoped_path


def _drain(sink: AuditSink) -> None:
    """Close the sink so the writer thread finishes and flushes."""
    sink.close(timeout=10.0)


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


def test_process_scoped_path_inserts_pid_before_the_suffix():
    result = process_scoped_path("/var/log/audit.log", pid=4321)
    assert result == Path("/var/log/audit.4321.log")


def test_process_scoped_path_appends_when_there_is_no_suffix():
    result = process_scoped_path("/var/log/audit", pid=4321)
    assert result == Path("/var/log/audit.4321")


def test_process_scoped_path_uses_the_live_pid_by_default():
    assert str(os.getpid()) in process_scoped_path("audit.log").name


def test_sink_writes_to_the_process_scoped_path(tmp_path):
    sink = AuditSink(tmp_path / "audit.log", max_bytes=4096, backup_count=1)
    try:
        assert sink.path == tmp_path / f"audit.{os.getpid()}.log"
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
    path = tmp_path / f"audit.{os.getpid()}.log"
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
    collision = tmp_path / f"audit.{os.getpid()}.log"
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
