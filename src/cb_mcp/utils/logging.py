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
from collections.abc import Mapping
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any, NamedTuple

import couchbase

from .constants import (
    ALLOWED_LOG_LEVELS,
    ALLOWED_LOG_SINKS,
    BYTES_PER_MB,
    DEFAULT_LOG_DATEFMT,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
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
    ``log_max_bytes`` maps each active level to its resolved rotation size in
    bytes, and ``log_backup_counts`` maps each active level to the number of
    rotated backups retained for it (``{"INFO": 1, "ERROR": 5, ...}``). All three
    per-level maps share the same keys and are ``None`` whenever the file sink
    isn't part of that set — including under ``level="OFF"``, where no handlers
    are attached at all.

    ``server_config_file`` is the path of the dedicated, non-rotating JSON file
    that captures the one-shot server-config/environment snapshot (derived from
    the ``--log-file`` base, e.g. ``mcp_server_config.log.json``). Like the
    per-level maps it is ``None`` unless file logging is active *and* at least
    one per-level file was opened — so it never advertises a file that couldn't
    actually be written.
    """

    level: str
    sinks: tuple[str, ...]
    log_files: dict[str, str] | None
    log_max_bytes: dict[str, int] | None
    log_backup_counts: dict[str, int] | None
    server_config_file: str | None

    def as_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict with shorter key names."""
        return {
            "level": self.level,
            "sinks": list(self.sinks),
            "log_files": dict(self.log_files) if self.log_files else None,
            "max_bytes": dict(self.log_max_bytes) if self.log_max_bytes else None,
            "backup_counts": dict(self.log_backup_counts)
            if self.log_backup_counts
            else None,
            "server_config_file": self.server_config_file,
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


def _resolve_global_max_bytes(
    rotation_max_size_mb: float | None,
    max_bytes: int | None,
) -> tuple[int, list[str]]:
    """Resolve the effective global rotation size in bytes.

    ``rotation_max_size_mb`` is the canonical ``CB_MCP_LOG_ROTATION_MAX_SIZE_MB``
    (MB) and takes precedence; ``max_bytes`` is the deprecated
    ``CB_MCP_LOG_MAX_BYTES`` (bytes), still honored for backward compatibility.
    A value of 0 (either variable) is invalid and falls back to
    :data:`DEFAULT_LOG_MAX_BYTES`. Returns ``(bytes, warnings)``.
    """
    warnings: list[str] = []
    if max_bytes is not None:
        warnings.append(
            "CB_MCP_LOG_MAX_BYTES is deprecated; use CB_MCP_LOG_ROTATION_MAX_SIZE_MB "
            "(in MB) instead. It will be removed in a future release."
        )

    if rotation_max_size_mb is not None:
        if max_bytes is not None:
            warnings.append(
                "Both CB_MCP_LOG_ROTATION_MAX_SIZE_MB and CB_MCP_LOG_MAX_BYTES are "
                "set; using CB_MCP_LOG_ROTATION_MAX_SIZE_MB and ignoring the "
                "deprecated CB_MCP_LOG_MAX_BYTES."
            )
        if rotation_max_size_mb == 0:
            warnings.append(
                "CB_MCP_LOG_ROTATION_MAX_SIZE_MB=0 is not a valid rotation size; "
                f"falling back to the default of {DEFAULT_LOG_MAX_BYTES} bytes."
            )
            return DEFAULT_LOG_MAX_BYTES, warnings
        return max(1, round(rotation_max_size_mb * BYTES_PER_MB)), warnings

    # Deprecated fallback: only reached when CB_MCP_LOG_ROTATION_MAX_SIZE_MB is unset.
    if max_bytes is not None:
        if max_bytes == 0:
            warnings.append(
                "CB_MCP_LOG_MAX_BYTES=0 is not a valid rotation size; falling back "
                f"to the default of {DEFAULT_LOG_MAX_BYTES} bytes."
            )
            return DEFAULT_LOG_MAX_BYTES, warnings
        return max_bytes, warnings

    return DEFAULT_LOG_MAX_BYTES, warnings


def _resolve_per_level_max_bytes(
    rotation_max_size_mb: float | None,
    max_bytes: int | None,
    overrides_mb: Mapping[str, float],
) -> tuple[dict[str, int], list[str]]:
    """Resolve per-level rotation sizes in bytes from the size configuration.

    Resolves the effective global via :func:`_resolve_global_max_bytes`, then
    applies per-level ``CB_MCP_LOG_<LEVEL>_ROTATION_MAX_SIZE_MB`` overrides (in
    **MB**): a level absent from ``overrides_mb`` inherits the global, and a
    level's 0 is invalid and falls back to inheriting the global. Returns the
    ``{level: bytes}`` map for all levels plus human-readable warnings the caller
    should surface once the logger is wired.
    """
    resolved_global_bytes, warnings = _resolve_global_max_bytes(
        rotation_max_size_mb, max_bytes
    )

    per_level: dict[str, int] = {}
    for lvl in _PER_LEVEL_FILE_LEVELS:
        size_mb = overrides_mb.get(lvl)
        if size_mb:  # explicit, non-zero override (MB) -> convert to bytes
            per_level[lvl] = max(1, round(size_mb * BYTES_PER_MB))
            continue
        if size_mb == 0:  # explicit 0 is invalid (None is silent)
            warnings.append(
                f"CB_MCP_LOG_{lvl}_ROTATION_MAX_SIZE_MB=0 is not a valid rotation "
                f"size; falling back to the global rotation size "
                f"({resolved_global_bytes} bytes)."
            )
        per_level[lvl] = resolved_global_bytes  # unset or 0 -> inherit the global
    return per_level, warnings


class BoundedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps the live file bounded when backupCount=0.

    Stock ``RotatingFileHandler`` with ``backupCount == 0`` reopens the base
    file in append mode on rollover, so the live file grows past ``maxBytes``
    without limit and re-triggers a rollover on every subsequent write. When an
    operator sets retention to 0 (keep only the live file, no backups), they
    still expect ``maxBytes`` to cap it — so here we truncate the file on
    rollover instead, cycling a single bounded live file. With
    ``backupCount > 0`` the stock numbered-backup behaviour is used unchanged.
    """

    def doRollover(self) -> None:  # noqa: N802 (overrides stdlib camelCase API)
        if self.backupCount > 0:
            super().doRollover()
            return
        if self.stream:
            self.stream.close()
            self.stream = None
        # Reset the live file to empty on disk so the truncation holds even on
        # the delayed-open path; a fresh append then starts from zero bytes.
        with open(self.baseFilename, "w", encoding=self.encoding):
            pass
        if not self.delay:
            self.stream = self._open()


def _attach_per_level_file_handlers(
    logger: logging.Logger,
    formatter: logging.Formatter,
    log_file: str,
    max_bytes: Mapping[str, int],
    global_backup_count: int,
    backup_count_overrides: Mapping[str, int],
) -> tuple[dict[str, str], dict[str, int], list[str]]:
    """Attach one rotating file handler per active level to ``logger``.

    All per-level files derive from the single ``log_file`` base path by
    inserting the level name (``mcp_server.log`` -> ``mcp_server.info.log``,
    ``mcp_server.error.log``, ...). Each handler's ``maxBytes`` comes from the
    pre-resolved ``max_bytes`` map; its retention is resolved here — a level's
    ``backup_count_overrides`` value if set, otherwise ``global_backup_count``.
    A count of 0 keeps no rotated backups; only the live file is retained, and it
    is truncated on rollover so it stays bounded by ``maxBytes``
    (see :class:`BoundedRotatingFileHandler`).

    Returns ``(attached, backup_counts, errors)`` where ``attached`` maps each
    level that got a handler to its file path, ``backup_counts`` maps those same
    levels to the retention count actually applied, and ``errors`` collects
    human-readable problems (a missing base path that fell back to the default,
    or a file that couldn't be opened) for the caller to log once all handlers
    are wired and visible.

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
    backup_counts: dict[str, int] = {}
    for lvl_name in _PER_LEVEL_FILE_LEVELS:
        lvl_no = logging.getLevelName(lvl_name)
        if lvl_no < logger.level:
            continue
        path = _per_level_path(log_file, lvl_name)
        backup_count = backup_count_overrides.get(lvl_name, global_backup_count)
        try:
            handler = BoundedRotatingFileHandler(
                path,
                maxBytes=max_bytes[lvl_name],
                backupCount=backup_count,
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
        backup_counts[lvl_name] = backup_count
    return attached, backup_counts, errors


class ParsedLogLevel(NamedTuple):
    """Resolved log level plus any rejected input, for deferred warning."""

    level: str
    invalid_token: str | None


class ParsedLogSinks(NamedTuple):
    """Resolved sink set plus any rejected tokens, for deferred warning."""

    sinks: set[str]
    invalid_tokens: list[str]


def parse_log_level(value: str) -> ParsedLogLevel:
    """Parse a log level value, falling back to the default for invalid input.

    When ``value`` matches one of ``ALLOWED_LOG_LEVELS`` (case-insensitive),
    ``invalid_token`` is ``None``. Otherwise ``level`` is ``DEFAULT_LOG_LEVEL``
    and the original input is returned as ``invalid_token`` so the caller can
    surface it via the logger once handlers are wired.
    """
    token = value.strip().upper()
    if token in ALLOWED_LOG_LEVELS:
        return ParsedLogLevel(token, None)
    return ParsedLogLevel(DEFAULT_LOG_LEVEL, value)


def parse_log_sinks(value: str) -> ParsedLogSinks:
    """Parse a comma-separated CB_MCP_LOG_SINKS value.

    Tokens are case-insensitive and whitespace around them is trimmed. Valid
    tokens are accumulated in ``sinks`` (a non-empty set drawn from
    ``ALLOWED_LOG_SINKS``); unknown tokens are collected in ``invalid_tokens``
    (original case) so the caller can surface them via the logger once it is
    configured. If no valid tokens survive, the default sink is used so the
    server still produces output.
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
    return ParsedLogSinks(sinks, invalid)


def configure_logging(
    level: str,
    sinks: set[str],
    log_file: str,
    log_backup_count: int,
    log_rotation_max_size_mb: float | None = None,
    log_max_bytes: int | None = None,
    log_rotation_size_overrides: Mapping[str, float] | None = None,
    log_backup_count_overrides: Mapping[str, int] | None = None,
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

    Retention is per level. ``log_backup_count`` is the global number of rotated
    backups kept for every level file; ``log_backup_count_overrides`` maps
    individual level names (``"DEBUG"``/``"INFO"``/``"WARNING"``/``"ERROR"``) to
    an explicit count that wins over the global for that level. A level absent
    from the overrides inherits ``log_backup_count``. A resolved count of 0 keeps
    no rotated backups for that level — only the live file remains, still bounded
    by the resolved rotation size (it is truncated on rollover rather than
    rotated).

    Rotation size is per level too, configured in **MB**. ``log_rotation_max_size_mb``
    is the canonical global size (``CB_MCP_LOG_ROTATION_MAX_SIZE_MB``, MB);
    ``log_max_bytes`` is the **deprecated** ``CB_MCP_LOG_MAX_BYTES`` (bytes), still
    honored for backward compatibility but superseded by the canonical variable
    when both are set (with a deprecation warning). ``log_rotation_size_overrides``
    maps individual level names to an explicit size **in MB**
    (``CB_MCP_LOG_<LEVEL>_ROTATION_MAX_SIZE_MB``) that wins over the global; a level
    absent from the overrides inherits it. A size of 0 (global or per level) is
    invalid and falls back to the default with a startup warning (the global to
    :data:`DEFAULT_LOG_MAX_BYTES`, a level to the inherited global). All values
    are converted to bytes internally.

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
            log_max_bytes=None,
            log_backup_counts=None,
            server_config_file=None,
        )
        return

    logger.setLevel(level_name)

    formatter = logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=DEFAULT_LOG_DATEFMT)

    effective_sinks = set(sinks)
    file_sink_active = "file" in effective_sinks

    # The server-config/environment snapshot gets its own non-rotating JSON file
    # derived from the base path (mcp_server.log -> mcp_server_config.log.json).
    # Only when the file sink is active; ``log_environment_info`` writes it later.
    server_config_file = (
        "{}_config{}.json".format(*os.path.splitext(log_file or DEFAULT_LOG_FILE))
        if file_sink_active
        else None
    )

    # Rotation size is resolved up front because its global resolution emits
    # deprecation / 0-is-invalid warnings (``size_warnings``) that must be
    # surfaced regardless of which levels attach. Retention (backup count) is a
    # plain default-fill, so it's resolved per level inside the handler attach.
    resolved_max_bytes, size_warnings = _resolve_per_level_max_bytes(
        log_rotation_max_size_mb, log_max_bytes, log_rotation_size_overrides or {}
    )

    if "stderr" in effective_sinks:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        logger.addHandler(stderr_handler)

    # Deferred so these surface after handlers (incl. stderr) are wired and are
    # therefore actually visible.
    file_warnings: list[str] = []
    file_errors: list[str] = []
    attached_files: dict[str, str] = {}
    active_backup_counts: dict[str, int] = {}

    if file_sink_active:
        attached_files, active_backup_counts, file_errors = (
            _attach_per_level_file_handlers(
                logger,
                formatter,
                log_file,
                resolved_max_bytes,
                log_backup_count,
                log_backup_count_overrides or {},
            )
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
    # 0-is-invalid rotation-size fallbacks (global or per level) — surface these
    # regardless of sink so an operator learns their 0 was ignored.
    for message in size_warnings:
        logger.warning(message)

    # Rotation sizes for the levels that actually got a file (retention counts
    # come back from the attach as ``active_backup_counts``).
    active_max_bytes = {lvl: resolved_max_bytes[lvl] for lvl in attached_files}

    # Show the per-level files in the summary only when the file sink is active;
    # for a stderr-only run printing paths would falsely suggest files exist.
    logger.info(
        "Logging configured: level=%s, sinks=%s, log_files=%s, max_bytes=%s, backup_counts=%s",
        level_name,
        ",".join(sorted(effective_sinks)),
        attached_files or "-",
        active_max_bytes or "-",
        active_backup_counts or "-",
    )

    # Only populate the file-based snapshot fields when file logging actually
    # succeeded — if the sink is stderr-only, or every per-level handler failed
    # to open (e.g. an unwritable directory), attached_files is empty and
    # claiming files/paths would be misleading (and writing them would fail).
    file_logging_active = bool(file_sink_active and attached_files)

    # Record the snapshot so the server-config MCP tool and env-info diagnostic
    # record can read the active configuration without re-deriving it.
    _resolved_config = ResolvedLoggingConfig(
        level=level_name,
        sinks=tuple(sorted(effective_sinks)),
        log_files=dict(attached_files) if file_logging_active else None,
        log_max_bytes=active_max_bytes if file_logging_active else None,
        log_backup_counts=active_backup_counts if file_logging_active else None,
        server_config_file=server_config_file if file_logging_active else None,
    )
