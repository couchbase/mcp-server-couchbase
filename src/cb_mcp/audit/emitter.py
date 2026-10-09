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
import os
import signal
import threading
import types
from dataclasses import replace
from typing import Any

from ..utils.constants import LOGGER_NAMESPACE
from .catalog import SERVICE_PACKAGE, AuditEvent, ToolCallEvent
from .config import SINK_FILE, ResolvedAuditConfig
from .record import OUTCOME_SUCCESS, AuditRecord, ServerContext
from .sink import (
    AuditSink,
    AuditSinkProtocol,
    CompositeAuditSink,
    ConsoleAuditSink,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.audit")


class AuditLogger:
    """Formats catalogue events into records and hands them to the sink."""

    def __init__(
        self, config: ResolvedAuditConfig, sink: AuditSinkProtocol | None
    ) -> None:
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
        sinks=(),
        file=None,
        process_file=None,
        rotation_max_size_mb=0.0,
        max_bytes=0,
        rotation_interval="0",
        rotation_interval_seconds=0,
        max_backups=0,
        tool_args=False,
        disabled_events=(),
    ),
    sink=None,
)

_active: AuditLogger = _DISABLED


def get_audit_logger() -> AuditLogger:
    """Return the process-wide audit logger, active or not."""
    return _active


def _open_file_sink(config: ResolvedAuditConfig) -> AuditSink | None:
    """Open the file sink, or report why it could not be opened.

    A sink that cannot be opened is reported as an error and left out — per the
    PRD, a bad audit path must not prevent the server starting. Any console
    sink the operator also selected still runs, so the records go somewhere.
    """
    if config.file is None:
        return None
    try:
        sink = AuditSink(
            config.file,
            max_bytes=config.max_bytes,
            max_backups=config.max_backups,
            interval_seconds=config.rotation_interval_seconds,
        )
    except (OSError, ValueError) as exc:
        logger.error(
            "Failed to open the audit file %r: %s. The server will start "
            "without the file audit sink.",
            config.file,
            exc,
        )
        return None
    return sink


def _describe_rotation(config: ResolvedAuditConfig) -> str:
    """How the live file will roll over, in the operator's own vocabulary."""
    triggers = []
    if config.max_bytes > 0:
        triggers.append(f"size>{config.rotation_max_size_mb:g}MB")
    if config.rotation_interval_seconds > 0:
        triggers.append(f"age>{config.rotation_interval}")
    if not triggers:
        return "never (both size and interval rotation are off)"
    return " or ".join(triggers)


