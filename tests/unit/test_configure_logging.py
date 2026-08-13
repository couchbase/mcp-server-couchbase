"""Tests for configure_logging end-to-end behaviour.

The Couchbase SDK's ``configure_logging`` is one-shot per process (it raises
``InvalidArgumentException`` on a second call), so we patch
:func:`cb_mcp.utils.logging.couchbase.configure_logging` for every test. Each
test also restores the ``couchbase`` logger and the module-level snapshot
afterwards via an autouse fixture, so tests don't bleed state into one another.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

import pytest

import cb_mcp.utils.logging as logmod
from cb_mcp.utils.constants import (
    BYTES_PER_MB,
    DEFAULT_LOG_MAX_BYTES,
    MCP_SERVER_NAME,
)
from cb_mcp.utils.logging import (
    LEVEL_OFF,
    ResolvedLoggingConfig,
    configure_logging,
    get_resolved_logging_config,
)


@pytest.fixture(autouse=True)
def reset_logging_state():
    """Restore the couchbase logger and the resolved-config singleton."""
    yield
    logger = logging.getLogger(MCP_SERVER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    logmod._resolved_config = None


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    """Couchbase SDK ``configure_logging`` is one-shot per process; mock it.

    The patch target is the ``couchbase`` symbol *as imported into our logging
    module* — patching ``couchbase.configure_logging`` directly wouldn't catch
    references already resolved at module load time.
    """
    with patch.object(logmod.couchbase, "configure_logging") as mock:
        yield mock


def _call(level="INFO", sinks=None, log_file="m.log", **kwargs):
    """Helper that fills in the boilerplate arguments.

    Size is left unset by default, so configure_logging applies the effective
    1 MB default. Tests that exercise rotation pass an explicit size — either
    ``log_rotation_max_size_mb`` (MB, canonical) or ``log_max_bytes`` (bytes,
    deprecated, handy for byte-granular rollover thresholds).
    """
    configure_logging(
        level=level,
        sinks=sinks if sinks is not None else {"stderr"},
        log_file=log_file,
        log_backup_count=kwargs.pop("log_backup_count", 1),
        **kwargs,
    )


class TestStderrSinkHandlerAttachment:
    """Default sinks={'stderr'} attaches exactly one handler to the couchbase logger."""

    def test_attaches_single_stream_handler(self):
        _call(sinks={"stderr"})
        logger = logging.getLogger(MCP_SERVER_NAME)
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.StreamHandler)

    def test_propagate_false_to_avoid_root_double_emit(self):
        _call(sinks={"stderr"})
        logger = logging.getLogger(MCP_SERVER_NAME)
        assert logger.propagate is False

    def test_level_set_on_logger(self):
        _call(level="DEBUG", sinks={"stderr"})
        logger = logging.getLogger(MCP_SERVER_NAME)
        assert logger.level == logging.DEBUG


class TestPerLevelFileSink:
    """File sink attaches one rotating file handler per active log level."""

    def test_attaches_one_file_per_active_level_at_info(self, tmp_path):
        # At INFO threshold the active level files are INFO/WARNING/ERROR
        # (CRITICAL shares the ERROR file, so it gets no file of its own).
        _call(level="INFO", sinks={"file"}, log_file=str(tmp_path / "main.log"))
        logger = logging.getLogger(MCP_SERVER_NAME)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 3

    def test_attaches_one_file_per_active_level_at_debug(self, tmp_path):
        # At DEBUG threshold the active level files are DEBUG/INFO/WARNING/ERROR.
        _call(level="DEBUG", sinks={"file"}, log_file=str(tmp_path / "main.log"))
        logger = logging.getLogger(MCP_SERVER_NAME)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 4

    def test_trace_records_fold_into_debug_file(self, tmp_path):
        # TRACE has no file of its own: at TRACE the same four files attach as at
        # DEBUG, and TRACE records land in the DEBUG file.
        _call(level="TRACE", sinks={"file"}, log_file=str(tmp_path / "main.log"))
        logger = logging.getLogger(MCP_SERVER_NAME)
        assert logger.level == logmod.LEVEL_TRACE
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 4
        snap = get_resolved_logging_config()
        assert snap is not None
        assert set(snap.log_files or {}) == {"DEBUG", "INFO", "WARNING", "ERROR"}

        # A TRACE record must be written to the DEBUG file and no other level file.
        logger.log(logmod.LEVEL_TRACE, "trace-marker")
        for h in rotating:
            h.flush()
        assert "trace-marker" in (tmp_path / "main.debug.log").read_text()
        assert "trace-marker" not in (tmp_path / "main.info.log").read_text()

    def test_levels_below_threshold_get_no_file(self, tmp_path):
        # At WARNING threshold, DEBUG/INFO files must not be created.
        _call(level="WARNING", sinks={"file"}, log_file=str(tmp_path / "main.log"))
        logger = logging.getLogger(MCP_SERVER_NAME)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 2  # WARNING/ERROR (CRITICAL folds into ERROR)
        snap = get_resolved_logging_config()
        assert snap is not None
        assert set(snap.log_files or {}) == {"WARNING", "ERROR"}

    def test_critical_records_routed_to_error_file(self, tmp_path):
        """There is no CRITICAL file; CRITICAL records land in the ERROR file
        (the error file is derived from the base path: main.log -> main.error.log)."""
        _call(level="DEBUG", sinks={"file"}, log_file=str(tmp_path / "main.log"))
        # No dedicated critical file is tracked.
        snap = get_resolved_logging_config()
        assert snap is not None
        assert "CRITICAL" not in (snap.log_files or {})

        log = logging.getLogger(f"{MCP_SERVER_NAME}.test")
        log.critical("a-critical")
        for h in logging.getLogger(MCP_SERVER_NAME).handlers:
            h.flush()
        assert "a-critical" in (tmp_path / "main.error.log").read_text()
        assert not (tmp_path / "main.critical.log").exists()

    def test_each_handler_filters_to_exactly_its_level(self, tmp_path):
        _call(level="DEBUG", sinks={"file"}, log_file=str(tmp_path / "main.log"))
        logger = logging.getLogger(MCP_SERVER_NAME)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        info_rec = logging.LogRecord("x", logging.INFO, "f", 1, "i", None, None)
        warn_rec = logging.LogRecord("x", logging.WARNING, "f", 1, "w", None, None)

        def _would_emit(handler, record):
            # A handler emits a record only if it clears both the handler level
            # (setLevel, used by the ERROR file) and its filters (exact-level,
            # used by the others).
            return record.levelno >= handler.level and handler.filter(record)

        # Exactly one handler emits INFO, and it does not emit WARNING.
        accepting_info = [h for h in rotating if _would_emit(h, info_rec)]
        assert len(accepting_info) == 1
        assert not _would_emit(accepting_info[0], warn_rec)

    def test_all_files_including_error_derived_from_base_path(self, tmp_path):
        _call(level="DEBUG", sinks={"file"}, log_file=str(tmp_path / "mcp_server.log"))
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_files["DEBUG"] == str(tmp_path / "mcp_server.debug.log")
        assert snap.log_files["INFO"] == str(tmp_path / "mcp_server.info.log")
        assert snap.log_files["WARNING"] == str(tmp_path / "mcp_server.warning.log")
        # The ERROR file is derived from the same base, not a separate path.
        assert snap.log_files["ERROR"] == str(tmp_path / "mcp_server.error.log")

    def test_records_routed_to_their_own_level_file(self, tmp_path):
        """End-to-end: each level's record lands only in its own file."""
        _call(level="DEBUG", sinks={"file"}, log_file=str(tmp_path / "mcp_server.log"))
        log = logging.getLogger(f"{MCP_SERVER_NAME}.test")
        log.info("an-info")
        log.warning("a-warning")
        log.error("an-error")

        for h in logging.getLogger(MCP_SERVER_NAME).handlers:
            h.flush()

        info_text = (tmp_path / "mcp_server.info.log").read_text()
        warn_text = (tmp_path / "mcp_server.warning.log").read_text()
        err_text = (tmp_path / "mcp_server.error.log").read_text()
        assert "an-info" in info_text and "a-warning" not in info_text
        assert "a-warning" in warn_text and "an-error" not in warn_text
        assert "an-error" in err_text and "an-info" not in err_text


class TestStderrAndFileTogether:
    """sinks={'stderr', 'file'} attaches stderr plus one file per active level."""

    def test_stderr_plus_per_level_files(self, tmp_path):
        # INFO threshold: stderr + INFO/WARNING/ERROR files = 4 handlers
        # (CRITICAL shares the ERROR file).
        _call(
            level="INFO",
            sinks={"stderr", "file"},
            log_file=str(tmp_path / "m.log"),
        )
        logger = logging.getLogger(MCP_SERVER_NAME)
        assert len(logger.handlers) == 4


class TestFileSinkEdgeCases:
    """Permission failures, missing paths, and the disabled-file warning."""

    def test_missing_path_falls_back_to_default_with_error(
        self, tmp_path, monkeypatch, capsys
    ):
        # Empty log_file with the file sink: error logged, default path used.
        monkeypatch.chdir(tmp_path)
        _call(level="INFO", sinks={"stderr", "file"}, log_file="")
        err = capsys.readouterr().err
        assert "no --log-file" in err
        assert "falling back to default" in err
        snap = get_resolved_logging_config()
        assert snap is not None and snap.log_files  # fallback files attached

    def test_unwritable_path_logged_as_error(self, tmp_path, capsys):
        # A base path under a non-existent directory can't be opened; the file
        # sink is the only sink, so a stderr fallback must surface the error.
        missing_dir = tmp_path / "nope"
        _call(level="INFO", sinks={"file"}, log_file=str(missing_dir / "main.log"))
        err = capsys.readouterr().err
        assert "Cannot write" in err
        snap = get_resolved_logging_config()
        # Nothing could attach, so no per-level files recorded — and no env file
        # is advertised either (writing it to the same bad dir would fail too).
        assert snap is not None and not snap.log_files
        assert snap.server_config_file is None

    def test_partial_failure_error_surfaces_on_stderr(self, tmp_path, capsys):
        """If one level's file fails but others succeed, and stderr isn't a sink,
        the 'Cannot write' error must still be visible (a stderr fallback is
        added when no ERROR-capable handler attached). We force the ERROR file
        to fail by pre-creating a *directory* at its derived path."""
        # base main.log -> ERROR file derives to main.error.log; make that a dir.
        (tmp_path / "main.error.log").mkdir()
        _call(level="INFO", sinks={"file"}, log_file=str(tmp_path / "main.log"))
        err = capsys.readouterr().err
        assert "Cannot write ERROR log file" in err
        snap = get_resolved_logging_config()
        assert snap is not None
        # INFO/WARNING attached; ERROR did not.
        assert "INFO" in (snap.log_files or {})
        assert "ERROR" not in (snap.log_files or {})

    def test_warning_when_file_sink_not_enabled(self, capsys):
        _call(level="INFO", sinks={"stderr"})
        err = capsys.readouterr().err
        assert (
            "WARNING: File logging is disabled. Log files required for product support "
            "are not being generated."
        ) in err


