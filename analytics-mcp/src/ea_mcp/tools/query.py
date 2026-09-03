"""Query execution tools for Enterprise Analytics.

``run_query_sync`` blocks and buffers the whole result set. The Server Async
Request API tools (EA 2.2+, analytics SDK >= 1.1.0) expose EA's handle-based
flow for long-running queries: start -> check/fetch -> discard, or cancel.
``get_async_query_results`` does double duty as the readiness check, so there
is no separate status tool.

Design notes (kept out of the tool docstrings, which are sent to the model as
tool descriptions and are deliberately short):

* The live SDK handle objects hold an HTTP client and a thread pool, so they
  cannot be serialized to the client. They stay in a server-side
  ``HandleRegistry``, referenced by an opaque ``query_handle`` token. See
  handle_registry.py for the design and its single-process caveat.
* Fetching results does NOT free them: EA serves the same buffers on repeated
  fetches (verified against EA 2.2 — the result URL still returns 200 after a
  fetch and only 404s after a discard). So a token stays valid after
  ``get_async_query_results``; only discard and cancel evict it. Callers that
  never discard leave buffers allocated until EA times them out.
* Tools are plain ``def`` (not ``async def``): the SDK's handle calls are
  blocking, so they run on FastMCP's thread pool rather than blocking the event
  loop inside a coroutine.
"""

import logging
from typing import Any

from fastmcp import Context

from ..connection import get_cluster_connection, get_handle_registry
from ..responses import tool_error, tool_success

logger = logging.getLogger("ea-mcp-server.tools.query")


