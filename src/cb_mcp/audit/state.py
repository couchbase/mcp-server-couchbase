"""Per-request audit state.

Holds two kinds of thing for the life of one MCP request: the correlation id
that every record for that request shares, and the refusal channel.

**Correlation.** ``cid`` identifies one request. It is written once, by the
middleware's outermost hook, and only read afterwards. Keeping it here rather
than passing it down as an argument means tool code can reach it too — which is
what a later phase needs in order to propagate it to Couchbase Server as a
SQL++ ``client_context_id``.

There is deliberately no session id. MCP is moving to a stateless model in
which a server must not treat connection identity as session continuity, so a
request is the only correlation scope this audit log claims.

**Refusals.** FastMCP wraps a tool's exception in ``ToolError`` before
middleware sees it, so middleware cannot reliably classify a refusal from the
exception it catches. Instead the raising site records a structured refusal
here, and the middleware reads it after ``call_next`` returns or raises.

The contextvar holds a **mutable dict** that the middleware installs once per
tool call. Everything downstream mutates that dict *in place* rather than
rebinding the contextvar. This matters: FastMCP runs synchronous tools on a
worker thread via ``anyio.to_thread``, which copies the context. A rebind
inside the worker would be invisible to the middleware, but an in-place
mutation of the shared object is visible because both contexts reference the
same dict. This behaviour is covered by
``tests/unit/test_audit_state.py::test_mutation_from_worker_thread_is_visible``.

Every accessor is a no-op when no state is installed, so tool code can record
unconditionally without caring whether auditing is enabled.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_AUDIT_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "cb_mcp_audit_state", default=None
)

#: Recorded when a confirmation-gated tool ran without confirmation because the
#: client does not advertise elicitation support.
CONFIRMATION_SKIPPED = "skipped"

#: Recorded when the user accepted an elicitation.
CONFIRMATION_ACCEPTED = "accepted"

#: Recorded when the user rejected an elicitation.
CONFIRMATION_DECLINED = "declined"


def install(*, cid: str | None = None) -> Token:
    """Install a fresh state dict for this request and return its reset token.

    Args:
        cid: Correlation id shared by every record this request produces.
    """
    return _AUDIT_STATE.set({"cid": cid})


def reset(token: Token) -> None:
    """Restore the previous state, undoing :func:`install`."""
    _AUDIT_STATE.reset(token)


def current() -> dict[str, Any] | None:
    """Return the live state dict, or ``None`` when auditing is not tracking."""
    return _AUDIT_STATE.get()


def record_refusal(
    *, event_id: int, event_name: str, outcome: str, reason: str, **extra: Any
) -> None:
    """Record a structured MCP-layer refusal for the middleware to emit.

    Safe to call when no state is installed. Only the first refusal in a call
    is kept — the earliest gate to fire is the one that actually stopped the
    request, and a later one cannot have run.
    """
    state = _AUDIT_STATE.get()
    if state is None or "refusal" in state:
        return
    state["refusal"] = {
        "event_id": event_id,
        "event_name": event_name,
        "outcome": outcome,
        "reason": reason,
        **extra,
    }


def record_confirmation(status: str) -> None:
    """Record the confirmation disposition for this call."""
    state = _AUDIT_STATE.get()
    if state is not None:
        state["confirmation"] = status


def record_statement_class(operation_class: str) -> None:
    """Record the read/write class of an inspected SQL++ statement.

    Lets the middleware book a SQL++ call against the query *write* ID when the
    statement actually modifies data or structure, and lets the read-only guard
    and the audit record share a single parse of the statement.
    """
    state = _AUDIT_STATE.get()
    if state is not None:
        state["statement_class"] = operation_class


def get_cid() -> str | None:
    """Return this request's correlation id, if state is installed."""
    state = _AUDIT_STATE.get()
    return state.get("cid") if state else None


def get_refusal() -> dict[str, Any] | None:
    """Return the recorded refusal, if any."""
    state = _AUDIT_STATE.get()
    return state.get("refusal") if state else None


def get_confirmation() -> str | None:
    """Return the recorded confirmation disposition, if any."""
    state = _AUDIT_STATE.get()
    return state.get("confirmation") if state else None


def get_statement_class() -> str | None:
    """Return the recorded SQL++ statement class, if any."""
    state = _AUDIT_STATE.get()
    return state.get("statement_class") if state else None


__all__ = [
    "CONFIRMATION_ACCEPTED",
    "CONFIRMATION_DECLINED",
    "CONFIRMATION_SKIPPED",
    "current",
    "get_cid",
    "get_confirmation",
    "get_refusal",
    "get_statement_class",
    "install",
    "record_confirmation",
    "record_refusal",
    "record_statement_class",
    "reset",
]