class TestOffMode:
    """OFF level attaches no handlers, sets sentinel level, records snapshot."""

    def test_no_handlers_attached(self):
        _call(level="OFF", sinks={"stderr", "file"})
        logger = logging.getLogger(MCP_SERVER_NAME)
        assert logger.handlers == []

    def test_logger_level_set_to_sentinel(self):
        _call(level="OFF")
        logger = logging.getLogger(MCP_SERVER_NAME)
        assert logger.level == LEVEL_OFF

    def test_sdk_called_with_sentinel(self, mock_sdk_configure_logging):
        _call(level="OFF")
        # SDK is told OFF too — drops records at the C++ boundary.
        mock_sdk_configure_logging.assert_called_with(MCP_SERVER_NAME, LEVEL_OFF)

    def test_snapshot_reflects_inactive_state(self):
        _call(level="OFF", sinks={"stderr", "file"})
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.level == "OFF"
        assert snap.sinks == ()
        assert snap.log_files is None


class TestSdkLevelPropagation:
    """The Couchbase SDK is configured with the same level as the MCP logger.

    PRD Req 4: "The SDK logs should also be configured with whatever
    configuration is set up for the MCP server." ``TestOffMode`` already pins
    the OFF/sentinel case; these pin the normal levels so the MCP and SDK log
    trees never drift to different thresholds.
    """

    def test_sdk_configured_with_matching_debug_level(self, mock_sdk_configure_logging):
        _call(level="DEBUG", sinks={"stderr"})
        mock_sdk_configure_logging.assert_called_with(MCP_SERVER_NAME, logging.DEBUG)

    def test_sdk_configured_with_trace_level(self, mock_sdk_configure_logging):
        # The whole point of trace: the SDK's C++ core level is set to 5 so its
        # logs surface through the MCP server.
        _call(level="TRACE", sinks={"stderr"})
        mock_sdk_configure_logging.assert_called_with(
            MCP_SERVER_NAME, logmod.LEVEL_TRACE
        )

    def test_sdk_configured_with_matching_warning_level(
        self, mock_sdk_configure_logging
    ):
        _call(level="WARNING", sinks={"stderr"})
        mock_sdk_configure_logging.assert_called_with(MCP_SERVER_NAME, logging.WARNING)

    def test_sdk_level_tracks_invalid_level_fallback(self, mock_sdk_configure_logging):
        """An invalid level falls back to INFO for the MCP logger; the SDK must
        be told the same resolved level, not the rejected input."""
        _call(level="NONSENSE", sinks={"stderr"})
        mock_sdk_configure_logging.assert_called_with(MCP_SERVER_NAME, logging.INFO)


