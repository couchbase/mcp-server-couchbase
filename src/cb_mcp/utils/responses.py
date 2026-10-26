"""Standard result envelopes for MCP tools that report failure as data.

These tools return a structured dict rather than a bare bool so the calling
LLM can see *why* an operation failed, not just that it did. The base shape is
``{"success": bool}`` plus ``"error"`` on failure; each tool adds
operation-specific context (``keyspace``, ``index_name``, ...) as keyword
arguments. KV write tools can adopt these helpers later for a uniform contract.

Used by every write tool, and by read tools that catch their own failures rather
than letting them propagate (``discover_tool_input_values``). Read tools whose
only failure mode is an unreachable cluster still return bare data and raise.
"""

from typing import Any

from .constants import MAX_ERROR_MESSAGE_LENGTH


def tool_success(**fields: Any) -> dict[str, Any]:
    """Build a success envelope: ``{"success": True, **fields}``."""
    return {"success": True, **fields}


def _truncate(text: str) -> str:
    if len(text) <= MAX_ERROR_MESSAGE_LENGTH:
        return text
    return text[:MAX_ERROR_MESSAGE_LENGTH] + "... [truncated]"


def error_message(error: Exception | str) -> str:
    """Condense an exception into a short, client-safe message.

    ``str()`` of a Couchbase SDK exception is a full dump: the SQL++ statement,
    every bound parameter (for vector tools that is the whole embedding, 30 KB+),
    node addresses and a C++ source path. None of that helps the caller and it
    floods the LLM's context, so SDK errors are reduced to the server's own
    error code and message. Everything else is returned as ``str(error)``.
    Whatever the source, the result is capped at ``MAX_ERROR_MESSAGE_LENGTH`` characters.

    SDK exceptions are recognised by duck typing (``error_context``) because this
    module is shared with the Operational Insights server, whose process must
    not import the ``couchbase`` SDK.
    """
    context = getattr(error, "error_context", None)
    if context is None:
        return _truncate(str(error))
    code = getattr(context, "first_error_code", None)
    message = getattr(context, "first_error_message", None)
    if code is not None or message:
        return _truncate(f"{code}: {message}" if code is not None else str(message))
    # No server-side error detail (timeouts, connection failures, ...): keep the
    # exception type and SDK message, drop the context.
    detail = getattr(error, "message", None) or "no details"
    return _truncate(f"{type(error).__name__}: {detail}")


def tool_error(error: Exception | str, **fields: Any) -> dict[str, Any]:
    """Build a failure envelope: ``{"success": False, "error": <short message>, **fields}``.

    SDK exceptions are condensed by :func:`error_message`.
    """
    return {"success": False, "error": error_message(error), **fields}
