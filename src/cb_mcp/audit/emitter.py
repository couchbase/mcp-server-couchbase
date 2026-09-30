"""The audit logger: catalogue plus sink, with a process-wide accessor.

A module-level singleton, mirroring how ``get_resolved_logging_config`` already
works in this codebase. The alternative — threading a reference through every
call site — does not work for the JWT verifier, which FastMCP constructs before
the lifespan runs and which has no access to the request context.

Every public method is a no-op when auditing is disabled or failed to start, so
callers never need to branch. Nothing here raises: an audit failure must not be
able to break a tool call.
"""

from __future__ import annotations

import atexit
import logging
from typing import Any

from ..utils.constants import LOGGER_NAMESPACE
from .catalog import SERVICE_PACKAGE, AuditEvent, ToolCallEvent
from .config import ResolvedAuditConfig
from .record import AuditRecord, ServerContext
from .sink import AuditSink

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.audit")


class AuditLogger:
    """Formats catalogue events into records and hands them to the sink."""

    def __init__(self, config: ResolvedAuditConfig, sink: AuditSink | None) -> None:
        self.config = config
        self._sink = sink
        self._server = ServerContext.detect()
        self._disabled = frozenset(config.disabled_events)

    @property
    def active(self) -> bool:
        """True when records are actually being written."""
        return self._sink is not None

    @property
    def server(self) -> ServerContext:
        return self._server

    @property
    def stats(self) -> dict[str, int]:
        return self._sink.stats if self._sink is not None else {}

    def is_enabled_for(self, event_id: int) -> bool:
        """False when the operator has filtered this event out."""
        return event_id not in self._disabled

    # -- emission ---------------------------------------------------------

    def emit(self, record: AuditRecord) -> None:
        """Write one record, unless the event is filtered or auditing is off."""
        if self._sink is None or not self.is_enabled_for(record.id):
            return
        try:
            self._sink.emit(record.to_json_line(self._server))
        except Exception:  # auditing must never break a call
            logger.exception("Unexpected failure while emitting an audit record")

    def emit_event(
        self,
        event: AuditEvent,
        *,
        outcome: str,
        real_userid: dict[str, str] | None = None,
        cid: str | None = None,
        reason: str | None = None,
        **payload: Any,
    ) -> None:
        """Emit a core-catalogue event."""
        self.emit(
            AuditRecord(
                id=event.id,
                name=event.event_name,
                description=event.description,
                outcome=outcome,
                real_userid=real_userid,
                cid=cid,
                reason=reason,
                payload=payload,
            )
        )

    def emit_tool_call(
        self,
        event: ToolCallEvent,
        *,
        outcome: str,
        real_userid: dict[str, str] | None = None,
        cid: str | None = None,
        reason: str | None = None,
        **payload: Any,
    ) -> None:
        """Emit a Tier-2 tool-call event."""
        self.emit(
            AuditRecord(
                id=event.id,
                name=event.event_name,
                description=event.description,
                outcome=outcome,
                real_userid=real_userid,
                cid=cid,
                reason=reason,
                payload=payload,
            )
        )

    def close(self) -> None:
        if self._sink is not None:
            self._sink.close()
            self._sink = None


#: Inactive logger used before initialisation and after shutdown, so callers
#: never have to guard against ``None``.
_DISABLED = AuditLogger(
    ResolvedAuditConfig(
        enabled=False,
        file=None,
        process_file=None,
        rotation_max_size_mb=0.0,
        max_bytes=0,
        retention_backup_count=0,
        tool_args=False,
        disabled_events=(),
    ),
    sink=None,
)

_active: AuditLogger = _DISABLED


def get_audit_logger() -> AuditLogger:
    """Return the process-wide audit logger, active or not."""
    return _active


def init_audit(config: ResolvedAuditConfig) -> AuditLogger:
    """Build and install the audit logger for this process.

    A sink that cannot be opened is reported as an error and auditing is left
    off — per the PRD, a bad audit path must not prevent the server starting.
    """
    global _active  # noqa: PLW0603

    if not config.enabled or config.file is None:
        _active = AuditLogger(config, sink=None)
        return _active

    try:
        sink = AuditSink(
            config.file,
            max_bytes=config.max_bytes,
            backup_count=config.retention_backup_count,
        )
        sink.start()
    except (OSError, ValueError) as exc:
        logger.error(
            "Failed to open the audit file %r: %s. The server will start with "
            "audit logging disabled.",
            config.file,
            exc,
        )
        _active = AuditLogger(config, sink=None)
        return _active

    _active = AuditLogger(config, sink=sink)
    logger.info(
        "Audit logging enabled. Writing to %s (service_package=%s, "
        "max_bytes=%d, backups=%d, tool_args=%s, disabled_events=%s). "
        "Each server process writes its own file; the configured path has the "
        "process id inserted so concurrent stdio servers cannot corrupt one "
        "another's records.",
        sink.path,
        SERVICE_PACKAGE,
        config.max_bytes,
        config.retention_backup_count,
        config.tool_args,
        list(config.disabled_events) or "none",
    )
    return _active


def shutdown_audit() -> None:
    """Flush and close the audit sink, and revert to the inactive logger."""
    global _active  # noqa: PLW0603
    current = _active
    _active = _DISABLED
    current.close()


@atexit.register
def _shutdown_at_exit() -> None:
    """Best-effort flush if the process exits without the lifespan closing.

    A stdio client that kills the server, or an unhandled exit, would otherwise
    lose whatever is still queued. This cannot help on ``SIGKILL``; a missing
    ``server stopped`` record therefore means "not a clean shutdown", not
    "tampered with", and ``AUDIT.md`` says so.
    """
    shutdown_audit()


__all__ = [
    "AuditLogger",
    "get_audit_logger",
    "init_audit",
    "shutdown_audit",
]
