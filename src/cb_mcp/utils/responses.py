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


_TRUNCATION_SUFFIX = "... [truncated]"


def _truncate(text: str) -> str:
    """Cap ``text`` at ``MAX_ERROR_MESSAGE_LENGTH`` characters, suffix included."""
    if len(text) <= MAX_ERROR_MESSAGE_LENGTH:
        return text
    return (
        text[: MAX_ERROR_MESSAGE_LENGTH - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
    )


def error_message(error: Exception | str) -> str:
    """Condense an exception into a short message for the MCP client.

    ``str()`` of a Couchbase SDK query exception is a full dump: the SQL++
    statement, every bound parameter (for vector tools the whole embedding), node
    addresses and a C++ source path. When the exception carries a query/analytics
    error context, the server's own ``code: message`` is returned instead.
    Anything else is ``str(error)``. The result is capped at
    ``MAX_ERROR_MESSAGE_LENGTH`` characters.

    The context is read by duck typing (``error_context``) because this module is
    shared with the Operational Insights server, whose process must not import the
    ``couchbase`` SDK.
    """
    context = getattr(error, "error_context", None)
    code = getattr(context, "first_error_code", None)
    message = getattr(context, "first_error_message", None)
    if code is not None or message:
        return _truncate(f"{code}: {message}" if code is not None else str(message))
    return _truncate(str(error))


def tool_error(error: Exception | str, **fields: Any) -> dict[str, Any]:
    """Build a failure envelope: ``{"success": False, "error": <short message>, **fields}``.

    SDK exceptions are condensed by :func:`error_message`.
    """
    return {"success": False, "error": error_message(error), **fields}
