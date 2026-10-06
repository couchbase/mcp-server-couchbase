"""Resolution of the audit configuration from CLI / environment values.

Mirrors the shape of :mod:`cb_mcp.utils.logging`'s resolution: parse, fall back
loudly on unusable input, and expose an immutable snapshot that both
``get_server_configuration_status`` and the startup records can report, so the
tool output and the audit file always agree on what is running.


**Zero means off, not invalid.** Both rotation triggers accept ``0`` as a
first-class value: ``CB_MCP_AUDIT_LOG_ROTATION_MAX_SIZE_MB=0`` turns size-based
rotation off and ``CB_MCP_AUDIT_LOG_ROTATION_INTERVAL=0`` turns interval-based
rotation off. Setting both gives one unbounded file, which is the PRD's Case 2
("audit logs are important for me and I want to store them all"). Only a
*negative* or unparseable value is a configuration error, and that falls back to
the default with a warning rather than aborting.

**A broken file sink degrades, it does not abort.** Selecting the file sink
without naming a path is reported and the file sink is dropped; if console was
also selected the server keeps auditing to console, and if it was not, auditing
is off. The server starts either way.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.constants import (
    ALLOWED_AUDIT_SINKS,
    AUDIT_INTERVAL_UNIT_SECONDS,
    BYTES_PER_MB,
    DEFAULT_AUDIT_ENABLED,
    DEFAULT_AUDIT_MAX_BACKUPS,
    DEFAULT_AUDIT_ROTATION_INTERVAL,
    DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB,
    DEFAULT_AUDIT_SINKS,
    DEFAULT_AUDIT_TOOL_ARGS,
    LOGGER_NAMESPACE,
)
from .catalog import ALL_IDS, FILTERABLE_IDS
from .sink import process_scoped_path

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.audit.config")

SINK_CONSOLE = "console"
SINK_FILE = "file"

#: ``<value><unit>``: one or more digits followed by a single unit letter.
#: Anchored, so "1d2h" and "d1" are rejected rather than half-read.
_INTERVAL_PATTERN = re.compile(r"^(\d+)([a-zA-Z])$")


@dataclass(frozen=True)
class ResolvedAuditConfig:
    """Snapshot of the audit configuration actually in force."""

    enabled: bool
    sinks: tuple[str, ...]
    file: str | None
    process_file: str | None
    rotation_max_size_mb: float
    max_bytes: int
    rotation_interval: str
    rotation_interval_seconds: int
    max_backups: int
    tool_args: bool
    disabled_events: tuple[int, ...]

    @property
    def writes_file(self) -> bool:
        """True when a file sink is actually going to be opened."""
        return self.enabled and SINK_FILE in self.sinks and self.file is not None

    @property
    def writes_console(self) -> bool:
        """True when records are going to stderr."""
        return self.enabled and SINK_CONSOLE in self.sinks

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view for the server-configuration tool and records."""
        return {
            "enabled": self.enabled,
            "sinks": list(self.sinks),
            "file": self.file,
            "process_file": self.process_file,
            "rotation_max_size_mb": self.rotation_max_size_mb,
            "rotation_interval": self.rotation_interval,
            "max_backups": self.max_backups,
            "tool_args": self.tool_args,
            "disabled_events": list(self.disabled_events),
        }


def parse_audit_sinks(raw: str | None) -> tuple[str, ...]:
    """Parse ``CB_MCP_AUDIT_LOG_SINKS`` into an ordered tuple of sink names.

    Same shape as ``CB_MCP_LOG_SINKS``: comma-separated, case-insensitive,
    whitespace trimmed. Unknown tokens are warned about and ignored; if nothing
    valid survives the default sink is used, so enabling auditing always
    produces output somewhere rather than silently producing none.

    The return is ordered rather than a set so the status tool and the ``audit
    configuration changed`` record report sinks in a stable order.
    """
    if raw is None:
        raw = DEFAULT_AUDIT_SINKS

    selected: list[str] = []
    invalid: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        normalised = token.lower()
        if normalised not in ALLOWED_AUDIT_SINKS:
            invalid.append(token)
        elif normalised not in selected:
            selected.append(normalised)

    if invalid:
        logger.warning(
            "Ignored invalid audit sink value(s) %s in "
            "--audit-log-sinks/CB_MCP_AUDIT_LOG_SINKS; allowed values are %s.",
            sorted(invalid),
            list(ALLOWED_AUDIT_SINKS),
        )
    if not selected:
        logger.warning(
            "No valid audit sink configured; falling back to the default of %r.",
            DEFAULT_AUDIT_SINKS,
        )
        selected.append(DEFAULT_AUDIT_SINKS)

    # Canonical order, independent of how the operator happened to type it.
    return tuple(name for name in ALLOWED_AUDIT_SINKS if name in selected)


