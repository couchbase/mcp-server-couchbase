"""Enterprise Analytics MCP Server.

A FastMCP server exposing Couchbase Enterprise Analytics' Server Async Request
API (start_query / QueryHandle) as five MCP tools.
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
    DEFAULT_EA_ENDPOINT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TRANSPORT,
    MCP_SERVER_NAME,
    NETWORK_TRANSPORTS,
    NETWORK_TRANSPORTS_SDK_MAPPING,
)
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
def main(endpoint, username, password, transport, host, port):
    """Enterprise Analytics MCP Server."""

    settings = {
        "endpoint": endpoint,
        "username": username,
        "password": password,
        "transport": transport,
        "host": host,
        "port": port,
    }

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        logger.info("Enterprise Analytics MCP server starting (endpoint=%s)", endpoint)
        app_context = AppContext(
            cluster_provider=StaticEAClusterProvider(settings=settings),
            settings=settings,
        )
        try:
            yield app_context
        finally:
            if app_context.cluster_provider:
                app_context.cluster_provider.close()
            logger.info("Closing Enterprise Analytics MCP server")

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
