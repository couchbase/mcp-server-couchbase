"""
Per-tool OAuth scope enforcement.

FastMCP's ``JWTVerifier`` exposes only a single, server-wide ``required_scopes``
gate. The Couchbase MCP server needs finer granularity: ``couchbase-mcp:read``
should permit read-only tools, ``couchbase-mcp:write`` should permit KV
mutations only, and (deliberately) neither scope alone unlocks the other —
SCOPE_WRITE on its own cannot reach read tools or SQL++. This module wraps
each tool with a scope check that runs inside FastMCP's request context after
token validation has already populated the access token.

The wrapper is a no-op when no token is present in context (stdio transport
or OAuth not configured), so wrapping is safe to apply unconditionally and
the same tool registration path serves both modes.
"""

import functools
import inspect
import logging
from collections.abc import Callable

from fastmcp.server.dependencies import get_access_token

from .constants import MCP_SERVER_NAME, SCOPE_READ, SCOPE_WRITE

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.scope_enforcement")

# Per-tool hints appended to the PermissionError message when a token is
# missing required scopes. Use these to explain *why* a tool requires a
# particular scope when the literal "missing X" line under-explains the
# situation. Tools not listed here fall back to the generic message.
TOOL_SCOPE_HINTS: dict[str, str] = {
    "run_sql_plus_plus_query": (
        f"A '{SCOPE_WRITE}'-only token cannot invoke SQL++; '{SCOPE_READ}' is required."
    ),
}


def required_scopes_for_tool(
    tool_name: str,
    *,
    write_tool_names: set[str],
) -> set[str]:
    """Return the set of scopes a token must hold to invoke ``tool_name``.

    Categorization rule (deliberately strict, matches the spec):
      - Names in ``write_tool_names`` (the KV mutation tools) require
        ``SCOPE_WRITE`` and ONLY ``SCOPE_WRITE``.
      - Every other tool — including ``run_sql_plus_plus_query`` and other
        read-only tools — requires ``SCOPE_READ``.

    A token holding only ``SCOPE_WRITE`` therefore cannot reach SQL++ or any
    read tool. Full access requires both scopes.
    """
    if tool_name in write_tool_names:
        return {SCOPE_WRITE}
    return {SCOPE_READ}


def wrap_with_scope_check(
    fn: Callable,
    required_scopes: set[str],
    hint: str | None = None,
) -> Callable:
    """Wrap a tool function with a per-call scope check.

    The wrapper consults FastMCP's request-scoped access token via
    ``fastmcp.server.dependencies.get_access_token``. If no token is present
    (stdio / OAuth disabled), the check is skipped — letting the same wrapped
    function serve both authenticated HTTP and unauthenticated stdio runs
    without branching at registration time.

    Pass ``hint`` to append a tool-specific explanation to the rejection
    message (e.g. for tools whose scope semantics are non-obvious from the
    literal ``required_scopes`` set, such as SQL++ which is classified read
    but mutates only with an additional write scope).

    Raises ``PermissionError`` when a token is present but missing one or
    more required scopes; FastMCP surfaces this as a tool-call error to the
    client.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        token = get_access_token()
        if token is not None:
            held = set(token.scopes or [])
            missing = required_scopes - held
            if missing:
                msg = (
                    f"Tool '{fn.__name__}' requires scope(s) "
                    f"{sorted(required_scopes)}; token is missing "
                    f"{sorted(missing)}."
                )
                if hint:
                    msg = f"{msg} {hint}"
                logger.warning(msg)
                raise PermissionError(msg)

        if inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return fn(*args, **kwargs)

    return wrapper
