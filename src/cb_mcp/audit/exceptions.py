"""Typed exceptions for auditable MCP-layer refusals.

Three semantically different refusals used to surface as a bare
``PermissionError`` carrying only a human-readable message, and a read-only
DML block surfaced as ``ValueError`` — the same type a malformed query raises.
An audit middleware that assigned ``outcome``/``reason`` by matching on those
message strings would silently mis-attribute records after the next copy edit,
and a mis-attributed audit record is worse than a missing one.

Every class here **subclasses the exception type the call site raised before**,
so existing ``pytest.raises`` assertions and any client-visible behaviour stay
exactly as they were. The public message text is unchanged too; only the type
is narrowed.

Note that FastMCP wraps a tool's exception in ``ToolError`` before it reaches
middleware (the original is preserved on ``__cause__``). Middleware therefore
does **not** rely on catching these types — the raising site records the
structured refusal into the per-request audit state (see
:mod:`cb_mcp.audit.state`), and these classes exist so the raise site is
self-documenting and so callers can still discriminate programmatically.
"""

from .catalog import AuditEvent


class AuditableRefusalError(Exception):
    """Mixin marking an exception the audit layer knows how to classify.

    Carries the catalogue event plus the ``outcome``/``reason`` pair the record
    should report. Subclasses inherit from both this and the original builtin
    exception type, keeping backwards compatibility at the call site.
    """

    audit_event: AuditEvent
    audit_outcome: str
    audit_reason: str


class ScopeDeniedError(AuditableRefusalError, PermissionError):
    """A token was present but lacked a scope the tool requires."""

    audit_event = AuditEvent.SCOPE_CHECK_DENIED
    audit_outcome = "denied"
    audit_reason = "missing_scope"


class ConfirmationDeclinedError(AuditableRefusalError, PermissionError):
    """The user rejected an elicitation for a confirmation-required tool."""

    audit_event = AuditEvent.CONFIRMATION_DECLINED
    audit_outcome = "blocked"
    audit_reason = "confirmation_declined"


class ReadOnlyWriteBlockedError(AuditableRefusalError, ValueError):
    """A SQL++ statement that modifies data or structure was refused.

    Raised when ``CB_MCP_READ_ONLY_MODE`` is enabled. Subclasses ``ValueError``
    because that is what ``run_sql_plus_plus_query`` raised for this case
    before, and callers (including tests) match on it.
    """

    audit_event = AuditEvent.WRITE_BLOCKED_READ_ONLY
    audit_outcome = "blocked"
    audit_reason = "read_only_mode"


class StatementScopeDeniedError(AuditableRefusalError, PermissionError):
    """A SQL++ modification was refused because the token lacks write scope.

    Distinct from :class:`ScopeDeniedError`: the per-tool scope wrapper classifies
    SQL++ as a read tool, so this denial is decided by inspecting the statement
    at invocation rather than by the tool's static classification.
    """

    audit_event = AuditEvent.SCOPE_CHECK_DENIED
    audit_outcome = "denied"
    audit_reason = "missing_scope"


__all__ = [
    "AuditableRefusalError",
    "ConfirmationDeclinedError",
    "ReadOnlyWriteBlockedError",
    "ScopeDeniedError",
    "StatementScopeDeniedError",
]