class TestTimestampFormat:
    """Emitted records carry a timezone-aware timestamp.

    PRD Req 4: "The timestamp should have the timezone." This asserts the
    formatter actually wired onto the handlers produces an ISO-8601 timestamp
    with a UTC offset (e.g. ``2026-06-19T16:20:25+0530``), rather than just
    trusting the format-string constant.
    """

    # ISO-8601 local time followed by a +HHMM / -HHMM UTC offset.
    _TS_WITH_TZ = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}")

    def test_stderr_handler_timestamp_includes_timezone_offset(self):
        _call(level="INFO", sinks={"stderr"})
        logger = logging.getLogger(MCP_SERVER_NAME)
        formatter = logger.handlers[0].formatter
        assert formatter is not None
        record = logging.LogRecord(
            MCP_SERVER_NAME, logging.INFO, "f", 1, "hello", None, None
        )
        formatted = formatter.format(record)
        assert self._TS_WITH_TZ.search(formatted), (
            f"timestamp is missing a timezone offset:\n{formatted}"
        )

    def test_file_handler_timestamp_includes_timezone_offset(self, tmp_path):
        _call(level="INFO", sinks={"file"}, log_file=str(tmp_path / "m.log"))
        log = logging.getLogger(f"{MCP_SERVER_NAME}.test")
        log.info("an-info")
        for h in logging.getLogger(MCP_SERVER_NAME).handlers:
            h.flush()
        info_line = (tmp_path / "m.info.log").read_text()
        assert self._TS_WITH_TZ.search(info_line), (
            f"file timestamp is missing a timezone offset:\n{info_line}"
        )


class TestLenientLevelFallback:
    """Invalid `level` argument falls back to DEFAULT_LOG_LEVEL, doesn't raise."""

    def test_invalid_level_does_not_raise(self):
        _call(level="VERBOSE")  # not in ALLOWED_LOG_LEVELS

    def test_invalid_level_falls_back_to_default(self):
        _call(level="VERBOSE")
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.level == "INFO"

    def test_invalid_level_emits_deferred_error_record(self, capsys):
        """The error record fires only after handlers are wired so it's visible.

        We capture stderr directly because ``configure_logging`` sets
        ``propagate = False`` on the ``couchbase`` logger; pytest's ``caplog``
        hooks into the root logger by default and wouldn't see records that
        don't propagate.
        """
        _call(level="NONSENSE", sinks={"stderr"})
        captured = capsys.readouterr()
        assert "NONSENSE" in captured.err
        assert "Ignored invalid log level" in captured.err


class TestSnapshot:
    """ResolvedLoggingConfig snapshot reflects the active state."""

    def test_snapshot_populated_after_call(self):
        _call(level="DEBUG", sinks={"stderr"})
        snap = get_resolved_logging_config()
        assert snap is not None
        assert isinstance(snap, ResolvedLoggingConfig)
        assert snap.level == "DEBUG"
        assert snap.sinks == ("stderr",)
        assert snap.log_files is None

    def test_file_paths_visible_only_when_file_sink_active(self, tmp_path):
        # User passed a path but only stderr sink; paths should NOT appear in snapshot.
        _call(sinks={"stderr"}, log_file=str(tmp_path / "m.log"))
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_files is None

    def test_sinks_sorted_for_deterministic_output(self, tmp_path):
        _call(sinks={"stderr", "file"}, log_file=str(tmp_path / "m.log"))
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.sinks == ("file", "stderr")  # sorted alphabetically


