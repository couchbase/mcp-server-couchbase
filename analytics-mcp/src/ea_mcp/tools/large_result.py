"""Shared truncation for query tools that can return large result sets.

The wrapper
-----------
``deliver_rows`` is the single place that decides how much of a result set
goes to the model. Both ``run_query_sync`` and ``get_async_query_results``
call it with their rows; it either returns everything (small result) or a
prefix plus a ``result_id`` the caller can page through (large result). Having
one function means the two paths cannot drift apart in how they truncate or
what they promise.

``get_large_result`` and ``release_large_result`` are the paging and cleanup
tools, shared by both.

Id sharing with the async handle
--------------------------------
An async query already has an EA ``query_handle``. Rather than mint a second
token, the async path stores its rows under that same string, so the caller
holds one id for both the query and its buffered rows. ``StoredResult.is_async``
records this, because releasing such an entry must also discard the EA-side
query — and discarding the query must drop the buffer. See
``release_large_result`` and the discard/cancel tools in query.py.
"""

import logging
from typing import Any

from fastmcp import Context

from ..connection import get_handle_registry, get_result_store
from ..responses import tool_error, tool_success
from ..result_store import (
    DEFAULT_MAX_RESPONSE_BYTES,
    UnknownResultError,
    fit_rows,
    measure,
)

logger = logging.getLogger("ea-mcp-server.tools.large_result")


def deliver_rows(
    ctx: Context,
    rows: list[Any],
    statement: str,
    result_id: str | None = None,
    is_async: bool = False,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    **extra: Any,
) -> dict[str, Any]:
    """Return rows to the model, truncating and buffering if they are too large.

    Small results are returned whole and nothing is stored — no id to clean up
    for the common case. Large results are truncated to the byte budget and
    the full set is buffered under ``result_id``.

    Args:
        rows: The complete result set.
        statement: The SQL++ that produced it (kept for diagnostics).
        result_id: Token to store under. The async path passes its EA
            query_handle so one id covers both; sync passes None for a fresh
            uuid.
        is_async: Whether result_id is an EA query_handle (affects release).
        max_bytes: Serialized byte budget for the inline rows.
        **extra: Extra fields to merge into the response envelope.

    Returns:
        {"success": True, "rows": [...], "row_count": N, "truncated": false}
        when everything fits; otherwise adds "returned_row_count",
        "result_id", "next_offset" and a message pointing at get_large_result.
    """
    total = len(rows)
    if measure(rows) <= max_bytes:
        return tool_success(rows=rows, row_count=total, truncated=False, **extra)

    shown, _ = fit_rows(rows, 0, max_bytes)
    store = get_result_store(ctx)
    entry = store.store(statement, rows, result_id=result_id, is_async=is_async)
    logger.info(
        f"Truncated {total} row(s) to {len(shown)} "
        f"(result_id={entry.result_id}, async={is_async})"
    )
    return tool_success(
        rows=shown,
        row_count=total,
        returned_row_count=len(shown),
        truncated=True,
        result_id=entry.result_id,
        next_offset=len(shown),
        message=(
            f"Truncated: showing rows 0-{len(shown) - 1} of {total}. Call "
            f"get_large_result with result_id='{entry.result_id}' and "
            f"offset={len(shown)} to read more. Call release_large_result "
            f"when you no longer need the rows."
        ),
        **extra,
    )


