"""Tests for configure_logging end-to-end behaviour.

The Couchbase SDK's ``configure_logging`` is one-shot per process (it raises
``InvalidArgumentException`` on a second call), so we patch
:func:`cb_mcp.utils.logging.couchbase.configure_logging` for every test. Each
test also restores the ``couchbase`` logger and the module-level snapshot
afterwards via an autouse fixture, so tests don't bleed state into one another.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

import pytest

import cb_mcp.utils.logging as logmod
from cb_mcp.utils.constants import MCP_SERVER_NAME
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
    """Helper that fills in the boilerplate arguments."""
    configure_logging(
        level=level,
        sinks=sinks if sinks is not None else {"stderr"},
        log_file=log_file,
        log_max_bytes=kwargs.pop("log_max_bytes", 1024),
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
        # Nothing could attach, so no per-level files recorded.
        assert snap is not None and not snap.log_files

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
            log_max_bytes=42,
            log_backup_count=3,
        )
        d = cfg.as_dict()
        # JSON-friendly key names
        assert set(d.keys()) == {
            "level",
            "sinks",
            "log_files",
            "max_bytes",
        }
        assert "backup_count" not in d

    def test_sinks_serialised_as_list(self):
        cfg = ResolvedLoggingConfig(
            level="INFO",
            sinks=("file", "stderr"),
            log_files={"INFO": "m.info.log", "ERROR": "e.log"},
            log_max_bytes=1,
            log_backup_count=1,
        )
        d = cfg.as_dict()
        assert d["sinks"] == ["file", "stderr"]
        assert d["log_files"] == {"INFO": "m.info.log", "ERROR": "e.log"}


class TestIdempotency:
    """configure_logging can be called multiple times without accumulating handlers."""

    def test_handlers_not_duplicated_on_second_call(self):
        _call(sinks={"stderr"})
        first_count = len(logging.getLogger(MCP_SERVER_NAME).handlers)
        _call(sinks={"stderr"})
        second_count = len(logging.getLogger(MCP_SERVER_NAME).handlers)
        assert first_count == second_count == 1
