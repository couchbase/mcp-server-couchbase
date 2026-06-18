"""Logging configuration for the Couchbase MCP Server.

Centralises handler/formatter wiring so the CLI entrypoint only needs a
single call. All MCP modules log under the ``MCP_SERVER_NAME`` ("couchbase")
logger hierarchy; the Couchbase Python SDK is routed into the same tree via
``couchbase.configure_logging``, which means handlers attached here apply to
SDK records as well.
"""

import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any

import couchbase

from .constants import (
    ALLOWED_LOG_LEVELS,
    ALLOWED_LOG_SINKS,
    DEFAULT_LOG_DATEFMT,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_SINKS,
    MCP_SERVER_NAME,
)

# When the file sink is active, one rotating file is written per log level so
# operators can isolate, e.g., just the errors. Ordered low → high.
_PER_LEVEL_FILE_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Sentinel above CRITICAL used to disable the MCP logger when --log-level=OFF.
# ``Logger.isEnabledFor(level)`` short-circuits before a LogRecord is built when
# the threshold is unreachable, so this is the cheapest way to silence the
# logger without touching other loggers in the process.
LEVEL_OFF = logging.CRITICAL + 1


@dataclass(frozen=True)
class ResolvedLoggingConfig:
    """Snapshot of the active logging configuration after configure_logging().

    Built once per call to :func:`configure_logging` and stashed in a
    module-level singleton so the server-config MCP tool and the env-info
    diagnostic record can both report exactly what's running, without each
    consumer keeping its own view in sync with the CLI flags.

    The fields reflect what's *active*: ``sinks`` lists only the destinations
    that received handler attachments, and ``log_files`` maps each active log
    level to the file it is written to (``{"INFO": "mcp_server.info.log", ...}``).
    ``log_files`` is ``None`` whenever the file sink isn't part of that set —
    including under ``level="OFF"``, where no handlers are attached at all.
    """

    level: str
    sinks: tuple[str, ...]
    log_files: dict[str, str] | None
    log_max_bytes: int
    log_backup_count: int

    def as_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict with shorter key names."""
        return {
            "level": self.level,
            "sinks": list(self.sinks),
            "log_files": dict(self.log_files) if self.log_files else None,
            "max_bytes": self.log_max_bytes,
        }


# Module-level singleton holding the most recent configure_logging() snapshot.
_resolved_config: ResolvedLoggingConfig | None = None


def get_resolved_logging_config() -> ResolvedLoggingConfig | None:
    """Return the snapshot recorded by the last configure_logging() call.

    Returns ``None`` if configure_logging has not yet been invoked in this
    process.
    """
    return _resolved_config


def _exact_level_filter(levelno: int):
    """Return a filter that keeps only records whose level is exactly ``levelno``.

    One file per level means each handler must accept just its own level, so a
    WARNING never lands in the INFO file and vice versa.
    """

    def _filter(record: logging.LogRecord) -> bool:
        return record.levelno == levelno

    return _filter


def _per_level_path(base_path: str, level_name: str) -> str:
    """Insert the level name before the extension of ``base_path``.

    ``mcp_server.log`` + ``INFO`` -> ``mcp_server.info.log``.
    """
    root, ext = os.path.splitext(base_path)
    return f"{root}.{level_name.lower()}{ext}"


def _attach_per_level_file_handlers(
    logger: logging.Logger,
    formatter: logging.Formatter,
    log_file: str,
    log_max_bytes: int,
    log_backup_count: int,
) -> tuple[dict[str, str], list[str]]:
    """Attach one rotating file handler per active level to ``logger``.

    All per-level files derive from the single ``log_file`` base path by
    inserting the level name (``mcp_server.log`` -> ``mcp_server.info.log``,
    ``mcp_server.error.log``, ...). Returns ``(attached, errors)`` where
    ``attached`` maps each level that got a handler to its file path, and
    ``errors`` collects human-readable problems (a missing base path that fell
    back to the default, or a file that couldn't be opened) for the caller to
    log once all handlers are wired and visible.

    A missing ``log_file`` falls back to the package default rather than
    dropping file logging entirely. Only levels at or above the logger's
    threshold get a file — opening an empty debug file under an INFO threshold
    would just be noise.
    """
    errors: list[str] = []
    if not log_file:
        errors.append(
            "File logging enabled but no --log-file/CB_MCP_LOG_FILE configured; "
            f"falling back to default '{DEFAULT_LOG_FILE}'."
        )
        log_file = DEFAULT_LOG_FILE

    attached: dict[str, str] = {}
    for lvl_name in _PER_LEVEL_FILE_LEVELS:
        lvl_no = logging.getLevelName(lvl_name)
        if lvl_no < logger.level:
            continue
        path = _per_level_path(log_file, lvl_name)
        try:
            handler = RotatingFileHandler(
                path,
                maxBytes=log_max_bytes,
                backupCount=log_backup_count,
                encoding="utf-8",
            )
        except OSError as e:
            # e.g. no write permission for the path / its directory.
            errors.append(f"Cannot write {lvl_name} log file '{path}': {e}")
            continue
        handler.setFormatter(formatter)
        if lvl_name == "ERROR":
            # The ERROR file is the catch-all for ERROR and above, so CRITICAL
            # records land here too rather than in a separate file.
            handler.setLevel(logging.ERROR)
        else:
            handler.addFilter(_exact_level_filter(lvl_no))
        logger.addHandler(handler)
        attached[lvl_name] = path
    return attached, errors


def parse_log_level(value: str) -> tuple[str, str | None]:
    """Parse a log level value, falling back to the default for invalid input.

    Returns ``(resolved_level, invalid_input)``. When ``value`` matches one of
    ``ALLOWED_LOG_LEVELS`` (case-insensitive), ``invalid_input`` is ``None``.
    Otherwise the resolved level is ``DEFAULT_LOG_LEVEL`` and the original
    input is returned so the caller can surface it via the logger once
    handlers are wired.
    """
    token = value.strip().upper()
    if token in ALLOWED_LOG_LEVELS:
        return token, None
    return DEFAULT_LOG_LEVEL, value


def parse_log_sinks(value: str) -> tuple[set[str], list[str]]:
    """Parse a comma-separated CB_MCP_LOG_SINKS value.

    Tokens are case-insensitive and whitespace around them is trimmed. Valid
    tokens are accumulated; unknown tokens are collected separately so the
    caller can surface them via the logger once it is configured. If no valid
    tokens survive, the default sink is used so the server still produces
    output.

    Returns a tuple ``(sinks, invalid_tokens)`` where ``sinks`` is a non-empty
    set drawn from ``ALLOWED_LOG_SINKS`` and ``invalid_tokens`` lists any
    rejected tokens in their original case.
    """
    sinks: set[str] = set()
    invalid: list[str] = []
    for part in value.split(","):
        token = part.strip()
        if token:
            normalised = token.lower()
            if normalised in ALLOWED_LOG_SINKS:
                sinks.add(normalised)
            else:
                invalid.append(token)
    if not sinks:
        sinks.add(DEFAULT_LOG_SINKS)
    return sinks, invalid


def configure_logging(
    level: str,
    sinks: set[str],
    log_file: str,
    log_max_bytes: int,
    log_backup_count: int,
    invalid_sinks: list[str] | None = None,
    invalid_level: str | None = None,
) -> None:
    """Configure the root MCP logger and the Couchbase SDK logs.

    The ``sinks`` set is authoritative: ``"stderr"`` attaches a stderr handler.
    ``"file"`` attaches **one rotating file handler per active log level**
    (DEBUG/INFO/WARNING/ERROR at or above the configured threshold). Every
    per-level file derives from the single ``log_file`` base path by inserting
    the level name (``mcp_server.log`` -> ``mcp_server.info.log``,
    ``mcp_server.error.log``, ...). The DEBUG/INFO/WARNING files are filtered to
    exactly their level; the ERROR file captures ERROR **and** CRITICAL (there
    is no separate CRITICAL file).

    File-sink edge cases:
      * If ``"file"`` is requested but ``log_file`` is missing, an error is
        logged and the default base path is used instead.
      * If a level's file can't be opened (e.g. no write permission), an error
        is logged for that path and the other levels still attach.
      * If the file sink is *not* requested, a warning is logged noting that
        support log files are not being generated.

    Setting ``level="OFF"`` suppresses output regardless of sinks.
    """
    # Both code paths below rebind the module-level snapshot.
    global _resolved_config  # noqa: PLW0603

    level_name = level.upper()
    if level_name not in ALLOWED_LOG_LEVELS:
        # Defer logging about the invalid level until after handlers are configured,
        # so the message is visible even when the user sets an unrecognised level.
        # ``DEFAULT_LOG_LEVEL`` is stored lowercase for help-text consistency;
        # uppercase here so ``logger.setLevel`` accepts it.
        invalid_level = level
        level_name = DEFAULT_LOG_LEVEL.upper()

    logger = logging.getLogger(MCP_SERVER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        # Close so RotatingFileHandlers release their file descriptor — otherwise
        # repeated configure_logging() calls (tests, reloads) leak FDs and keep
        # rotated files open against the filesystem.
        handler.close()
    logger.propagate = False

    if level_name == "OFF":
        logger.setLevel(LEVEL_OFF)
        couchbase.configure_logging(MCP_SERVER_NAME, LEVEL_OFF)
        # No handlers attached, no sinks active; record that state so the
        # MCP tool and env-info reflect reality.
        _resolved_config = ResolvedLoggingConfig(
            level=level_name,
            sinks=(),
            log_files=None,
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
        )
        return

    logger.setLevel(level_name)

    formatter = logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=DEFAULT_LOG_DATEFMT)

    effective_sinks = set(sinks)
    file_sink_active = "file" in effective_sinks

    if "stderr" in effective_sinks:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        logger.addHandler(stderr_handler)

    # Deferred so these surface after handlers (incl. stderr) are wired and are
    # therefore actually visible.
    file_warnings: list[str] = []
    file_errors: list[str] = []
    attached_files: dict[str, str] = {}

    if file_sink_active:
        attached_files, file_errors = _attach_per_level_file_handlers(
            logger,
            formatter,
            log_file,
            log_max_bytes,
            log_backup_count,
        )
        # Make sure file errors are actually visible. ERROR-level records are
        # only captured by a stderr handler or the ERROR file; if neither is
        # present (no stderr sink AND the ERROR file itself failed to attach),
        # the "Cannot write" records would be silently dropped. Add a stderr
        # handler in that case so the failure is reported clearly — this covers
        # both total failure and partial failure (only the ERROR file failed).
        no_error_handler = "ERROR" not in attached_files
        if file_errors and no_error_handler and "stderr" not in effective_sinks:
            fallback_handler = logging.StreamHandler(sys.stderr)
            fallback_handler.setFormatter(formatter)
            logger.addHandler(fallback_handler)
    else:
        # Requirement: warn when file logging isn't explicitly enabled so the
        # operator knows support logs aren't being persisted.
        file_warnings.append(
            "WARNING: File logging is disabled. Log files required for product support are not being generated."
        )

    couchbase.configure_logging(MCP_SERVER_NAME, logger.level)

    if invalid_level:
        logger.error(
            "Ignored invalid log level %r in --log-level/CB_MCP_LOG_LEVEL; "
            "allowed values are %s. Continuing with level=%s.",
            invalid_level,
            list(ALLOWED_LOG_LEVELS),
            level_name,
        )

    if invalid_sinks:
        logger.error(
            "Ignored invalid log sink value(s) %s in --log-sinks/CB_MCP_LOG_SINKS; "
            "allowed values are %s. Continuing with sinks=%s.",
            invalid_sinks,
            list(ALLOWED_LOG_SINKS),
            ",".join(sorted(effective_sinks)),
        )

    for message in file_errors:
        logger.error(message)
    for message in file_warnings:
        logger.warning(message)

    # Show the per-level files in the summary only when the file sink is active;
    # for a stderr-only run printing paths would falsely suggest files exist.
    logger.info(
        "Logging configured: level=%s, sinks=%s, log_files=%s, max_bytes=%d",
        level_name,
        ",".join(sorted(effective_sinks)),
        attached_files if file_sink_active else "-",
        log_max_bytes,
    )

    # Record the snapshot so the server-config MCP tool and env-info diagnostic
    # record can read the active configuration without re-deriving it.
    _resolved_config = ResolvedLoggingConfig(
        level=level_name,
        sinks=tuple(sorted(effective_sinks)),
        log_files=dict(attached_files)
        if (file_sink_active and attached_files)
        else None,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )
