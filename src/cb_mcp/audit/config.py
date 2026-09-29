"""Resolution of the audit configuration from CLI / environment values.

Mirrors the shape of :mod:`cb_mcp.utils.logging`'s resolution: parse, fall back
loudly on unusable input, and expose an immutable snapshot that both
``get_server_configuration_status`` and the startup records can report, so the
tool output and the audit file always agree on what is running.

Per the PRD, enabling auditing without a file path is an error that is
*reported* rather than fatal: the server still starts, with auditing off.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.constants import (
    BYTES_PER_MB,
    DEFAULT_AUDIT_BACKUP_COUNT,
    DEFAULT_AUDIT_ENABLED,
    DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB,
    DEFAULT_AUDIT_TOOL_ARGS,
    MCP_SERVER_NAME,
)
from .catalog import ALL_IDS, EVENT_NAMES_TO_IDS, FILTERABLE_IDS
from .sink import process_scoped_path

logger = logging.getLogger(f"{MCP_SERVER_NAME}.audit.config")


@dataclass(frozen=True)
class ResolvedAuditConfig:
    """Snapshot of the audit configuration actually in force."""

    enabled: bool
    file: str | None
    process_file: str | None
    rotation_max_size_mb: float
    max_bytes: int
    retention_backup_count: int
    tool_args: bool
    disabled_events: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view for the server-configuration tool and records."""
        return {
            "enabled": self.enabled,
            "file": self.file,
            "process_file": self.process_file,
            "rotation_max_size_mb": self.rotation_max_size_mb,
            "retention_backup_count": self.retention_backup_count,
            "tool_args": self.tool_args,
            "disabled_events": list(self.disabled_events),
        }


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
    """Map one entry — a numeric id or a catalogue name — onto an event id."""
    if token.isdigit():
        event_id = int(token)
        return event_id if event_id in ALL_IDS else None
    return EVENT_NAMES_TO_IDS.get(token)


def parse_disabled_events(raw: str | None) -> set[int]:
    """Parse ``CB_MCP_AUDIT_DISABLED_EVENTS`` into a set of event ids.

    Accepts a comma-separated list, or a path to a file with one entry per line
    (``#`` comments allowed), matching how ``--disabled-tools`` already works.
    Entries may be either the numeric event id or the catalogue event name.

    Two classes of entry are rejected with a warning rather than silently
    honoured, because both would leave an operator believing they had filtered
    something they had not:

    * an id or name that is not in the catalogue at all;
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
            "Ignored unknown audit event(s) in CB_MCP_AUDIT_DISABLED_EVENTS: %s.",
            sorted(unknown),
        )
    if not_filterable:
        logger.warning(
            "Refused to disable non-filterable audit event(s): %s. Write and "
            "security events are always recorded.",
            sorted(not_filterable),
        )
    return resolved


def resolve_audit_config(
    *,
    enabled: bool | None,
    file: str | None,
    rotation_max_size_mb: float | None,
    retention_backup_count: int | None,
    tool_args: bool | None,
    disabled_events: str | None,
) -> ResolvedAuditConfig:
    """Turn raw CLI/env values into the configuration actually in force."""
    is_enabled = DEFAULT_AUDIT_ENABLED if enabled is None else bool(enabled)
    trimmed_file = file.strip() if isinstance(file, str) else None
    trimmed_file = trimmed_file or None

    if is_enabled and trimmed_file is None:
        logger.error(
            "Audit logging is enabled but no audit file path is configured. "
            "Set --audit-file / CB_MCP_AUDIT_FILE to enable auditing. "
            "The server will start with audit logging disabled."
        )
        is_enabled = False

    size_mb = (
        DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB
        if rotation_max_size_mb is None
        else float(rotation_max_size_mb)
    )
    if size_mb <= 0:
        logger.warning(
            "Invalid audit rotation size %s MB; falling back to the default of %s MB.",
            rotation_max_size_mb,
            DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB,
        )
        size_mb = DEFAULT_AUDIT_ROTATION_MAX_SIZE_MB

    backup_count = (
        DEFAULT_AUDIT_BACKUP_COUNT
        if retention_backup_count is None
        else int(retention_backup_count)
    )
    if backup_count < 0:
        logger.warning(
            "Invalid audit retention backup count %s; falling back to the "
            "default of %s.",
            retention_backup_count,
            DEFAULT_AUDIT_BACKUP_COUNT,
        )
        backup_count = DEFAULT_AUDIT_BACKUP_COUNT

    include_args = DEFAULT_AUDIT_TOOL_ARGS if tool_args is None else bool(tool_args)

    if include_args and is_enabled:
        logger.warning(
            "CB_MCP_AUDIT_TOOL_ARGS is enabled: tool arguments will be "
            "recorded verbatim in the audit log, including full document "
            "bodies passed to document-write tools. There is no redaction "
            "capability in this release. Ensure the audit file's retention and "
            "access controls are appropriate for the data it will contain."
        )

    filtered = parse_disabled_events(disabled_events) if is_enabled else set()

    return ResolvedAuditConfig(
        enabled=is_enabled,
        file=trimmed_file,
        process_file=(
            str(process_scoped_path(trimmed_file))
            if is_enabled and trimmed_file
            else None
        ),
        rotation_max_size_mb=size_mb,
        max_bytes=max(1, int(size_mb * BYTES_PER_MB)),
        retention_backup_count=backup_count,
        tool_args=include_args,
        disabled_events=tuple(sorted(filtered)),
    )


def audit_config_from_settings(settings: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the audit snapshot out of the lifespan settings mapping."""
    snapshot = settings.get("audit_config")
    return dict(snapshot) if isinstance(snapshot, Mapping) else None


__all__ = [
    "ResolvedAuditConfig",
    "audit_config_from_settings",
    "parse_disabled_events",
    "resolve_audit_config",
]
