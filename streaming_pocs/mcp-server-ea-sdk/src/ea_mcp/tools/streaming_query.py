"""Streaming query tools for Couchbase Enterprise Analytics.

These four tools expose the SDK's row-streaming API (analytics SDK >= 1.1.0)
as a resumable, forward-only cursor:

    stream_query_results -> cluster.execute_query(stmt).rows() + N x next()
    fetch_next_rows      -> N x next() on the stored iterator
    close_query_stream   -> result.cancel()
    list_query_streams   -> registry read

Rather than materializing a whole result set with ``get_all_rows()``, the
cursor pulls rows off an open HTTP response on demand, so a caller can walk a
very large result a batch at a time with bounded memory.

The live ``BlockingQueryResult`` / ``BlockingIterator`` objects cannot be
serialized or sent to the client, so they are kept in a server-side
``CursorRegistry`` and referenced by an opaque ``cursor`` token that is
returned to the client and passed back on every subsequent call. See
cursor_registry.py for the design and its single-process caveat.

Cursors are forward-only and independent: several may be open at once and read
in any interleaved order, but none can be rewound.

Tools are plain ``def`` (not ``async def``): ``get_next_row()`` blocks on a
socket, so it must run on FastMCP's thread pool rather than stalling the event
loop inside a coroutine.
"""

import logging
from typing import Any

from couchbase_analytics.common.errors import AnalyticsError, InternalSDKError
from couchbase_analytics.common.errors import TimeoutError as AnalyticsTimeoutError
from fastmcp import Context

