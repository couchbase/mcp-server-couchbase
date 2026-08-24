"""Query tool for Enterprise Analytics, exposed as an MCP Task.

Design contrast with the other two servers
------------------------------------------
The `mcp-server-ea` (SDK+registry) and `mcp-server-ea-rest` (stateless) servers
implement EA's *async* request API by hand: 5 tools (start/status/results/
discard/cancel) plus a way to carry the query handle between calls.

This server takes the opposite approach. It uses EA's plain **blocking**
`cluster.execute_query()` — which runs the query to completion in one call — and
lets the **MCP Tasks protocol** provide the async "call-now, fetch-later"
lifecycle instead:

    * The tool is registered with task=True.
    * The MCP client submits it as a task and immediately gets a taskId.
    * FastMCP runs this function in the background (docket backend).
    * The client polls tasks/get and retrieves the rows via tasks/result.

So the tool body is dead simple: run the query, return the rows. All the
async-ness lives at the protocol layer, not in our code.

Why this tool is ``async def`` (unlike the other servers' ``def`` tools)
----------------------------------------------------------------------
FastMCP's task backend runs tasks in an async worker, so a task-enabled tool
MUST be a coroutine (`async def`) — a plain `def` is rejected. But EA's
`execute_query()` is *blocking*. So we do the correct async-with-blocking-I/O
pattern: an `async def` tool that offloads the blocking call to a worker thread
via ``anyio.to_thread.run_sync``. That keeps the event loop free (the blocking
call runs off-loop) while still presenting the coroutine the task worker needs.

This is exactly the "when does async def make sense" case: the function is
awaitable for the framework, and the blocking work is pushed to a thread.
"""

import logging
from typing import Any

import anyio
from fastmcp import Context

from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.query")


def _execute_blocking(cluster, statement: str) -> dict[str, Any]:
    """Run the blocking query and shape the result. Called on a worker thread."""
    result = cluster.execute_query(statement)
    rows = result.get_all_rows()
    metadata: dict[str, Any] = {}
    try:
        meta = result.metadata()
        metadata["request_id"] = meta.request_id()
        metrics = meta.metrics()
        metadata["metrics"] = {
            "result_count": metrics.result_count(),
            "result_size": metrics.result_size(),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("Partial metadata read: %s", e)
    return {"rows": rows, "metadata": metadata}


async def run_query(ctx: Context, statement: str) -> dict[str, Any]:
    """Run a SQL++ query on Enterprise Analytics and return all rows.

    Registered as an MCP Task: when the client submits it, it returns a taskId
    immediately and runs in the background. Poll with tasks/get and retrieve the
    result with tasks/result — no separate status/fetch tools needed.

    Args:
        statement: The SQL++ statement to execute.

    Returns:
        {rows, metadata} once the query completes.
    """
    cluster = get_cluster_connection(ctx)
    logger.info("Executing query (as task): %s", statement)

    # Offload the blocking SDK call to a worker thread so the event loop (and
    # the task worker) stays free while the query runs on EA.
    out = await anyio.to_thread.run_sync(_execute_blocking, cluster, statement)

    logger.info("Query complete: %d row(s)", len(out["rows"]))
    return out
