"""Stateless Server Async Request API tools for Enterprise Analytics.

These five tools mirror the SDK-based server but hold NO server-side state.
Query identity lives entirely in strings that the client passes back:

    run_query_async            -> POST /api/v1/request  (mode=async)
                                  returns {request_id, status_handle}
    get_async_query_status     -> GET  <status_handle>
                                  returns {ready, result_handle?}
    get_async_query_results    -> GET  <result_handle>
                                  returns {rows, metrics}
    discard_async_query_results-> DELETE <result_handle>
    cancel_async_query         -> DELETE /api/v1/active_requests?request_id=...

Because nothing is stored server-side, any replica can service any call and the
server survives restarts: the query lives on EA, keyed by request id.

The tokens EA returns are bearer capabilities — anyone holding the string can
act on that query. If clients are mutually untrusted, add identity scoping on
top of this layer.

Tools are plain ``def``; blocking httpx calls run on FastMCP's thread pool.
"""

import logging
from typing import Any

from fastmcp import Context

from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_ea_client

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.async_query")


def run_query_async(ctx: Context, statement: str) -> dict[str, Any]:
    """Submit a long-running SQL++ query to Enterprise Analytics asynchronously.

    Starts the query via EA's async REST API and returns immediately with the
    string identifiers needed to track it — it does NOT wait for results.

    Pass ``status_handle`` to ``get_async_query_status`` to poll readiness, and
    ``request_id`` to ``cancel_async_query`` to cancel a still-running query.

    Args:
        statement: The SQL++ statement to execute.

    Returns:
        {request_id, status_handle, status, message}. These are plain strings —
        the server keeps no state, so keep them to make follow-up calls.
    """
    client = get_ea_client(ctx)
    started = client.start_query(statement)
    logger.info("Started async query request_id=%s", started["request_id"])
    return {
        "request_id": started["request_id"],
        "status_handle": started["status_handle"],
        "status": started["status"],
        "message": (
            "Query submitted. Poll get_async_query_status with the "
            "status_handle; cancel with cancel_async_query using request_id."
        ),
    }


def get_async_query_status(ctx: Context, status_handle: str) -> dict[str, Any]:
    """Check whether an async query's results are ready.

    Args:
        status_handle: The ``status_handle`` string from ``run_query_async``.

    Returns:
        {ready, status, result_handle?, ...}. When ``ready`` is true, pass
        ``result_handle`` to ``get_async_query_results`` or
        ``discard_async_query_results``.
    """
    client = get_ea_client(ctx)
    st = client.fetch_status(status_handle)
    logger.info("Status status=%s ready=%s", st.get("status"), st.get("ready"))
    result: dict[str, Any] = {
        "ready": st["ready"],
        "status": st["status"],
    }
    if st["ready"]:
        result["result_handle"] = st["result_handle"]
    if st["failed"]:
        result["failed"] = True
        result["errors"] = st.get("errors")
    return result


def get_async_query_results(ctx: Context, result_handle: str) -> dict[str, Any]:
    """Retrieve rows and metadata for a ready async query.

    Call ``get_async_query_status`` first; use the ``result_handle`` it returns
    once ``ready`` is true.

    Args:
        result_handle: The ``result_handle`` string from the status call.

    Returns:
        {rows, metrics}.
    """
    client = get_ea_client(ctx)
    res = client.fetch_results(result_handle)
    logger.info("Fetched %d row(s)", len(res.get("rows", [])))
    return {"rows": res["rows"], "metrics": res["metrics"]}


def discard_async_query_results(
    ctx: Context, result_handle: str
) -> dict[str, Any]:
    """Release server-side result buffers for a completed async query.

    Use when you no longer need the rows — frees EA's buffers without fetching.

    Args:
        result_handle: The ``result_handle`` string from the status call.

    Returns:
        {discarded: true}.
    """
    client = get_ea_client(ctx)
    client.discard_results(result_handle)
    logger.info("Discarded results for handle=%s", result_handle)
    return {"discarded": True}


def list_async_queries(ctx: Context) -> dict[str, Any]:
    """List async queries currently running on the Enterprise Analytics server.

    Use this to recover a query you no longer have identifiers for — for example
    if a request_id scrolled out of the conversation context. Because this
    server is stateless, the list comes straight from EA (its active requests),
    so it reflects queries started by ANY replica, and survives restarts of this
    server.

    Each entry includes ``request_id`` (usable with cancel_async_query),
    ``statement``, ``state``, and ``elapsed_time``.

    Note: EA reports only *running* requests here. A query that has already
    finished is not "active" — it is instead waiting to be fetched via its
    result handle, which EA does not re-hand-out through this list. So this
    recovers running/cancellable queries, not the result handles of completed
    ones. Recovering a completed query's result_handle would require having kept
    the status_handle it was reported under.

    Returns:
        {queries: [...], count: N}.
    """
    client = get_ea_client(ctx)
    raw = client.list_active_requests()
    queries = [
        {
            "request_id": r.get("uuid"),
            "statement": r.get("statement"),
            "state": r.get("state"),
            "elapsed_time": r.get("elapsedTime"),
            "cancellable": r.get("cancellable"),
        }
        for r in raw
    ]
    logger.info("Listing %d active EA request(s)", len(queries))
    return {"queries": queries, "count": len(queries)}


def cancel_async_query(ctx: Context, request_id: str) -> dict[str, Any]:
    """Cancel the query associated with the request id.

    Args:
        request_id: The ``request_id`` string from ``run_query_async``.

    Returns:
        {cancelled: true}.
    """
    client = get_ea_client(ctx)
    client.cancel_query(request_id)
    logger.info("Cancelled request_id=%s", request_id)
    return {"cancelled": True}
