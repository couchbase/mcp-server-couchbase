"""Stateless Enterprise Analytics MCP Server (REST-based).

A FastMCP server exposing EA's Server Async Request API as five MCP tools,
implemented directly against EA's REST endpoints. Holds no per-query state, so
it is correct across multiple replicas and across restarts.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from ea_mcp.tools import TOOL_ANNOTATIONS, get_tools
from ea_mcp.utils import AppContext, EARestClient
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(MCP_SERVER_NAME)


@click.command(context_settings={"show_default": True})
@click.option(
    "--endpoint",
    envvar="EA_ENDPOINT",
    default=DEFAULT_EA_ENDPOINT,
    help="EA query service endpoint (e.g. http://localhost:9095). Query port, "
    "NOT the web console.",
)
@click.option("--username", envvar="EA_USERNAME", help="EA username.")
@click.option("--password", envvar="EA_PASSWORD", help="EA password.")
@click.option(
    "--tls-verify/--no-tls-verify",
    envvar="EA_TLS_VERIFY",
    default=True,
    help="Verify the EA TLS certificate (https endpoints). Disable only for "
    "local self-signed setups.",
)
@click.option(
    "--transport",
    envvar="EA_MCP_TRANSPORT",
    type=click.Choice(ALLOWED_TRANSPORTS),
    default=DEFAULT_TRANSPORT,
    help="Transport mode (stdio, http, or sse).",
)
@click.option("--host", envvar="EA_MCP_HOST", default=DEFAULT_HOST,
              help="Host (network transports only).")
@click.option("--port", envvar="EA_MCP_PORT", default=DEFAULT_PORT, type=int,
              help="Port (network transports only).")
def main(endpoint, username, password, tls_verify, transport, host, port):
    """Stateless Enterprise Analytics MCP Server (REST)."""

    settings = {
        "endpoint": endpoint,
        "username": username,
        "transport": transport,
        "host": host,
        "port": port,
        "tls_verify": tls_verify,
    }

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        logger.info("EA REST MCP server starting (endpoint=%s)", endpoint)
        # The client is a connection pool only — no per-query state. Built even
        # if EA is momentarily down; the first tool call surfaces any error.
        ea_client = None
        if username and password:
            ea_client = EARestClient(
                endpoint, username, password, verify=tls_verify
            )
        app_context = AppContext(ea_client=ea_client, settings=settings)
        try:
            yield app_context
        finally:
            if app_context.ea_client is not None:
                app_context.ea_client.close()
            logger.info("Closing EA REST MCP server")

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan)

    tools = get_tools()
    for tool in tools:
        annotations = TOOL_ANNOTATIONS.get(tool.__name__)
        mcp.add_tool(FunctionTool.from_function(tool, annotations=annotations))
    logger.info("Registered %d tool(s)", len(tools))

    sdk_transport = NETWORK_TRANSPORTS_SDK_MAPPING.get(transport, transport)
    run_kwargs = {"host": host, "port": port} if transport in NETWORK_TRANSPORTS else {}
    mcp.run(transport=sdk_transport, show_banner=False, **run_kwargs)


if __name__ == "__main__":
    main()