class TestAsDict:
    """ResolvedLoggingConfig.as_dict shape and field naming."""

    def test_keys_match_documented_shape(self):
        cfg = ResolvedLoggingConfig(
            level="DEBUG",
            sinks=("stderr",),
            log_files=None,
            log_max_bytes=None,
            log_backup_counts=None,
            server_config_file=None,
        )
        d = cfg.as_dict()
        # JSON-friendly key names
        assert set(d.keys()) == {
            "level",
            "sinks",
            "log_files",
            "max_bytes",
            "backup_counts",
            "server_config_file",
        }
        # The serialised key is the plural, per-level form.
        assert "backup_count" not in d

    def test_sinks_serialised_as_list(self):
        cfg = ResolvedLoggingConfig(
            level="INFO",
            sinks=("file", "stderr"),
            log_files={"INFO": "m.info.log", "ERROR": "e.log"},
            log_max_bytes={"INFO": 1048576, "ERROR": 5242880},
            log_backup_counts={"INFO": 1, "ERROR": 5},
            server_config_file="m_config.log.json",
        )
        d = cfg.as_dict()
        assert d["sinks"] == ["file", "stderr"]
        assert d["log_files"] == {"INFO": "m.info.log", "ERROR": "e.log"}
        assert d["max_bytes"] == {"INFO": 1048576, "ERROR": 5242880}
        assert d["backup_counts"] == {"INFO": 1, "ERROR": 5}
        assert d["server_config_file"] == "m_config.log.json"


class TestIdempotency:
    """configure_logging can be called multiple times without accumulating handlers."""

    def test_handlers_not_duplicated_on_second_call(self):
        _call(sinks={"stderr"})
        first_count = len(logging.getLogger(MCP_SERVER_NAME).handlers)
        _call(sinks={"stderr"})
        second_count = len(logging.getLogger(MCP_SERVER_NAME).handlers)
        assert first_count == second_count == 1


class TestPerLevelMaxBytes:
    """Per-level rotation size: global inheritance, MB overrides, 0-is-invalid."""

    _ALL_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

    # Signature: _resolve_per_level_max_bytes(rotation_max_size_mb, max_bytes, overrides_mb)

    def test_resolve_inherits_global_mb_when_no_overrides(self):
        # 2 MB global, no per-level overrides -> every level = 2 MB in bytes.
        per_level, warnings = logmod._resolve_per_level_max_bytes(2, None, {})
        assert per_level == dict.fromkeys(self._ALL_LEVELS, 2 * BYTES_PER_MB)
        assert warnings == []

    def test_resolve_converts_mb_overrides_to_bytes(self):
        per_level, warnings = logmod._resolve_per_level_max_bytes(
            1, None, {"ERROR": 5, "DEBUG": 2}
        )
        assert per_level["ERROR"] == 5 * BYTES_PER_MB
        assert per_level["DEBUG"] == 2 * BYTES_PER_MB
        # Unset levels inherit the global (1 MB).
        assert per_level["INFO"] == 1 * BYTES_PER_MB
        assert per_level["WARNING"] == 1 * BYTES_PER_MB
        assert warnings == []

    def test_deprecated_max_bytes_used_when_canonical_unset(self):
        # Only the deprecated bytes var set -> used as-is (bytes) + deprecation.
        per_level, warnings = logmod._resolve_per_level_max_bytes(None, 4096, {})
        assert all(v == 4096 for v in per_level.values())
        assert any("CB_MCP_LOG_MAX_BYTES is deprecated" in w for w in warnings)

    def test_canonical_wins_when_both_set(self):
        # Canonical MB wins over deprecated bytes; a warning notes MAX_BYTES ignored.
        per_level, warnings = logmod._resolve_per_level_max_bytes(3, 4096, {})
        assert all(v == 3 * BYTES_PER_MB for v in per_level.values())
        assert any(
            "ignoring the deprecated CB_MCP_LOG_MAX_BYTES" in w for w in warnings
        )

    def test_default_when_neither_global_set(self):
        per_level, warnings = logmod._resolve_per_level_max_bytes(None, None, {})
        assert all(v == DEFAULT_LOG_MAX_BYTES for v in per_level.values())
        assert warnings == []

    def test_resolve_global_zero_falls_back_to_default_with_warning(self):
        per_level, warnings = logmod._resolve_per_level_max_bytes(0, None, {})
        assert all(v == DEFAULT_LOG_MAX_BYTES for v in per_level.values())
        assert any("CB_MCP_LOG_ROTATION_MAX_SIZE_MB=0" in w for w in warnings)

    def test_deprecated_max_bytes_zero_falls_back_to_default_with_warning(self):
        # The deprecated bytes var at 0 is also invalid -> falls back to default.
        per_level, warnings = logmod._resolve_per_level_max_bytes(None, 0, {})
        assert all(v == DEFAULT_LOG_MAX_BYTES for v in per_level.values())
        assert any("CB_MCP_LOG_MAX_BYTES=0" in w for w in warnings)

    def test_resolve_per_level_zero_inherits_global_with_warning(self):
        # Global 2 MB; ERROR override of 0 -> invalid -> inherits the 2 MB global.
        per_level, warnings = logmod._resolve_per_level_max_bytes(2, None, {"ERROR": 0})
        assert per_level["ERROR"] == 2 * BYTES_PER_MB
        assert any("CB_MCP_LOG_ERROR_ROTATION_MAX_SIZE_MB=0" in w for w in warnings)

    def test_resolve_fractional_mb_rounds_to_bytes(self):
        # Fractional MB is allowed and rounded to the nearest whole byte.
        per_level, warnings = logmod._resolve_per_level_max_bytes(
            0.5, None, {"ERROR": 1.5}
        )
        assert per_level["INFO"] == round(0.5 * BYTES_PER_MB) == 524288
        assert per_level["ERROR"] == round(1.5 * BYTES_PER_MB) == 1572864
        assert warnings == []

    def test_handlers_wired_with_per_level_max_bytes(self, tmp_path):
        _call(
            level="INFO",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_rotation_max_size_mb=1,  # 1 MB global
            log_rotation_size_overrides={"ERROR": 3},  # 3 MB
        )
        logger = logging.getLogger(MCP_SERVER_NAME)
        by_path = {
            h.baseFilename: h.maxBytes
            for h in logger.handlers
            if isinstance(h, RotatingFileHandler)
        }
        assert by_path[str(tmp_path / "m.error.log")] == 3 * BYTES_PER_MB
        assert by_path[str(tmp_path / "m.info.log")] == 1 * BYTES_PER_MB

    def test_snapshot_reports_per_level_bytes(self, tmp_path):
        _call(
            level="DEBUG",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_rotation_max_size_mb=1,  # 1 MB global
            log_rotation_size_overrides={"DEBUG": 2},  # 2 MB
        )
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_max_bytes == {
            "DEBUG": 2 * BYTES_PER_MB,
            "INFO": 1 * BYTES_PER_MB,
            "WARNING": 1 * BYTES_PER_MB,
            "ERROR": 1 * BYTES_PER_MB,
        }

    def test_fractional_mb_converted_to_exact_bytes(self, tmp_path):
        # 0.5 MB -> exactly 524288 bytes on the handler and in the snapshot.
        _call(
            level="INFO",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_rotation_max_size_mb=0.5,
        )
        logger = logging.getLogger(MCP_SERVER_NAME)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert rotating and all(h.maxBytes == 524288 for h in rotating)
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.as_dict()["max_bytes"] == dict.fromkeys(
            ("INFO", "WARNING", "ERROR"), 524288
        )

    def test_deprecated_max_bytes_still_honored_end_to_end(self, tmp_path):
        # The deprecated bytes var still reaches the handlers (byte granularity).
        _call(
            level="INFO",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_max_bytes=4096,
        )
        logger = logging.getLogger(MCP_SERVER_NAME)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert rotating and all(h.maxBytes == 4096 for h in rotating)

    def test_max_bytes_none_when_file_sink_absent(self, tmp_path):
        _call(level="INFO", sinks={"stderr"}, log_file=str(tmp_path / "m.log"))
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_max_bytes is None


