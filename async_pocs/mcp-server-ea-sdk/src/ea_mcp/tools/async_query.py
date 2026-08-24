"""Server Async Request API tools for Couchbase Enterprise Analytics.

These five tools expose EA's handle-based async query flow (EA 2.2+, analytics
SDK >= 1.1.0):

    run_query_async            -> cluster.start_query(statement)
    get_async_query_status     -> handle.fetch_status().results_ready()
    get_async_query_results    -> status.result_handle().fetch_results()
    discard_async_query_results-> result_handle.discard_results()
    cancel_async_query         -> handle.cancel()

The live SDK handle objects cannot be serialized or sent to the client, so they
are kept in a server-side ``HandleRegistry`` and referenced by an opaque
``query_handle`` token that is returned to the client and passed back on every
subsequent call. See handle_registry.py for the design and its single-process
caveat.

Tools are plain ``def`` (not ``async def``): the SDK's blocking handle calls
run on FastMCP's thread pool, so they must not wrap blocking I/O in an async
coroutine that would block the event loop.
"""

import logging
from typing import Any

from fastmcp import Context

from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection, get_handle_registry

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.async_query")


def run_query_async(ctx: Context, statement: str) -> dict[str, Any]:
    """Submit a long-running SQL++ query to Enterprise Analytics asynchronously.

    Starts the query via the Server Async Request API and returns immediately
    with an opaque ``query_handle`` token — it does NOT wait for results. Use
    ``get_async_query_status`` to poll whether results are ready, then
    ``get_async_query_results`` to retrieve them, ``discard_async_query_results``
    to release them without fetching, or ``cancel_async_query`` to cancel a
    still-running query.

    Args:
        statement: The SQL++ statement to execute.

    Returns:
        A dict with the ``query_handle`` token to pass to the other async tools.
    """
    cluster = get_cluster_connection(ctx)
    registry = get_handle_registry(ctx)

    logger.debug("Starting async query: %s", statement)
    handle = cluster.start_query(statement)
    token = registry.register(handle, statement)
    request_id = getattr(handle, "_request_id", None)
    logger.info("Started async query (token=%s, request_id=%s)", token, request_id)

    return {
        "query_handle": token,
        "request_id": request_id,
        "message": (
            "Query submitted. Poll get_async_query_status with this "
            "query_handle; retrieve rows with get_async_query_results once ready."
        ),
    }


def get_async_query_status(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Check whether an async query's results are ready.

    Calls ``fetch_status()`` on the tracked query handle and reports
    ``results_ready``. When ready, the result handle is cached server-side so
    ``get_async_query_results`` / ``discard_async_query_results`` can use it.

    Args:
        query_handle: The token returned by ``run_query_async``.

    Returns:
        A dict with ``ready`` (bool) and the ``query_handle``.
    """
    registry = get_handle_registry(ctx)
    entry = registry.get(query_handle)

    status = entry.handle.fetch_status()
    ready = status.results_ready()
    if ready:
        # Cache the result handle so fetch/discard reuse it.
        registry.set_result_handle(query_handle, status.result_handle())
    logger.info("Async query status (token=%s): ready=%s", query_handle, ready)

    return {"query_handle": query_handle, "ready": ready}


def get_async_query_results(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Retrieve rows and metadata for a ready async query.

    Fetches results via the result handle. Call ``get_async_query_status``
    first and ensure ``ready`` is true. After a successful fetch the query is
    complete; the token is evicted from the registry.

    Args:
        query_handle: The token returned by ``run_query_async``.

    Returns:
        A dict with ``rows`` and ``metadata`` (request id, metrics).
    """
    registry = get_handle_registry(ctx)
    entry = registry.get(query_handle)

    result_handle = entry.result_handle
    if result_handle is None:
        # Status hasn't been polled to readiness yet; derive it now.
        status = entry.handle.fetch_status()
        if not status.results_ready():
            return {
                "query_handle": query_handle,
                "ready": False,
                "message": (
                    "Results are not ready yet. Poll get_async_query_status "
                    "until ready before fetching."
                ),
            }
        result_handle = status.result_handle()

    result = result_handle.fetch_results()
    rows = result.get_all_rows()
    meta = result.metadata()

    metadata: dict[str, Any] = {}
    try:
        metadata["request_id"] = meta.request_id()
        metrics = meta.metrics()
        metadata["metrics"] = {
            "result_count": metrics.result_count(),
            "result_size": metrics.result_size(),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("Partial metadata read for %s: %s", query_handle, e)

    # Results consumed — the async query lifecycle is complete.
    registry.remove(query_handle)
    logger.info(
        "Fetched %d row(s) for async query (token=%s)", len(rows), query_handle
    )

    return {"query_handle": query_handle, "rows": rows, "metadata": metadata}


def discard_async_query_results(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Release server-side result buffers for a completed async query.

    Use this when you no longer need the rows — it frees EA's result buffers
    without fetching them. After discarding, the token is evicted.

    Args:
        query_handle: The token returned by ``run_query_async``.

    Returns:
        A dict confirming the discard.
    """
    registry = get_handle_registry(ctx)
    entry = registry.get(query_handle)

    result_handle = entry.result_handle
    if result_handle is None:
        status = entry.handle.fetch_status()
        if not status.results_ready():
            return {
                "query_handle": query_handle,
                "discarded": False,
                "message": (
                    "Results are not ready yet; nothing to discard. Cancel the "
                    "query with cancel_async_query if you want to stop it."
                ),
            }
        result_handle = status.result_handle()

    result_handle.discard_results()
    registry.remove(query_handle)
    logger.info("Discarded results for async query (token=%s)", query_handle)

    return {"query_handle": query_handle, "discarded": True}


def list_async_queries(ctx: Context) -> dict[str, Any]:
    """List all async queries currently tracked by this server.

    Use this to recover a ``query_handle`` you no longer have — for example if
    it scrolled out of the conversation context. Returns each tracked query's
    handle token, its statement, EA request id, and whether results are cached.

    Note: this reflects only queries tracked by THIS server process (the
    in-memory registry). Queries started on another replica, or before a
    restart, are not listed.

    Returns:
        {queries: [...], count: N}.
    """
    registry = get_handle_registry(ctx)
    entries = registry.list_entries()
    logger.info("Listing %d tracked async query(ies)", len(entries))
    return {"queries": entries, "count": len(entries)}


def cancel_async_query(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Cancel the query associated with the query handle.

    Cancels a running async query. After cancelling, the token is evicted.

    Args:
        query_handle: The token returned by ``run_query_async``.

    Returns:
        A dict confirming the cancellation.
    """
    registry = get_handle_registry(ctx)
    entry = registry.get(query_handle)

    entry.handle.cancel()
    registry.remove(query_handle)
    logger.info("Cancelled async query (token=%s)", query_handle)

    return {"query_handle": query_handle, "cancelled": True}
