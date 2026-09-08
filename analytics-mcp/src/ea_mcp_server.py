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
from ea_mcp.tools.query_poc import (
    DEFAULT_RESOURCE_PAGE_ROWS,
    RESULT_PAGE_RESOURCE_URI,
    RESULT_RESOURCE_URI,
    read_query_poc_result_page_resource,
    read_query_poc_result_resource,
)

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
def main(connection_string: str, username: str, password: str) -> None:
    """Couchbase Enterprise Analytics MCP server (prototype)."""
    logging.basicConfig(level=logging.INFO)

    # The POC result resource is read outside any tool call, so it cannot take
    # the store off a tool ``Context``. The lifespan stashes the live
    # AppContext here and the resource reader closes over it.
    app_context: dict[str, AppContext] = {}

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        cluster = connect_to_analytics_cluster(connection_string, username, password)
        context = AppContext(cluster=cluster)
        app_context["current"] = context
        try:
            yield context
        finally:
            logger.info("Closing Enterprise Analytics MCP server")
            app_context.pop("current", None)
            cluster.shutdown()

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan)

    for tool in TOOLS:
        annotations = TOOL_ANNOTATIONS.get(tool.__name__)
        tool_obj = FunctionTool.from_function(tool, annotations=annotations)
        mcp.add_tool(tool_obj)

    @mcp.resource(
        RESULT_RESOURCE_URI,
        name="query_poc_result",
        description=(
            "The complete row set of a query run by run_query_poc_resource, "
            "as JSON. Large: it holds every row the query matched."
        ),
        mime_type="application/json",
    )
    def query_poc_result(result_id: str) -> str:
        context = app_context.get("current")
        if context is None:
            raise RuntimeError("Server is not running; no result store available.")
        return read_query_poc_result_resource(result_id, context.result_store)

    @mcp.resource(
        RESULT_PAGE_RESOURCE_URI,
        name="query_poc_result_page",
        description=(
            "One page of a query run by run_query_poc_resource, as JSON. "
            "Takes offset and limit query parameters, e.g. "
            "?offset=500&limit=200. Prefer this over the whole-result "
            "resource unless you genuinely need every row."
        ),
        mime_type="application/json",
    )
    def query_poc_result_page(
        result_id: str, offset: int = 0, limit: int = DEFAULT_RESOURCE_PAGE_ROWS
    ) -> str:
        context = app_context.get("current")
        if context is None:
            raise RuntimeError("Server is not running; no result store available.")
        return read_query_poc_result_page_resource(
            result_id, context.result_store, offset=offset, limit=limit
        )

    logger.info(f"Registered {len(TOOLS)} tool(s) and 2 resource templates")
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
