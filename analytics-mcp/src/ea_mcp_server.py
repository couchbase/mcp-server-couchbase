"""Couchbase Enterprise Analytics (EA) prototype MCP server.

Deliberately minimal: no OAuth, no scope enforcement, no read-only-mode
toggle, no telemetry/confirmation wrapping. Just enough plumbing to register
the EA tools (see ea_mcp.tools.TOOLS) and let unit/integration tests run
against them.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import click
from fastmcp import Context, FastMCP
from fastmcp.resources import ResourceTemplate
from fastmcp.resources.resource import ResourceContent, ResourceResult
from fastmcp.tools import FunctionTool

from ea_mcp.connection import AppContext, connect_to_analytics_cluster
from ea_mcp.resources import (
    RESULT_MIME_TYPE,
    RESULT_URI_TEMPLATE,
    read_saved_result,
)
from ea_mcp.result_config import (
    DEFAULT_STORAGE_MAX_BYTES,
    DEFAULT_TRUNCATE_BYTES,
    ResultConfig,
)
from ea_mcp.result_store import ResultStore
from ea_mcp.tools import TOOL_ANNOTATIONS, TOOLS

logger = logging.getLogger("ea-mcp-server")

MCP_SERVER_NAME = "couchbase-enterprise-analytics-mcp"


def _register_result_resource(mcp: FastMCP, store: ResultStore) -> None:
    """Register the ea://results/{result_id} resource template.

    One RFC 6570 template serves every read: a bare URI returns the whole
    result, and the optional ``{?offset,limit}`` query parameters return a
    slice of it.

    The store is closed over (one per process), while the handle registry is
    read off the injected ``Context`` because it is created per-lifespan.
    """

    def read_result(
        result_id: str,
        ctx: Context,
        offset: int = 0,
        limit: int | None = None,
    ) -> ResourceResult:
        """Read a saved query result as JSON Lines, or a slice of one.

        Pass offset and/or limit to page through a large result, e.g.
        ea://results/<id>?offset=0&limit=100. Omit both for the whole result.
        """
        registry = ctx.request_context.lifespan_context.handle_registry
        jsonl = read_saved_result(store, registry, result_id, offset, limit)
        # Wrapped in ResourceContent to carry the mime type: a bare str is
        # normalized to text/plain regardless of the template's mime_type.
        return ResourceResult([ResourceContent(jsonl, mime_type=RESULT_MIME_TYPE)])

    mcp.add_template(
        ResourceTemplate.from_function(
            read_result,
            uri_template=RESULT_URI_TEMPLATE,
            name="saved_query_result",
            description=(
                "A query result too large to return inline. Read the whole "
                "result, or page through it with ?offset=&limit=. Returns "
                "JSON Lines: one JSON object per row."
            ),
            mime_type=RESULT_MIME_TYPE,
        )
    )
    logger.info(f"Registered resource template {RESULT_URI_TEMPLATE}")


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
@click.option(
    "--save-large-results",
    envvar="EA_MCP_SAVE_LARGE_RESULTS",
    is_flag=True,
    default=False,
    help=(
        "Keep oversized query results on the server so clients can read them "
        "back as ea://results/... resources. Off by default, in which case "
        "oversized results are only truncated."
    ),
)
@click.option(
    "--result-truncate-bytes",
    envvar="EA_MCP_RESULT_TRUNCATE_BYTES",
    default=DEFAULT_TRUNCATE_BYTES,
    type=int,
    help="Max bytes of query results returned inline before truncating",
)
@click.option(
    "--result-storage-max-bytes",
    envvar="EA_MCP_RESULT_STORAGE_MAX_BYTES",
    default=DEFAULT_STORAGE_MAX_BYTES,
    type=int,
    help=(
        "Total disk budget for saved results; the least recently used are "
        "deleted to stay within it"
    ),
)
@click.option(
    "--result-storage-path",
    envvar="EA_MCP_RESULT_STORAGE_PATH",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for saved results (default: a temporary directory)",
)
def main(
    connection_string: str,
    username: str,
    password: str,
    transport: str,
    host: str,
    port: int,
    save_large_results: bool,
    result_truncate_bytes: int,
    result_storage_max_bytes: int,
    result_storage_path: Path | None,
) -> None:
    """Couchbase Enterprise Analytics MCP server (prototype)."""
    logging.basicConfig(level=logging.INFO)

    result_config = ResultConfig(
        save_large_results=save_large_results,
        truncate_bytes=result_truncate_bytes,
        storage_max_bytes=result_storage_max_bytes,
        storage_path=result_storage_path,
    )

    # Built once per process and closed over by the lifespan and the resource
    # reader, so both see the same store. Only created when saving is enabled:
    # the default configuration touches no disk at all.
    result_store: ResultStore | None = None
    if save_large_results:
        storage_path = result_storage_path or Path(
            tempfile.mkdtemp(prefix="ea-mcp-results-")
        )
        result_store = ResultStore(storage_path, result_storage_max_bytes)
        # Reclaim files left behind by a previous process: the index is in
        # memory, so a crash orphans its files permanently otherwise.
        result_store.sweep_orphans()
        logger.info(
            f"Large-result saving enabled: {storage_path} "
            f"(budget {result_storage_max_bytes} bytes, "
            f"truncating at {result_truncate_bytes} bytes)"
        )

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        cluster = connect_to_analytics_cluster(connection_string, username, password)
        try:
            yield AppContext(
                cluster=cluster,
                result_config=result_config,
                result_store=result_store,
            )
        finally:
            logger.info("Closing Enterprise Analytics MCP server")
            cluster.shutdown()

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan)

    for tool in TOOLS:
        annotations = TOOL_ANNOTATIONS.get(tool.__name__)
        tool_obj = FunctionTool.from_function(tool, annotations=annotations)
        mcp.add_tool(tool_obj)

    logger.info(f"Registered {len(TOOLS)} tool(s)")

    if result_store is not None:
        _register_result_resource(mcp, result_store)
    if transport == "http":
        mcp.run(transport="streamable-http", host=host, port=port, show_banner=False)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
