"""Audit logging for the Couchbase MCP Server.

The MCP server is the only place that knows *who* a caller is. Every request
reaches the cluster as the single ``CB_USERNAME`` the server connects with, so
Couchbase Server's own ``audit.log`` cannot tell two callers apart; and a whole
class of decisions — scope denials, read-only blocks, declined or skipped
confirmations — happens entirely inside this server and never reaches the
cluster at all. Those records can only be produced here.

Records are Couchbase-format JSON Lines, matching Couchbase Server and Sync
Gateway: the same field vocabulary, a descriptor-driven event catalogue on
numbered ID blocks, and per-event filtering as configuration rather than code.

Audit output is a separate sink from the ``CB_MCP_LOG_*`` operational logs. It
guarantees a stable schema, completeness for non-filterable events, and is
sensitive by default.

Layout:

* :mod:`~cb_mcp.audit.catalog` — event ids, names, descriptors
* :mod:`~cb_mcp.audit.classification` — ``tool_name -> (category, class)``
* :mod:`~cb_mcp.audit.config` — CLI/env resolution
* :mod:`~cb_mcp.audit.emitter` — the process-wide logger
* :mod:`~cb_mcp.audit.exceptions` — typed, auditable refusals
* :mod:`~cb_mcp.audit.identity` — ``real_userid`` resolution
* :mod:`~cb_mcp.audit.middleware` — the FastMCP middleware
* :mod:`~cb_mcp.audit.record` — the record and its serialisation
* :mod:`~cb_mcp.audit.sink` — the per-process file writer
* :mod:`~cb_mcp.audit.state` — per-request refusal channel
"""

from .catalog import SERVICE_PACKAGE, AuditEvent, build_descriptor
from .classification import (
    TOOL_CLASSIFICATION,
    classify_tool,
    resolve_tool_call_event,
    unclassified_tool_names,
)
from .config import ResolvedAuditConfig, resolve_audit_config
from .emitter import AuditLogger, get_audit_logger, init_audit, shutdown_audit
from .exceptions import (
    AuditableRefusalError,
    ConfirmationDeclinedError,
    ReadOnlyWriteBlockedError,
    ScopeDeniedError,
    StatementScopeDeniedError,
)
from .identity import resolve_real_userid, warn_on_unauthenticated_http
from .middleware import AuditMiddleware
from .record import (
    OUTCOME_BLOCKED,
    OUTCOME_DENIED,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    AuditRecord,
)
from .sink import AuditSink, process_scoped_path

__all__ = [
    "OUTCOME_BLOCKED",
    "OUTCOME_DENIED",
    "OUTCOME_ERROR",
    "OUTCOME_SUCCESS",
    "SERVICE_PACKAGE",
    "TOOL_CLASSIFICATION",
    "AuditEvent",
    "AuditLogger",
    "AuditMiddleware",
    "AuditRecord",
    "AuditSink",
    "AuditableRefusalError",
    "ConfirmationDeclinedError",
    "ReadOnlyWriteBlockedError",
    "ResolvedAuditConfig",
    "ScopeDeniedError",
    "StatementScopeDeniedError",
    "build_descriptor",
    "classify_tool",
    "get_audit_logger",
    "init_audit",
    "process_scoped_path",
    "resolve_audit_config",
    "resolve_real_userid",
    "resolve_tool_call_event",
    "shutdown_audit",
    "unclassified_tool_names",
    "warn_on_unauthenticated_http",
]
