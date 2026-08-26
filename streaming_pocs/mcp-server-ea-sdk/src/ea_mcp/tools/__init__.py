"""Enterprise Analytics streaming MCP tools."""

from collections.abc import Callable

from mcp.types import ToolAnnotations

from .streaming_query import (
    close_query_stream,
    fetch_next_rows,
    list_query_streams,
    stream_query_results,
)

# The row-streaming tools.
STREAMING_QUERY_TOOLS = [
    stream_query_results,
    fetch_next_rows,
    close_query_stream,
    list_query_streams,
]

ALL_TOOLS = list(STREAMING_QUERY_TOOLS)

# Hints for MCP clients. Opening a stream and advancing a cursor consume
# server-side resources and move cursor position, so neither is marked
# read-only; listing is a pure read and closing is destructive.
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "stream_query_results": ToolAnnotations(),
    "fetch_next_rows": ToolAnnotations(),
    "close_query_stream": ToolAnnotations(destructiveHint=True),
    "list_query_streams": ToolAnnotations(readOnlyHint=True),
}


def get_tools() -> list[Callable]:
    """Return the list of tools to register."""
    return list(ALL_TOOLS)


__all__ = [
    "stream_query_results",
    "fetch_next_rows",
    "close_query_stream",
    "list_query_streams",
    "STREAMING_QUERY_TOOLS",
    "ALL_TOOLS",
    "TOOL_ANNOTATIONS",
    "get_tools",
]
