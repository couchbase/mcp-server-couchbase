"""Enterprise Analytics Streaming MCP Server.

A FastMCP server exposing Couchbase Enterprise Analytics' row-streaming query
API (``execute_query().rows()``) as four MCP tools, so a client can walk a very
large result set a batch at a time instead of loading it all into memory.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from ea_mcp.tools import TOOL_ANNOTATIONS, get_tools
from ea_mcp.utils import AppContext
from ea_mcp.utils.constants import (
    ALLOWED_TRANSPORTS,
    CURSOR_REAPER_INTERVAL_SECONDS,
    DEFAULT_CURSOR_IDLE_TTL_SECONDS,
    DEFAULT_EA_ENDPOINT,
    DEFAULT_HOST,
    DEFAULT_MAX_OPEN_STREAMS,
    DEFAULT_PORT,
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    DEFAULT_TRANSPORT,
    MCP_SERVER_NAME,
    NETWORK_TRANSPORTS,
    NETWORK_TRANSPORTS_SDK_MAPPING,
)
from ea_mcp.utils.cursor_registry import CursorRegistry
from ea_mcp.utils.reaper import CursorReaper
from providers.static import StaticEAClusterProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(MCP_SERVER_NAME)


@click.command(context_settings={"show_default": True})
@click.option(
    "--endpoint",
    envvar="EA_ENDPOINT",
    default=DEFAULT_EA_ENDPOINT,
    help="Enterprise Analytics query service endpoint (e.g. http://localhost:9095). "
    "This is the query port, NOT the web console.",
)
@click.option(
    "--username",
    envvar="EA_USERNAME",
    help="Enterprise Analytics username.",
)
@click.option(
    "--password",
    envvar="EA_PASSWORD",
    help="Enterprise Analytics password.",
)
@click.option(
    "--query-timeout",
    envvar="EA_QUERY_TIMEOUT",
    default=DEFAULT_QUERY_TIMEOUT_SECONDS,
    type=float,
    help="Whole-request deadline for a streaming query, in seconds. NOT an idle "
    "timeout: it keeps running while a cursor sits paused between tool calls, "
    "so a stream read more slowly than this expires mid-iteration.",
)
@click.option(
    "--max-open-streams",
    envvar="EA_MAX_OPEN_STREAMS",
    default=DEFAULT_MAX_OPEN_STREAMS,
    type=int,
    help="Maximum concurrently open cursors. Each holds an HTTP connection, a "
    "thread-pool slot, and the SDK's ~100-row parse-ahead buffer.",
)
@click.option(
    "--cursor-idle-ttl",
    envvar="EA_CURSOR_IDLE_TTL",
    default=DEFAULT_CURSOR_IDLE_TTL_SECONDS,
    type=float,
    help="Seconds of inactivity after which an open cursor is reaped. This is "
    "what reclaims cursors a client never returns to.",
)
@click.option(
    "--transport",
    envvar="EA_MCP_TRANSPORT",
    type=click.Choice(ALLOWED_TRANSPORTS),
    default=DEFAULT_TRANSPORT,
    help="Transport mode (stdio, http, or sse).",
)
@click.option(
    "--host",
    envvar="EA_MCP_HOST",
    default=DEFAULT_HOST,
    help="Host to run the server on (network transports only).",
)
@click.option(
    "--port",
    envvar="EA_MCP_PORT",
    default=DEFAULT_PORT,
    type=int,
    help="Port to run the server on (network transports only).",
)
def main(
    endpoint,
    username,
    password,
    query_timeout,
    max_open_streams,
    cursor_idle_ttl,
    transport,
    host,
    port,
):
    """Enterprise Analytics Streaming MCP Server."""

    settings = {
        "endpoint": endpoint,
        "username": username,
        "password": password,
        "query_timeout": query_timeout,
        "max_open_streams": max_open_streams,
        "cursor_idle_ttl": cursor_idle_ttl,
        "transport": transport,
        "host": host,
        "port": port,
    }

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        logger.info(
            "EA streaming MCP server starting (endpoint=%s, max_open_streams=%d)",
            endpoint,
            max_open_streams,
        )
        registry = CursorRegistry(
            max_open_streams=max_open_streams,
            idle_ttl_seconds=cursor_idle_ttl,
        )
        reaper = CursorReaper(
            registry=registry,
            interval_seconds=CURSOR_REAPER_INTERVAL_SECONDS,
        )
        reaper.start()
        app_context = AppContext(
            cluster_provider=StaticEAClusterProvider(settings=settings),
            cursor_registry=registry,
            settings=settings,
        )
        try:
            yield app_context
        finally:
            reaper.stop()
            # Cancel open streams before dropping the cluster, so EA is told
            # they are finished rather than having sockets closed underneath it.
            closed = registry.close_all()
            if closed:
                logger.info("Closed %d open cursor(s) at shutdown", closed)
            if app_context.cluster_provider:
                app_context.cluster_provider.close()
            logger.info("Closing EA streaming MCP server")

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan)

    tools = get_tools()
    for tool in tools:
        annotations = TOOL_ANNOTATIONS.get(tool.__name__)
        tool_obj = FunctionTool.from_function(tool, annotations=annotations)
        mcp.add_tool(tool_obj)
    logger.info("Registered %d tool(s)", len(tools))

    sdk_transport = NETWORK_TRANSPORTS_SDK_MAPPING.get(transport, transport)
    run_kwargs = {"host": host, "port": port} if transport in NETWORK_TRANSPORTS else {}
    mcp.run(transport=sdk_transport, show_banner=False, **run_kwargs)


if __name__ == "__main__":
    main()