class TestPerLevelBackupCounts:
    """Retention: global backup count plus per-level overrides that inherit it."""

    def test_global_applies_to_all_active_levels(self, tmp_path):
        _call(
            level="DEBUG",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_backup_count=4,
        )
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_backup_counts == {
            "DEBUG": 4,
            "INFO": 4,
            "WARNING": 4,
            "ERROR": 4,
        }

    def test_per_level_override_wins_others_inherit(self, tmp_path):
        _call(
            level="DEBUG",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_backup_count=2,
            log_backup_count_overrides={"ERROR": 10, "DEBUG": 0},
        )
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_backup_counts == {
            "DEBUG": 0,  # explicit override
            "INFO": 2,  # inherited global
            "WARNING": 2,  # inherited global
            "ERROR": 10,  # explicit override
        }

    def test_counts_restricted_to_active_levels(self, tmp_path):
        # At WARNING threshold only WARNING/ERROR files exist, so DEBUG/INFO
        # counts do not appear in the snapshot even when overridden.
        _call(
            level="WARNING",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_backup_count=3,
            log_backup_count_overrides={"DEBUG": 9, "ERROR": 7},
        )
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_backup_counts == {"WARNING": 3, "ERROR": 7}

    def test_zero_keeps_no_backups_on_handler(self, tmp_path):
        # backupCount=0 -> the RotatingFileHandler retains only the live file.
        _call(
            level="INFO",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_backup_count=0,
        )
        logger = logging.getLogger(MCP_SERVER_NAME)
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert rotating
        assert all(h.backupCount == 0 for h in rotating)

    def test_per_level_count_reaches_matching_handler(self, tmp_path):
        # The ERROR override must land on the ERROR file's handler specifically,
        # not leak onto the INFO handler.
        _call(
            level="INFO",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_backup_count=2,
            log_backup_count_overrides={"ERROR": 6},
        )
        logger = logging.getLogger(MCP_SERVER_NAME)
        by_path = {
            h.baseFilename: h.backupCount
            for h in logger.handlers
            if isinstance(h, RotatingFileHandler)
        }
        assert by_path[str(tmp_path / "m.error.log")] == 6
        assert by_path[str(tmp_path / "m.info.log")] == 2

    def test_backup_counts_none_when_file_sink_absent(self, tmp_path):
        _call(level="INFO", sinks={"stderr"}, log_file=str(tmp_path / "m.log"))
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.log_backup_counts is None

    def test_zero_keeps_only_live_file_bounded_by_max_bytes(self, tmp_path):
        # backupCount=0 must keep ONLY the live file and cap it at maxBytes by
        # truncating on rollover — not grow unbounded (stock RFH behaviour).
        _call(
            level="ERROR",
            sinks={"file"},
            log_file=str(tmp_path / "m.log"),
            log_max_bytes=2000,
            log_backup_count=0,
        )
        log = logging.getLogger(f"{MCP_SERVER_NAME}.test")
        handlers = logging.getLogger(MCP_SERVER_NAME).handlers
        max_seen = 0
        for i in range(500):
            log.error("x" * 80 + f" {i}")
            for h in handlers:
                h.flush()
            max_seen = max(max_seen, (tmp_path / "m.error.log").stat().st_size)

        # Only the live file exists — no numbered backups were created.
        assert not list(tmp_path.glob("m.error.log.*"))
        # And it stayed bounded (within one record of the cap), proving the file
        # was truncated on rollover rather than growing without limit.
        assert max_seen <= 2000 + 200


class TestEnvFile:
    """The dedicated environment-file path is derived and reported correctly."""

    def test_server_config_file_derived_from_base_when_file_sink_active(self, tmp_path):
        _call(
            level="INFO",
            sinks={"file"},
            log_file=str(tmp_path / "mcp_server.log"),
        )
        snap = get_resolved_logging_config()
        assert snap is not None
        # Derived from the --log-file base by inserting ".env".
        assert snap.server_config_file == str(tmp_path / "mcp_server_config.log.json")

    def test_server_config_file_none_for_stderr_only(self, tmp_path):
        _call(level="INFO", sinks={"stderr"}, log_file=str(tmp_path / "m.log"))
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.server_config_file is None

    def test_server_config_file_none_when_off(self, tmp_path):
        _call(level="OFF", sinks={"file"}, log_file=str(tmp_path / "m.log"))
        snap = get_resolved_logging_config()
        assert snap is not None
        assert snap.server_config_file is None