def parse_rotation_interval(raw: str | None) -> int | None:
    """Parse ``<value><unit>`` into seconds. ``0`` means off; ``None`` invalid.

    Accepts the PRD's vocabulary exactly: an integer followed by ``d`` (24
    hours) or ``w`` (7 days), plus the bare ``0`` that turns interval rotation
    off. Returns the interval in seconds, ``0`` for off, or ``None`` when the
    value cannot be read — the caller decides what to do about that, because
    the right response differs between a Click callback and a direct call.
    """
    if raw is None:
        return None
    entry = raw.strip().lower()
    if not entry:
        return None
    match = _INTERVAL_PATTERN.match(entry)
    if match is None:
        return 0 if entry.isdigit() and int(entry) == 0 else None
    value, unit = int(match.group(1)), match.group(2)
    if unit not in AUDIT_INTERVAL_UNIT_SECONDS:
        return None
    return value * AUDIT_INTERVAL_UNIT_SECONDS[unit]


def _normalise_interval(raw: str | None) -> tuple[str, int]:
    """Resolve the interval setting to its canonical text and its seconds.

    The canonical text is what the status tool reports, so an operator reading
    it back sees the same vocabulary they configured.
    """
    if raw is None:
        raw = DEFAULT_AUDIT_ROTATION_INTERVAL

    seconds = parse_rotation_interval(raw)
    if seconds is None:
        default_seconds = parse_rotation_interval(DEFAULT_AUDIT_ROTATION_INTERVAL)
        logger.warning(
            "Invalid audit rotation interval %r; expected <value><unit> where "
            "the unit is 'd' (days) or 'w' (weeks), e.g. '1d', '30d', '8w', or "
            "'0' to turn interval-based rotation off. Falling back to the "
            "default of %r.",
            raw,
            DEFAULT_AUDIT_ROTATION_INTERVAL,
        )
        return DEFAULT_AUDIT_ROTATION_INTERVAL, int(default_seconds or 0)
    if seconds == 0:
        return "0", 0
    return raw.strip().lower(), seconds


def _read_event_tokens(value: str) -> list[str] | None:
    """Split the raw setting into entries, from a file or a comma-separated list.

    Returns ``None`` when a file was named but could not be read, so the caller
    can distinguish "nothing configured" from "configuration unreadable".
    """
    candidate = Path(value)
    try:
        is_file = candidate.exists() and candidate.is_file()
    except OSError:  # pragma: no cover - unreadable path, treat as a list
        is_file = False

    if not is_file:
        return [part.strip() for part in value.split(",") if part.strip()]

    try:
        with open(candidate, encoding="utf-8") as handle:
            return [
                entry
                for entry in (line.strip() for line in handle)
                if entry and not entry.startswith("#")
            ]
    except OSError as exc:
        logger.error(
            "Failed to read audit disabled-events file %s: %s. "
            "No events will be filtered.",
            candidate,
            exc,
        )
        return None


def _resolve_event_id(token: str) -> int | None:
    """Map one entry — a numeric event id — onto an event id.

    Numeric ids only. Catalogue names were accepted here at one point and were
    withdrawn deliberately: the id is the wire contract and the name is not.
    Names carry spaces and are English prose ("write blocked (read-only mode)"),
    which makes them awkward to quote in a shell and in a Docker ``-e`` value,
    and a name is free to be reworded for clarity in a way an id never is — so
    a filter written against a name could silently stop matching after an
    editorial change. ``descriptor.json`` maps every id to its name for anyone
    composing a filter.
    """
    if not token.isdigit():
        return None
    event_id = int(token)
    return event_id if event_id in ALL_IDS else None


def parse_disabled_events(raw: str | None) -> set[int]:
    """Parse ``CB_MCP_AUDIT_LOG_DISABLED_EVENTS`` into a set of event ids.

    Accepts a comma-separated list, or a path to a file with one entry per line
    (``#`` comments allowed), matching how ``--disabled-tools`` already works.
    Entries are **numeric event ids**; see :func:`_resolve_event_id` for why
    catalogue names are not accepted.

    Two classes of entry are rejected with a warning rather than silently
    honoured, because both would leave an operator believing they had filtered
    something they had not:

    * an entry that is not a numeric id in the catalogue at all;
    * a **non-filterable** event. Writes and security decisions are mandated —
      an operator may turn read noise down but may not switch off the record of
      who changed what.
    """
    if not raw or not raw.strip():
        return set()

    tokens = _read_event_tokens(raw.strip())
    if tokens is None:
        return set()

    resolved: set[int] = set()
    unknown: list[str] = []
    not_filterable: list[str] = []

    for token in tokens:
        event_id = _resolve_event_id(token)
        if event_id is None:
            unknown.append(token)
        elif event_id not in FILTERABLE_IDS:
            not_filterable.append(token)
        else:
            resolved.add(event_id)

    if unknown:
        logger.warning(
            "Ignored unknown audit event(s) in CB_MCP_AUDIT_LOG_DISABLED_EVENTS: %s.",
            sorted(unknown),
        )
    if not_filterable:
        logger.warning(
            "Refused to disable non-filterable audit event(s): %s. Write and "
            "security events are always recorded.",
            sorted(not_filterable),
        )
    return resolved