def get_large_result(
    ctx: Context, result_id: str, offset: int = 0, num_rows: int = 100
) -> dict[str, Any]:
    """Read a window of rows from a truncated query result.

    Rows are numbered from 0. Returns up to num_rows starting at offset, but
    stops early if the rows would exceed the response size budget — so a
    window of wide documents comes back shorter than asked, with truncated:
    true and a next_offset to continue from.

    Works for both sync and async queries: pass the result_id from the
    truncated response (for async queries that is the same string as the
    query_handle).

    Args:
        result_id: The result_id from a truncated query response.
        offset: Zero-based index of the first row to return (default 0).
        num_rows: How many rows to return, subject to the size budget
            (default 100).

    Returns:
        {"success": True, "rows": [...], "offset": N, "returned_row_count": K,
        "row_count": TOTAL, "truncated": bool, "has_more": bool,
        "next_offset": N+K}; or {"success": False, "error": "..."} on failure.
    """
    store = get_result_store(ctx)
    try:
        entry = store.get(result_id)
        # Clamp rather than reject: a negative offset would wrap to the tail of
        # the list in Python, which is a confusing thing to hand a model.
        offset = max(0, min(offset, entry.row_count))
        num_rows = max(1, num_rows)

        window = entry.rows[offset : offset + num_rows]
        shown, _ = fit_rows(window, 0, DEFAULT_MAX_RESPONSE_BYTES)
        size_capped = len(shown) < len(window)

        next_offset = offset + len(shown)
        has_more = next_offset < entry.row_count

        if size_capped:
            message = (
                f"Rows {offset}-{next_offset - 1} of {entry.row_count}. "
                f"Truncated: the {num_rows} rows requested exceed the response "
                f"size budget, so only {len(shown)} are returned. Call again "
                f"with offset={next_offset} for more."
            )
        elif has_more:
            message = (
                f"Rows {offset}-{next_offset - 1} of {entry.row_count}. "
                f"Call again with offset={next_offset} for more."
            )
        elif shown:
            message = (
                f"Rows {offset}-{next_offset - 1} of {entry.row_count}. "
                f"End of result set."
            )
        else:
            message = (
                f"No rows at offset {offset}; the result set has "
                f"{entry.row_count} row(s)."
            )

        logger.info(
            f"Served rows {offset}-{next_offset} of {entry.row_count} "
            f"(result_id={result_id})"
        )
        return tool_success(
            result_id=result_id,
            rows=shown,
            offset=offset,
            returned_row_count=len(shown),
            row_count=entry.row_count,
            truncated=size_capped,
            has_more=has_more,
            next_offset=next_offset if has_more else None,
            message=message,
        )
    except Exception as e:
        logger.error(f"Error reading large result: {e}", exc_info=True)
        return tool_error(e, result_id=result_id)


def release_large_result(ctx: Context, result_id: str) -> dict[str, Any]:
    """Free a buffered query result.

    Call when done paging through a truncated result. The rows cannot be read
    afterwards and the result_id stops working.

    If the result came from an async query, this also discards the query on
    the Enterprise Analytics server — one call cleans up both sides, so
    discard_async_query_results is not needed as well.

    Args:
        result_id: The result_id from a truncated query response.

    Returns:
        {"success": True, "result_id": "...", "released": true/false,
        "async_query_discarded": true/false}.
    """
    store = get_result_store(ctx)
    entry = store.peek(result_id)
    discarded = False

    # An async entry shares its id with the EA query_handle, so freeing the
    # local buffer without discarding the EA-side query would strand its
    # result buffers on the server with the token gone. Best-effort: a failure
    # to reach EA must not prevent the local entry from being freed.
    if entry is not None and entry.is_async:
        discarded = _discard_async_side(ctx, result_id)

    released = store.release(result_id)
    logger.info(
        f"Released result_id={result_id} "
        f"(released={released}, async_discarded={discarded})"
    )
    if not released:
        message = "No such result_id; it may already have been released or expired."
    elif discarded:
        message = "Result freed and the async query discarded on the server."
    else:
        message = "Result freed."
    return tool_success(
        result_id=result_id,
        released=released,
        async_query_discarded=discarded,
        message=message,
    )


def _discard_async_side(ctx: Context, query_handle: str) -> bool:
    """Discard an async query's EA-side results and evict its handle.

    Returns whether the discard actually happened. Failures are logged rather
    than raised: the caller is freeing memory and should not be blocked by an
    unreachable server.
    """
    registry = get_handle_registry(ctx)
    try:
        handle_entry = registry.get(query_handle)
    except Exception:
        return False  # already discarded/cancelled, or never async
    try:
        status = handle_entry.handle.fetch_status()
        if status.results_ready():
            status.result_handle().discard_results()
        else:
            handle_entry.handle.cancel()
        registry.remove(query_handle)
        return True
    except Exception as e:
        logger.warning(f"Could not discard async query {query_handle} on EA: {e}")
        return False


def release_buffered_rows(ctx: Context, result_id: str) -> bool:
    """Drop any buffered rows for an id, without touching the EA side.

    The mirror of ``_discard_async_side``: called by
    discard_async_query_results and cancel_async_query so that freeing the
    query also frees the rows buffered under the same id. Kept separate from
    ``release_large_result`` to avoid the two recursing into each other.

    Never raises: this runs *after* the EA-side query has been freed, so a
    problem reaching the buffer must not turn a successful discard into a
    failed one. That includes a context with no result store at all, which is
    what a caller constructing a minimal AppContext will have.
    """
    try:
        return get_result_store(ctx).release(result_id)
    except (UnknownResultError, AttributeError):
        return False