def run_query_sync(ctx: Context, statement: str) -> dict[str, Any]:
    """Run a SQL++ statement and buffer all result rows in memory.

    Can carry SELECT, DML, or DDL statements. Buffers the entire result set
    in client memory before returning.

    Returns {"success": True, "rows": [...], "row_count": N} on success, or
    {"success": False, "error": "..."} on failure.
    """
    cluster = get_cluster_connection(ctx)
    try:
        logger.debug("Running SQL++ statement synchronously")
        result = cluster.execute_query(statement)
        rows = result.get_all_rows()
        logger.info(f"Query returned {len(rows)} row(s)")
        return tool_success(rows=rows, row_count=len(rows))
    except Exception as e:
        logger.error(f"Error running query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def _extract_metadata(result: Any, query_handle: str) -> dict[str, Any]:
    """Convert a query result's metadata into a JSON-serializable dict.

    The SDK exposes metadata as objects behind accessor methods
    (``QueryMetadata`` / ``QueryMetrics``), none of which serialize, so each
    value is read out by hand. Timing metrics come back as ``timedelta``, which
    is not JSON-safe either, and are emitted as float milliseconds.

    Metadata is a nicety, not the payload: a field the server did not send
    should not fail an otherwise successful fetch, so every read is
    independently guarded and whatever was gathered is returned.
    """

    def _read(fn: Any, label: str) -> Any:
        """Call one accessor, returning None (and logging) if it fails."""
        try:
            return fn()
        except Exception as e:
            logger.debug(
                f"Metadata field {label!r} unavailable for {query_handle}: {e}"
            )
            return None

    metadata: dict[str, Any] = {}
    meta = _read(lambda: result.metadata(), "metadata")
    if meta is None:
        return metadata

    metadata["request_id"] = _read(meta.request_id, "request_id")
    metadata["warnings"] = _read(meta.warnings, "warnings") or []

    metrics = _read(meta.metrics, "metrics")
    if metrics is not None:
        elapsed = _read(metrics.elapsed_time, "elapsed_time")
        execution = _read(metrics.execution_time, "execution_time")
        metadata["metrics"] = {
            # timedelta -> float ms, so the values survive JSON encoding.
            "elapsed_time_ms": elapsed.total_seconds() * 1000
            if elapsed is not None
            else None,
            "execution_time_ms": execution.total_seconds() * 1000
            if execution is not None
            else None,
            "result_count": _read(metrics.result_count, "result_count"),
            "result_size": _read(metrics.result_size, "result_size"),
            "processed_objects": _read(metrics.processed_objects, "processed_objects"),
        }
    return metadata


def run_query_async(ctx: Context, statement: str) -> dict[str, Any]:
    """Start a SQL++ query without waiting for it to finish.

    Use for queries expected to take a while. Returns right away with a
    query_handle; it does not return rows. Keep that query_handle: it is
    needed for every follow-up call, and the query holds resources on the
    server until you finish with discard_async_query_results or
    cancel_async_query.

    Usual sequence: get_async_query_results until it reports ready, then
    discard_async_query_results. For quick queries use run_query_sync
    instead, which returns rows directly.

    Args:
        statement: The SQL++ statement to execute.

    Returns:
        {"success": True, "query_handle": "...", "request_id": "..."}, or
        {"success": False, "error": "..."} on failure.
    """
    cluster = get_cluster_connection(ctx)
    registry = get_handle_registry(ctx)
    try:
        logger.debug("Starting async query")
        handle = cluster.start_query(statement)
        token = registry.register(handle, statement)
        request_id = getattr(handle, "_request_id", None)
        logger.info(f"Started async query (token={token}, request_id={request_id})")
        return tool_success(
            query_handle=token,
            request_id=request_id,
            message=(
                "Query submitted. Call get_async_query_results with this "
                "query_handle to check whether it has finished and retrieve "
                "the rows."
            ),
        )
    except Exception as e:
        logger.error(f"Error starting async query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def get_async_query_results(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Check the status of an async query and get its results once it has finished.

    This both reports progress and returns results. If the query is still
    running it returns ready: false and no rows, rather than waiting; call it
    again later to check. If it has finished it returns ready: true with the
    rows.

    Each call is a request to the server, so space out repeat calls instead of
    looping tightly — and prefer telling the user the query is still running
    over waiting indefinitely for it.

    Safe to call more than once after it is ready: it does not consume the
    results. When you no longer need them, call discard_async_query_results
    to free them on the server.

    Args:
        query_handle: The query_handle returned by run_query_async.

    Returns:
        {"success": True, "ready": true, "rows": [...], "row_count": N,
        "metadata": {"request_id": ..., "warnings": [...], "metrics":
        {"elapsed_time_ms", "execution_time_ms", "result_count",
        "result_size", "processed_objects"}}}; or {"success": True,
        "ready": false} if not finished; or {"success": False,
        "error": "..."} on failure.
    """
    registry = get_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)
        result_handle = entry.result_handle
        if result_handle is None:
            # Status hasn't been polled to readiness yet; derive it now.
            status = entry.handle.fetch_status()
            if not status.results_ready():
                return tool_success(
                    query_handle=query_handle,
                    ready=False,
                    message=(
                        "Query is still running. Call this tool again later "
                        "to check for results."
                    ),
                )
            result_handle = status.result_handle()
            # Cache it so a re-fetch or a later discard skips the status call.
            registry.set_result_handle(query_handle, result_handle)

        result = result_handle.fetch_results()
        rows = result.get_all_rows()
        metadata = _extract_metadata(result, query_handle)

        # Deliberately NOT evicted: EA keeps the result buffers after a fetch,
        # so the token must stay valid for a re-fetch or an explicit discard.
        logger.info(
            f"Fetched {len(rows)} row(s) for async query (token={query_handle})"
        )
        return tool_success(
            query_handle=query_handle,
            ready=True,
            rows=rows,
            row_count=len(rows),
            metadata=metadata,
        )
    except Exception as e:
        logger.error(f"Error fetching async query results: {e}", exc_info=True)
        return tool_error(e, query_handle=query_handle)


def discard_async_query_results(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Free the results of a finished async query on the server.

    Call this when done with a query's results — whether or not you fetched
    them, since fetching does not free them. This is the normal cleanup step
    after get_async_query_results, and the rows cannot be retrieved
    afterwards.

    If the query is still running there is nothing to discard: this returns
    discarded: false and the query_handle stays usable, so use
    cancel_async_query to stop it instead.

    Args:
        query_handle: The query_handle returned by run_query_async.

    Returns:
        {"success": True, "query_handle": "...", "discarded": true}; or
        {"success": True, "discarded": false, "ready": false} if the query has
        not finished; or {"success": False, "error": "..."} on failure.
    """
    registry = get_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)
        result_handle = entry.result_handle
        if result_handle is None:
            status = entry.handle.fetch_status()
            if not status.results_ready():
                return tool_success(
                    query_handle=query_handle,
                    discarded=False,
                    ready=False,
                    message=(
                        "Results are not ready yet; nothing to discard. Cancel "
                        "the query with cancel_async_query to stop it."
                    ),
                )
            result_handle = status.result_handle()

        result_handle.discard_results()
        registry.remove(query_handle)
        logger.info(f"Discarded results for async query (token={query_handle})")
        return tool_success(query_handle=query_handle, discarded=True)
    except Exception as e:
        logger.error(f"Error discarding async query results: {e}", exc_info=True)
        return tool_error(e, query_handle=query_handle)


def cancel_async_query(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Stop an async query that is still running.

    Use this to abandon a query you no longer want to wait for. On success the
    query stops and the query_handle is no longer usable.

    A query that has already finished cannot be cancelled: this returns
    cancelled: false and the query_handle stays usable, so call
    discard_async_query_results to free its results.

    Args:
        query_handle: The query_handle returned by run_query_async.

    Returns:
        {"success": True, "query_handle": "...", "cancelled": true}, or
        {"success": False, "error": "..."} on failure.
    """
    registry = get_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)

        # Check first: EA answers a cancel for an already-completed query with
        # a bare 404 (no message), and the SDK treats 404 as success — so a
        # blind cancel would report success, evict the token, and strand the
        # result buffers on the server with no handle left to discard them.
        status = entry.handle.fetch_status()
        if status.results_ready():
            # Cache the result handle and deliberately KEEP the entry, so the
            # discard this message recommends is still possible.
            registry.set_result_handle(query_handle, status.result_handle())
            logger.info(
                f"Cancel skipped, query already complete (token={query_handle})"
            )
            return tool_success(
                query_handle=query_handle,
                cancelled=False,
                message=(
                    "Query has already completed, so it cannot be cancelled. "
                    "Call discard_async_query_results to free its results."
                ),
            )

        entry.handle.cancel()
        registry.remove(query_handle)
        logger.info(f"Cancelled async query (token={query_handle})")
        return tool_success(query_handle=query_handle, cancelled=True)
    except Exception as e:
        logger.error(f"Error cancelling async query: {e}", exc_info=True)
        return tool_error(e, query_handle=query_handle)