def _resolve_size_mb(raw: float | None) -> float:
    """Rotation size in MB. ``0`` is off; negative falls back with a warning."""
    if raw is None:
        return DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB
    size_mb = float(raw)
    if size_mb < 0:
        logger.warning(
            "Invalid audit rotation size %s MB; falling back to the default of "
            "%s MB. Use 0 to turn size-based rotation off.",
            raw,
            DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB,
        )
        return DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB
    return size_mb


def _resolve_max_backups(raw: int | None) -> int:
    """Retained backups. ``0`` keeps only the live file; negative falls back."""
    if raw is None:
        return DEFAULT_AUDIT_MAX_BACKUPS
    backups = int(raw)
    if backups < 0:
        logger.warning(
            "Invalid audit retention max backups %s; falling back to the "
            "default of %s.",
            raw,
            DEFAULT_AUDIT_MAX_BACKUPS,
        )
        return DEFAULT_AUDIT_MAX_BACKUPS
    return backups


def resolve_audit_config(
    *,
    enabled: bool | None,
    sinks: str | None = None,
    file: str | None,
    rotation_max_size_mb: float | None,
    rotation_interval: str | None = None,
    max_backups: int | None,
    tool_args: bool | None,
    disabled_events: str | None,
) -> ResolvedAuditConfig:
    """Turn raw CLI/env values into the configuration actually in force."""
    is_enabled = DEFAULT_AUDIT_ENABLED if enabled is None else bool(enabled)
    selected_sinks = parse_audit_sinks(sinks)

    trimmed_file = file.strip() if isinstance(file, str) else None
    trimmed_file = trimmed_file or None

    if is_enabled and SINK_FILE in selected_sinks and trimmed_file is None:
        remaining = tuple(name for name in selected_sinks if name != SINK_FILE)
        logger.error(
            "The 'file' audit sink is selected but no audit file path is "
            "configured. Set --audit-log-file-path / "
            "CB_MCP_AUDIT_LOG_FILE_PATH. The server will start with %s.",
            (
                f"audit records going to {', '.join(remaining)} only"
                if remaining
                else "audit logging disabled"
            ),
        )
        selected_sinks = remaining
        if not selected_sinks:
            is_enabled = False

    if is_enabled and trimmed_file is not None and SINK_FILE not in selected_sinks:
        # The opposite mistake, and a quieter one: a path is configured but the
        # file sink was never selected, so nothing is written to it and the
        # operator finds an empty directory where they expected records.
        logger.warning(
            "An audit file path is configured (%s) but 'file' is not in "
            "--audit-log-sinks/CB_MCP_AUDIT_LOG_SINKS (sinks=%s), so no audit "
            "file will be written. Add 'file' to the sinks to use the path.",
            trimmed_file,
            ",".join(selected_sinks),
        )

    size_mb = _resolve_size_mb(rotation_max_size_mb)
    interval_text, interval_seconds = _normalise_interval(rotation_interval)
    backups = _resolve_max_backups(max_backups)
    include_args = DEFAULT_AUDIT_TOOL_ARGS if tool_args is None else bool(tool_args)

    if include_args and is_enabled:
        logger.warning(
            "CB_MCP_AUDIT_LOG_TOOL_ARGS is enabled: tool arguments will be "
            "recorded verbatim in the audit log, including full document "
            "bodies passed to document-write tools. There is no redaction "
            "capability in this release. Ensure the audit file's retention and "
            "access controls are appropriate for the data it will contain."
        )

    filtered = parse_disabled_events(disabled_events) if is_enabled else set()
    writes_file = (
        is_enabled and SINK_FILE in selected_sinks and trimmed_file is not None
    )

    return ResolvedAuditConfig(
        enabled=is_enabled,
        sinks=selected_sinks,
        file=trimmed_file,
        process_file=(str(process_scoped_path(trimmed_file)) if writes_file else None),
        rotation_max_size_mb=size_mb,
        # 0 stays 0 — the sink reads that as "no size-based rotation". Any
        # positive size is at least one byte, so a sub-byte setting cannot
        # round down into the off switch.
        max_bytes=0 if size_mb == 0 else max(1, int(size_mb * BYTES_PER_MB)),
        rotation_interval=interval_text,
        rotation_interval_seconds=interval_seconds,
        max_backups=backups,
        tool_args=include_args,
        disabled_events=tuple(sorted(filtered)),
    )


__all__ = [
    "SINK_CONSOLE",
    "SINK_FILE",
    "ResolvedAuditConfig",
    "parse_audit_sinks",
    "parse_disabled_events",
    "parse_rotation_interval",
    "resolve_audit_config",
]