from ..utils.constants import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MCP_SERVER_NAME,
)
from ..utils.context import get_cluster_connection, get_cursor_registry
from ..utils.cursor_registry import (
    TooManyOpenStreamsError,
    UnknownCursorError,
    _Entry,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.streaming_query")


def _resolve_batch_size(batch_size: int | None) -> int:
    """Clamp a requested batch size into the allowed range."""
    if batch_size is None:
        return DEFAULT_BATCH_SIZE
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    return min(batch_size, MAX_BATCH_SIZE)


def _collect_metadata(entry: _Entry) -> dict[str, Any]:
    """Read query metadata, which the SDK only exposes once rows are drained."""
    metadata: dict[str, Any] = {}
    try:
        meta = entry.result.metadata()
        metadata["request_id"] = meta.request_id()
        metrics = meta.metrics()
        metadata["metrics"] = {
            "result_count": metrics.result_count(),
            "result_size": metrics.result_size(),
        }
    except Exception as e:  # noqa: BLE001
        # metadata() raises until iteration completes, and a cancelled stream
        # never completes -- so absent metadata is expected, not an error.
        logger.debug("Metadata unavailable for cursor: %s", e)
    return metadata


def _drain_batch(
    ctx: Context, cursor: str, entry: _Entry, batch_size: int
) -> dict[str, Any]:
    """Pull up to ``batch_size`` rows, then describe where the cursor stands.

    Shared by ``stream_query_results`` and ``fetch_next_rows`` so both report
    progress identically.
    """
    registry = get_cursor_registry(ctx)
    rows: list[Any] = []
    exhausted = False

    try:
        for _ in range(batch_size):
            try:
                rows.append(next(entry.iterator))
            except StopIteration:
                exhausted = True
                break
    except AnalyticsTimeoutError as e:
        # query_timeout is a deadline on the whole request, and it keeps
        # running while the cursor sits idle between calls. Surface it as a
        # clear terminal state rather than an SDK traceback.
        rows_served = entry.rows_served + len(rows)
        registry.remove(cursor)
        logger.info(
            "Cursor %s expired after %d row(s): %s", cursor, rows_served, e
        )
        return {
            "cursor": cursor,
            "error": "stream_expired",
            "rows": rows,
            "rows_so_far": rows_served,
            "done": True,
            "message": (
                f"The stream exceeded the query timeout after {rows_served} "
                "row(s) and has been closed. Streaming cannot resume from a "
                "partial read; re-run stream_query_results (consider a "
                "narrower query or a larger batch_size)."
            ),
        }
    except (AnalyticsError, InternalSDKError) as e:
        rows_served = entry.rows_served + len(rows)
        registry.remove(cursor)
        logger.warning("Cursor %s failed after %d row(s): %s", cursor, rows_served, e)
        return {
            "cursor": cursor,
            "error": "stream_failed",
            "rows": rows,
            "rows_so_far": rows_served,
            "done": True,
            "message": f"The query stream failed after {rows_served} row(s): {e}",
        }

    rows_served = entry.rows_served + len(rows)
    response: dict[str, Any] = {
        "cursor": cursor,
        "rows": rows,
        "rows_in_batch": len(rows),
        "rows_so_far": rows_served,
        "done": exhausted,
    }

    if exhausted:
        # The SDK closed the socket when iteration ended; dropping our
        # reference here releases the iterator and its parse-ahead buffer.
        response["metadata"] = _collect_metadata(entry)
        registry.remove(cursor)
        response["message"] = (
            f"Stream complete: {rows_served} row(s) total. The cursor is now "
            "closed and no further rows can be fetched."
        )
        logger.info("Cursor %s exhausted after %d row(s)", cursor, rows_served)
    else:
        registry.touch(cursor, rows_served=rows_served, done=False)
        response["next"] = "fetch_next_rows"
        response["message"] = (
            f"Returned {len(rows)} row(s); more remain. Call fetch_next_rows "
            "with this cursor to continue, or close_query_stream to stop early."
        )

    return response


def stream_query_results(
    ctx: Context, statement: str, batch_size: int | None = None
) -> dict[str, Any]:
    """Run a SQL++ query and stream the first batch of rows.

    Use this instead of a plain query when the result set is large or its size
    is unknown: rows are pulled from Enterprise Analytics on demand rather than
    all loaded into memory at once. Returns an opaque ``cursor`` token plus the
    first ``batch_size`` rows; call ``fetch_next_rows`` with that cursor to keep
    reading until the response reports ``done: true``.

    The cursor is forward-only -- it cannot be rewound to re-read earlier rows,
    so retain any rows you still need. Several cursors may be open at once and
    read in any order; each keeps its own independent position. Close any cursor
    you stop reading with ``close_query_stream`` to free server resources.

    Args:
        statement: The SQL++ statement to execute.
        batch_size: Rows to return per call (default 10, max 1000). Use 1 for
            strict row-at-a-time reads, or a larger value to reduce round-trips.

    Returns:
        A dict with ``cursor``, ``rows``, ``rows_so_far``, and ``done``.
    """
    batch = _resolve_batch_size(batch_size)
    cluster = get_cluster_connection(ctx)
    registry = get_cursor_registry(ctx)

    logger.debug("Opening query stream (batch_size=%d): %s", batch, statement)
    result = cluster.execute_query(statement)
    iterator = iter(result.rows())

    try:
        cursor = registry.register(result, iterator, statement)
    except TooManyOpenStreamsError as e:
        # Don't leave the just-opened socket dangling when we refuse the cursor.
        try:
            result.cancel()
        except Exception:  # noqa: BLE001
            pass
        return {"error": "too_many_open_streams", "message": str(e)}

    entry = registry.get(cursor)
    response = _drain_batch(ctx, cursor, entry, batch)
    response["statement"] = statement
    response["batch_size"] = batch
    return response


def fetch_next_rows(
    ctx: Context, cursor: str, batch_size: int | None = None
) -> dict[str, Any]:
    """Fetch the next batch of rows from an open query stream.

    Resumes exactly where the previous call left off. Keep calling this with
    the same cursor until the response reports ``done: true``, at which point
    the cursor is closed automatically and query ``metadata`` is included.

    Args:
        cursor: The token returned by ``stream_query_results``.
        batch_size: Rows to return for this call (default 10, max 1000). May
            differ from call to call.

    Returns:
        A dict with ``rows``, ``rows_so_far``, and ``done``.
    """
    batch = _resolve_batch_size(batch_size)
    registry = get_cursor_registry(ctx)

    try:
        entry = registry.get(cursor)
    except UnknownCursorError as e:
        return {"cursor": cursor, "error": "unknown_cursor", "message": str(e)}

    response = _drain_batch(ctx, cursor, entry, batch)
    response["batch_size"] = batch
    return response


def close_query_stream(ctx: Context, cursor: str) -> dict[str, Any]:
    """Close an open query stream without reading the remaining rows.

    Use this as soon as you stop reading a cursor: it cancels the underlying
    request and frees the server-side connection and buffers. Streams left open
    are eventually reaped, but only after an idle timeout.

    Args:
        cursor: The token returned by ``stream_query_results``.

    Returns:
        A dict confirming the close and the number of rows read.
    """
    registry = get_cursor_registry(ctx)

    try:
        entry = registry.close(cursor)
    except UnknownCursorError as e:
        return {"cursor": cursor, "error": "unknown_cursor", "message": str(e)}

    logger.info("Closed cursor %s after %d row(s)", cursor, entry.rows_served)
    return {
        "cursor": cursor,
        "closed": True,
        "rows_served": entry.rows_served,
        "statement": entry.statement,
    }


def list_query_streams(ctx: Context) -> dict[str, Any]:
    """List the query streams currently open on this server.

    Use this to recover a ``cursor`` token you no longer have -- for example if
    it scrolled out of the conversation context -- along with how far each
    cursor has read and how long it has been idle.

    Note: this reflects only cursors held by THIS server process (the in-memory
    registry). Streams opened on another replica, or before a restart, are not
    listed.

    Returns:
        A dict with ``streams`` and ``count``.
    """
    registry = get_cursor_registry(ctx)
    entries = registry.list_entries()
    logger.info("Listing %d open query stream(s)", len(entries))
    return {"streams": entries, "count": len(entries)}
