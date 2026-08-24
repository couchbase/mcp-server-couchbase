"""Enterprise Analytics MCP Server — Tasks edition.

Exposes EA queries through the MCP **Tasks** protocol: a single blocking
`run_query` tool registered with task=True. The client submits it as a task,
gets a taskId immediately, and polls/fetches via the protocol — the MCP layer
provides the async lifecycle, so we don't hand-build start/status/fetch tools.

Task state/results are stored by docket. Default backend is memory:// (single
process, no Redis). Set FASTMCP_DOCKET_URL=redis://host:port/db to coordinate
tasks across multiple replicas.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from ea_mcp.tools import run_query
from ea_mcp.utils import AppContext, close_cluster
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
@click.option("--endpoint", envvar="EA_ENDPOINT", default=DEFAULT_EA_ENDPOINT,
              help="EA query service endpoint (e.g. http://localhost:9095).")
@click.option("--username", envvar="EA_USERNAME", help="EA username.")
@click.option("--password", envvar="EA_PASSWORD", help="EA password.")
@click.option("--transport", envvar="EA_MCP_TRANSPORT",
              type=click.Choice(ALLOWED_TRANSPORTS), default=DEFAULT_TRANSPORT,
              help="Transport mode (stdio, http, or sse).")
@click.option("--host", envvar="EA_MCP_HOST", default=DEFAULT_HOST,
              help="Host (network transports only).")
@click.option("--port", envvar="EA_MCP_PORT", default=DEFAULT_PORT, type=int,
              help="Port (network transports only).")
def main(endpoint, username, password, transport, host, port):
    """Enterprise Analytics MCP Server (Tasks edition)."""

    settings = {
        "endpoint": endpoint, "username": username, "password": password,
        "transport": transport, "host": host, "port": port,
    }

    # Publish connection settings to the environment so the process-level
    # cluster singleton (used by the detached task worker) can read them. The
    # task worker has no request context, so it can't get settings via ctx.
    os.environ["EA_ENDPOINT"] = endpoint or DEFAULT_EA_ENDPOINT
    if username:
        os.environ["EA_USERNAME"] = username
    if password:
        os.environ["EA_PASSWORD"] = password

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        logger.info("EA Tasks MCP server starting (endpoint=%s)", endpoint)
        app_context = AppContext(settings=settings)
        try:
            yield app_context
        finally:
            close_cluster()
            logger.info("Closing EA Tasks MCP server")

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan)

    # Register run_query as an MCP Task. task=True makes it submittable via the
    # Tasks protocol. The tool is async def and offloads the blocking
    # execute_query() to a thread itself (see tools/query.py), so run_in_thread
    # is not needed here.
    tool_obj = FunctionTool.from_function(run_query, task=True)
    mcp.add_tool(tool_obj)
    logger.info("Registered run_query as an MCP Task")

    sdk_transport = NETWORK_TRANSPORTS_SDK_MAPPING.get(transport, transport)
    run_kwargs = {"host": host, "port": port} if transport in NETWORK_TRANSPORTS else {}
    mcp.run(transport=sdk_transport, show_banner=False, **run_kwargs)


if __name__ == "__main__":
    main()