def init_audit(config: ResolvedAuditConfig) -> AuditLogger:
    """Build and install the audit logger for this process."""
    global _active  # noqa: PLW0603

    if not config.enabled or not config.sinks:
        _active = AuditLogger(config, sink=None)
        return _active

    sinks: list[AuditSinkProtocol] = []
    file_sink: AuditSink | None = None
    if config.writes_console:
        sinks.append(ConsoleAuditSink())
    if config.writes_file:
        file_sink = _open_file_sink(config)
        if file_sink is not None:
            sinks.append(file_sink)
        else:
            # The snapshot is not cosmetic: it is reported by
            # ``get_server_configuration_status`` and written into the ``audit
            # configuration changed`` record, where it is permanent. A config
            # still claiming a file sink that failed to open would tell an
            # auditor records were being kept on disk when they were not.
            config = replace(
                config,
                sinks=tuple(name for name in config.sinks if name != SINK_FILE),
                process_file=None,
            )

    if not sinks:
        _active = AuditLogger(replace(config, enabled=False), sink=None)
        return _active

    sink: AuditSinkProtocol = sinks[0] if len(sinks) == 1 else CompositeAuditSink(sinks)
    sink.start()
    _active = AuditLogger(config, sink=sink)
    # Only once auditing is actually running: a server with auditing off has no
    # reason to touch the process's signal disposition.
    _install_sigterm_handler()

    logger.info(
        "Audit logging enabled. sinks=%s, service_package=%s, file=%s, "
        "rotates=%s, max_backups=%d, backups_compressed=yes, tool_args=%s, "
        "disabled_events=%s. Each server process writes its own file; the "
        "configured path has the host and process id inserted so concurrent "
        "stdio servers cannot corrupt one another's records.",
        ",".join(config.sinks),
        SERVICE_PACKAGE,
        file_sink.path if file_sink is not None else "none",
        _describe_rotation(config),
        config.max_backups,
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
    ``server stopped`` record therefore means "not a clean shutdown" — it is
    not evidence of tampering, and must not be read as such.
    """
    shutdown_audit()


#: The SIGTERM handler that was in place before auditing installed its own, so
#: it can still run. ``None`` means nothing has been installed yet.
_previous_sigterm: Any = None

#: How long to wait for the audit trail to close on a SIGTERM that nothing else
#: is handling. Bounded because a shutdown must not hang; short because the
#: process is about to terminate either way.
_SIGTERM_DRAIN_SECONDS = 3.0


def _close_audit_for_signal() -> None:
    """Record the stop and close the sink. Runs on a helper thread, not inline.

    A signal handler runs on the main thread — the same thread that calls
    ``emit`` and therefore the one that holds the sink queue's lock while it
    does. That lock is not reentrant, so queueing a record from inside the
    handler could deadlock a shutdown that must not hang. On a helper thread the
    worst case is waiting for the main thread's lock, which the bounded join
    below turns into a missing record rather than a wedged process.
    """
    audit = get_audit_logger()
    if audit.active:
        audit.emit_event(
            AuditEvent.SERVER_STOPPED,
            outcome=OUTCOME_SUCCESS,
            shutdown_signal="SIGTERM",
        )
    shutdown_audit()


def _handle_sigterm(signum: int, frame: types.FrameType | None) -> None:
    """Close the audit trail when, and only when, nothing else will.

    ``docker stop`` sends SIGTERM, and Python's default disposition terminates
    the process without running ``atexit`` hooks or unwinding the lifespan. So
    under stdio the most ordinary shutdown a container has produced no ``server
    stopped`` record at all — while the catalogue says a missing one means the
    shutdown was not clean. An operator restarting a container would have read
    an unclean shutdown on every single restart, which makes the signal
    worthless for spotting a real one.

    **When another handler already owns SIGTERM, this one does nothing but hand
    over.** Under the http transport that handler is uvicorn's, which sets a
    flag and then *drains in-flight requests* before exiting. Closing the audit
    trail here would leave every one of those calls unrecorded — the middleware
    reads the process-wide logger, finds it inactive, and emits nothing, not
    even a dropped count. A write cancelled by a rolling restart would vanish,
    which is precisely what recording cancelled calls exists to prevent. The
    lifespan already emits ``server stopped`` when uvicorn unwinds it, so
    deferring loses nothing and keeps the records that are still arriving.
    """
    previous = _previous_sigterm
    if previous is signal.SIG_IGN:
        return
    if callable(previous):
        previous(signum, frame)
        return

    # SIG_DFL (or an unreadable handler): nothing else will unwind anything, so
    # this is the only chance to close the trail.
    worker = threading.Thread(
        target=_close_audit_for_signal, name="cb-mcp-audit-sigterm", daemon=True
    )
    worker.start()
    worker.join(timeout=_SIGTERM_DRAIN_SECONDS)

    # Terminate with the conventional status for the signal rather than exiting
    # 0 and hiding that we were killed.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _install_sigterm_handler() -> None:
    """Install :func:`_handle_sigterm`, once, when it is possible to do so.

    ``signal.signal`` only works on the main thread of the main interpreter, so
    an embedding host that builds the app on a worker thread simply does not get
    this; auditing is unaffected otherwise.

    "Already installed" is read from the live signal disposition rather than
    tracked in a module flag. The flag was a second copy of state the ``signal``
    module already owns, and the two could disagree: a host that installed its
    own handler after ours would leave the flag saying "installed" while the
    disposition said otherwise, and ours would never be restored. Reading the
    disposition cannot drift — and it is what makes the guard below load-bearing
    rather than an optimisation.
    """
    global _previous_sigterm  # noqa: PLW0603
    try:
        previous = signal.getsignal(signal.SIGTERM)
        if previous is _handle_sigterm:
            # Ours is already in place. Falling through would record this very
            # function as its own predecessor, and the next SIGTERM would
            # recurse into it until the stack ran out.
            return
        # Published before the handler is installed: a SIGTERM delivered
        # between the two would otherwise read ``None`` and take the default
        # path, skipping the handler that actually owns the shutdown.
        _previous_sigterm = previous
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError, AttributeError):
        logger.debug(
            "Could not install the audit SIGTERM handler; a SIGTERM shutdown "
            "will not record a 'server stopped' event.",
            exc_info=True,
        )


__all__ = [
    "AuditLogger",
    "get_audit_logger",
    "init_audit",
    "shutdown_audit",
]
