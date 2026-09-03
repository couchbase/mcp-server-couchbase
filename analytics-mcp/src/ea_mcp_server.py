"""Couchbase Enterprise Analytics (EA) prototype MCP server.

Deliberately minimal: no OAuth, no scope enforcement, no read-only-mode
toggle, no telemetry/confirmation wrapping. Just enough plumbing to register
the EA tools (see ea_mcp.tools.TOOLS) and let unit/integration tests run
against them.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from ea_mcp.connection import AppContext, connect_to_analytics_cluster
from ea_mcp.tools import TOOL_ANNOTATIONS, TOOLS

logger = logging.getLogger("ea-mcp-server")

MCP_SERVER_NAME = "couchbase-enterprise-analytics-mcp"


@click.command()
@click.option(
    "--connection-string",
    envvar="EA_CONNECTION_STRING",
    required=True,
    help="Enterprise Analytics connection string, e.g. http://localhost:8095",
)
@click.option(
    "--username",
    envvar="EA_USERNAME",
    required=True,
    help="Enterprise Analytics username",
)
@click.option(
    "--password",
    envvar="EA_PASSWORD",
    required=True,
    help="Enterprise Analytics password",
)
@click.option(
    "--transport",
    envvar="EA_MCP_TRANSPORT",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    help="MCP transport to serve on",
)
@click.option(
    "--host",
    envvar="EA_MCP_HOST",
    default="127.0.0.1",
    help="Host to bind when --transport=http",
)
@click.option(
    "--port",
    envvar="EA_MCP_PORT",
    default=8000,
    type=int,
    help="Port to bind when --transport=http",
)
def main(
    connection_string: str,
    username: str,
    password: str,
    transport: str,
    host: str,
    port: int,
) -> None:
    """Couchbase Enterprise Analytics MCP server (prototype)."""
    logging.basicConfig(level=logging.INFO)

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        cluster = connect_to_analytics_cluster(connection_string, username, password)
        try:
            yield AppContext(cluster=cluster)
        finally:
            logger.info("Closing Enterprise Analytics MCP server")
            cluster.shutdown()

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan)

    for tool in TOOLS:
        annotations = TOOL_ANNOTATIONS.get(tool.__name__)
        tool_obj = FunctionTool.from_function(tool, annotations=annotations)
        mcp.add_tool(tool_obj)

    logger.info(f"Registered {len(TOOLS)} tool(s)")
    if transport == "http":
        mcp.run(transport="http", host=host, port=port, show_banner=False)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
