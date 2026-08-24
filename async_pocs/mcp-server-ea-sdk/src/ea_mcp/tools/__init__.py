"""Enterprise Analytics MCP tools."""

from collections.abc import Callable

from mcp.types import ToolAnnotations

from .async_query import (
    cancel_async_query,
    discard_async_query_results,
    get_async_query_results,
    get_async_query_status,
    list_async_queries,
    run_query_async,
)

# The Server Async Request API tools.
ASYNC_QUERY_TOOLS = [
    run_query_async,
    get_async_query_status,
    get_async_query_results,
    discard_async_query_results,
    cancel_async_query,
    list_async_queries,
]

ALL_TOOLS = list(ASYNC_QUERY_TOOLS)

# Hints for MCP clients. Status/results reads are read-only; start/discard/
# cancel change server-side state.
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "run_query_async": ToolAnnotations(),
    "get_async_query_status": ToolAnnotations(readOnlyHint=True),
    "get_async_query_results": ToolAnnotations(readOnlyHint=True),
    "discard_async_query_results": ToolAnnotations(destructiveHint=True),
    "cancel_async_query": ToolAnnotations(destructiveHint=True),
    "list_async_queries": ToolAnnotations(readOnlyHint=True),
}


def get_tools() -> list[Callable]:
    """Return the list of tools to register."""
    return list(ALL_TOOLS)


__all__ = [
    "run_query_async",
    "get_async_query_status",
    "get_async_query_results",
    "discard_async_query_results",
    "cancel_async_query",
    "list_async_queries",
    "ASYNC_QUERY_TOOLS",
    "ALL_TOOLS",
    "TOOL_ANNOTATIONS",
    "get_tools",
]
