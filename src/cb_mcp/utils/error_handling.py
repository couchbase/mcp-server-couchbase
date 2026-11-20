"""
Uniform failure reporting for tools.

Tools raise whatever the failure is, in whatever words they choose, and may keep
their own ``try``/``except`` to log context or translate an exception. This
wrapper is the last stop before FastMCP: it turns the exception into a
``ToolError`` so the MCP result carries ``isError: true`` and the audit layer
records a failed call, and it is the only place that caps the message.

The conversion has to happen here, inside the tool, rather than in a
middleware: FastMCP wraps a raw exception as ``Error calling tool 'x': {exc}``
before middleware sees it, and that text is the full, unbounded ``str()`` of
the exception (SDK query errors carry the statement, bound vectors and node
addresses). FastMCP already logs every failing tool with its name and
traceback, so this wrapper does not log.

``BaseException`` subclasses such as ``asyncio.CancelledError`` are not caught.
"""

import functools
import inspect
import json
from collections.abc import Callable

from fastmcp.exceptions import ToolError
from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from .constants import MAX_ERROR_MESSAGE_LENGTH

_TRUNCATION_SUFFIX = "... [truncated]"


def _truncate(text: str) -> str:
    """Cap ``text`` at ``MAX_ERROR_MESSAGE_LENGTH`` characters, suffix included."""
    if len(text) <= MAX_ERROR_MESSAGE_LENGTH:
        return text
    return (
        text[: MAX_ERROR_MESSAGE_LENGTH - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
    )


def _short_message(error: Exception) -> str:
    """A short message for ``error``; shorter is preferred over complete.

    ``str()`` of a Couchbase SDK exception is a dump of hundreds to thousands of
    characters: the SQL++ statement, every bound parameter (for vector tools the
    whole embedding), the search request body, node addresses and a C++ source
    path. An exception raised by the SDK core carries an ``error_context``, so it
    is reduced to the class plus whatever short reason it exposes:

    * query/analytics: ``<Type> <server code>: <server message>``
    * search: ``<Type> (ec=<code>): <reason>``, the reason being the ``error`` of
      the JSON response body, which also repeats the request
    * anything else with a context: ``<Type> (ec=<code>): <message>``

    The class is always kept because the reason alone no longer names it.
    Anything without a context (``ValueError`` from a guard, a plain
    ``RuntimeError``) is ``str(error)``.

    The context is read by duck typing because this module is shared with the
    Operational Insights server, whose process must not import the ``couchbase``
    SDK.
    """
    context = getattr(error, "error_context", None)
    if context is None:
        return str(error)
    name = type(error).__name__
    code = getattr(context, "first_error_code", None)
    message = getattr(context, "first_error_message", None)
    if code is not None or message:
        return f"{name} {code}: {message}" if code is not None else f"{name} {message}"
    try:
        reason = json.loads(getattr(context, "response_body", None) or "")["error"]
    except (ValueError, KeyError, TypeError):
        reason = None
    ec = getattr(error, "error_code", None)
    detail = reason or getattr(error, "message", None)
    if ec is None and not detail:
        return str(error)
    label = f"{name} (ec={ec})" if ec is not None else name
    return f"{label}: {detail}" if detail else label


def wrap_with_error_handling(fn: Callable) -> Callable:
    """Wrap a tool so any failure surfaces as a ``ToolError`` of bounded length.

    The message comes from :func:`_short_message` and is always passed through
    :func:`_truncate`, so no message exceeds ``MAX_ERROR_MESSAGE_LENGTH``
    whoever worded it. The original exception stays on ``__cause__``. Sync tools
    run in FastMCP's thread pool, as they would unwrapped: this wrapper is async,
    so calling a blocking SDK function directly would stall the event loop.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            if inspect.iscoroutinefunction(fn):
                return await fn(*args, **kwargs)
            return await call_sync_fn_in_threadpool(fn, *args, **kwargs)
        except Exception as e:
            raise ToolError(_truncate(_short_message(e))) from e

    return wrapper
