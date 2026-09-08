"""POC: serving large SQL++ result sets through MCP resources.

The problem
-----------
``run_query_sync`` returns every row it fetched directly in the tool result, so
``SELECT * FROM route`` costs the model all 24k rows. These tools run the same
query, return only a small preview inline, and keep the full result set
server-side (see result_store.py) behind an MCP resource the caller can read
if — and only if — it actually needs more.

How the rows come back
----------------------
Two resource templates are registered in ea_mcp_server.py, both reading the
same buffered result:

``ea://query-results/{result_id}``
    The complete row set as JSON. All-or-nothing, and the point of it: an MCP
    host can attach a resource to the conversation, hand it to a
    code-execution sandbox, or show it to the user *without* the rows passing
    through the model's context at all.

``ea://query-results/{result_id}/rows{?offset,limit}``
    One window of the same result — RFC 6570 query expansion, e.g.
    ``?offset=500&limit=200``. Lets a caller read rows 5-10, then 1-20, in any
    order, without pulling the whole set. Carries ``next_offset`` so pages can
    be walked without a tool call.

Sync and async
--------------
``run_query_poc_resource``
    Thin over ``cluster.execute_query`` — preview plus resource_uri.

``get_async_query_results_poc_resource``
    Truncating counterpart to ``get_async_query_results``. Reuses
    ``run_query_async`` unchanged (it returns no rows, so it has no truncation
    problem); only the fetch is replaced. Costs two live tokens — EA's
    ``query_handle`` and the POC's ``result_id`` — and so two cleanup calls.

``run_query_poc_async`` + ``get_query_poc_async_results``
    One ``result_id`` valid from submission through completion: the store
    entry holds the live EA handle while running, then swaps to buffered rows.
    One token, one cleanup call.

Cleanup
-------
``release_query_poc_result`` frees a buffer and cancels the query if it is
still running. Resources are read-only in MCP, so a URI cannot free anything —
this tool is the only on-demand cleanup. Results also expire by TTL and LRU
cap, but lazily: expiry runs on the next store access, not on a timer.

Known limit
-----------
Every path calls ``get_all_rows()``, so the full result set lands in this
server's memory even though only a preview reaches the model. Truncation
protects the model's context, not the server's RAM. The SDK's ``rows()``
returns a streaming iterator (measured: 45ms/1MB for a 10-row preview against
1633ms/14MB for ``get_all_rows()``), so a future version could stream the
preview and spool the rest; deliberately out of scope for the POC.
"""

import json
import logging
from typing import Any

from fastmcp import Context

from ..connection import (
    get_cluster_connection,
    get_handle_registry,
    get_result_store,
)
from ..responses import tool_error, tool_success
from ..result_store import StoredResult, UnknownResultError

logger = logging.getLogger("ea-mcp-server.tools.query_poc")

# How many rows a POC tool shows inline before pointing at the full result.
# Small on purpose: the preview exists to let the model judge shape and decide
# whether it needs more, not to be a usable sample.
DEFAULT_PREVIEW_ROWS = 5

# Hard ceiling on a single page from the paged resource. Without it a caller
# can undo the whole point of the POC with limit=1000000.
MAX_PAGE_ROWS = 500

# URI space served by the resource template registered in ea_mcp_server.py.
RESULT_RESOURCE_URI = "ea://query-results/{result_id}"

# Paged variant of the same result set. RFC 6570 query expansion: a client
# reads ``ea://query-results/{id}/rows?offset=500&limit=200``. Exists because
# the whole-result URI is all-or-nothing — a host with no sandbox to hand it
# to must either take every row or none, which is the very problem the POC is
# about. Same store, same ids; only the slice differs.
RESULT_PAGE_RESOURCE_URI = "ea://query-results/{result_id}/rows{?offset,limit}"

# Default page size when a client omits ``limit``. Smaller than MAX_PAGE_ROWS
# so an unparameterised read of the paged URI is cheap by default.
DEFAULT_RESOURCE_PAGE_ROWS = 100


def _resource_uri(result_id: str) -> str:
    return RESULT_RESOURCE_URI.format(result_id=result_id)


def _preview(entry: StoredResult, preview_rows: int) -> tuple[list[Any], bool]:
    """Return the inline preview slice and whether anything was withheld."""
    shown = entry.rows[:preview_rows]
    return shown, entry.row_count > len(shown)


def _resource_payload(entry: StoredResult, shown: list[Any], truncated: bool) -> dict:
    """Build the POC-1 (resource handoff) success fields for a stored result.

    Shared by the sync and async resource runners so the two differ only in how
    the rows were obtained, never in how the handoff is described.
    """
    uri = _resource_uri(entry.result_id)
    if truncated:
        message = (
            f"Truncated result: showing {len(shown)} of {entry.row_count} rows. "
            f"The complete result set is available as the resource {uri} — read "
            f"it only if you need rows beyond this sample, as it contains all "
            f"{entry.row_count} rows."
        )
    else:
        message = (
            f"Complete result: all {entry.row_count} row(s) shown. "
            f"Also available as the resource {uri}."
        )
    return {
        "rows": shown,
        "row_count": entry.row_count,
        "preview_row_count": len(shown),
        "truncated": truncated,
        "result_id": entry.result_id,
        "resource_uri": uri,
        "message": message,
    }


def run_query_poc_resource(
    ctx: Context, statement: str, preview_rows: int = DEFAULT_PREVIEW_ROWS
) -> dict[str, Any]:
    """Run a SQL++ query, return a few sample rows, and link the full result as a resource.

    Use instead of run_query_sync when a query may return many rows. The
    complete result set is kept on the server and exposed at the returned
    resource_uri; read that resource only if the sample is not enough, since
    it contains every row.

    Args:
        statement: The SQL++ statement to execute.
        preview_rows: How many rows to include inline (default 5).

    Returns:
        {"success": True, "rows": [...], "row_count": N, "preview_row_count": K,
        "truncated": true/false, "result_id": "...", "resource_uri": "ea://..."};
        or {"success": False, "error": "..."} on failure.
    """
    cluster = get_cluster_connection(ctx)
    store = get_result_store(ctx)
    preview_rows = max(0, preview_rows)
    try:
        logger.debug("POC(resource): running SQL++ statement synchronously")
        result = cluster.execute_query(statement)
        rows = result.get_all_rows()
        entry = store.store(statement, rows)
        shown, truncated = _preview(entry, preview_rows)
        logger.info(
            f"POC(resource): query returned {entry.row_count} row(s), "
            f"showing {len(shown)} (result_id={entry.result_id})"
        )
        return tool_success(**_resource_payload(entry, shown, truncated))
    except Exception as e:
        logger.error(f"POC(resource): error running query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def release_query_poc_result(ctx: Context, result_id: str) -> dict[str, Any]:
    """Free a result set held by the POC query tools, cancelling it if still running.

    Call when done with a result. The rows cannot be read afterwards and the
    result_id stops working; re-run the query to get a new one. This is the
    single cleanup call for every POC tool, including run_query_poc_async.

    Args:
        result_id: The result_id returned by any run_query_poc_* tool.

    Returns:
        {"success": True, "result_id": "...", "released": true/false,
        "cancelled": true/false}.
    """
    store = get_result_store(ctx)
    cancelled = False

    # A run_query_poc_async id may still be running. Dropping the entry would
    # strand the query on the EA server with no handle left to reach it, so
    # cancel first and only then evict — the ordering cancel_async_query uses.
    try:
        entry = store.get(result_id)
        if not entry.is_ready and entry.handle is not None:
            try:
                status = entry.handle.fetch_status()
                if status.results_ready():
                    # Finished between our last check and now: free EA's
                    # buffers rather than leaving them allocated.
                    status.result_handle().discard_results()
                else:
                    entry.handle.cancel()
                    cancelled = True
            except Exception as e:
                # Best-effort: a failure to reach EA must not prevent the local
                # entry from being freed, but it is worth surfacing in the log.
                logger.warning(
                    f"POC(async): could not cancel/discard on EA for "
                    f"result_id={result_id}: {e}"
                )
    except UnknownResultError:
        pass

    released = store.release(result_id)
    logger.info(
        f"POC: release result_id={result_id} "
        f"(released={released}, cancelled={cancelled})"
    )
    if not released:
        message = "No such result_id; it may already have been released or expired."
    elif cancelled:
        message = "Query cancelled and result set freed."
    else:
        message = "Result set freed."
    return tool_success(
        result_id=result_id,
        released=released,
        cancelled=cancelled,
        message=message,
    )


# ---------------------------------------------------------------------------
# Async POC, shape A: truncate at fetch time.
#
# ``run_query_async`` is reused unchanged — it returns no rows, so it has no
# truncation problem. Only the fetch step is replaced. The cost is that two
# token types are live at once (EA's query_handle and the POC's result_id) and
# cleanup is two calls: discard_async_query_results *and*
# release_query_poc_result.
# ---------------------------------------------------------------------------


def _fetch_async_into_store(
    ctx: Context, query_handle: str, preview_rows: int
) -> tuple[StoredResult, list[Any], bool] | dict[str, Any]:
    """Fetch a ready async query's rows into the store.

    Returns ``(entry, shown, truncated)`` once rows are buffered, or a
    ready-false / error envelope to hand straight back to the caller. Shared by
    both shape-A fetch tools so they differ only in how they present the result.
    """
    registry = get_handle_registry(ctx)
    store = get_result_store(ctx)
    handle_entry = registry.get(query_handle)
    status = handle_entry.handle.fetch_status()
    if not status.results_ready():
        return tool_success(
            query_handle=query_handle,
            ready=False,
            message=(
                "Query is still running. Call this tool again later to check "
                "for results."
            ),
        )

    result = status.result_handle().fetch_results()
    rows = result.get_all_rows()
    entry = store.store(handle_entry.statement, rows)
    shown, truncated = _preview(entry, max(0, preview_rows))
    return entry, shown, truncated


def get_async_query_results_poc_resource(
    ctx: Context, query_handle: str, preview_rows: int = DEFAULT_PREVIEW_ROWS
) -> dict[str, Any]:
    """Get a finished async query's results as sample rows plus a full-result resource.

    The truncating counterpart to get_async_query_results, for async queries
    that may return many rows. If the query is still running it returns
    ready: false and no rows; call it again later. Once ready it returns a
    sample and a resource_uri holding every row.

    The EA query is separate from the buffered rows: call
    discard_async_query_results with the query_handle to free it on the server,
    and release_query_poc_result with the result_id to free the buffer.

    Args:
        query_handle: The query_handle returned by run_query_async.
        preview_rows: How many rows to include inline (default 5).

    Returns:
        {"success": True, "ready": true, "rows": [...], "row_count": N,
        "result_id": "...", "resource_uri": "ea://..."}; or {"success": True,
        "ready": false}; or {"success": False, "error": "..."} on failure.
    """
    try:
        outcome = _fetch_async_into_store(ctx, query_handle, preview_rows)
        if isinstance(outcome, dict):
            return outcome
        entry, shown, truncated = outcome
        logger.info(
            f"POC(async/resource): fetched {entry.row_count} row(s), showing "
            f"{len(shown)} (query_handle={query_handle}, "
            f"result_id={entry.result_id})"
        )
        return tool_success(
            query_handle=query_handle,
            ready=True,
            **_resource_payload(entry, shown, truncated),
        )
    except Exception as e:
        logger.error(f"POC(async/resource): error fetching results: {e}", exc_info=True)
        return tool_error(e, query_handle=query_handle)


# ---------------------------------------------------------------------------
# Async POC, shape B: one id spanning both phases.
#
# ``run_query_poc_async`` mints a single result_id that is valid from the
# moment the query starts. The store entry holds the live EA handle while it
# runs, then swaps to buffered rows when it finishes. The model sees one token
# and one cleanup call (release_query_poc_result, which cancels if still
# running); the cost is that the async lifecycle is duplicated here rather than
# reusing the existing handle tools.
# ---------------------------------------------------------------------------


def run_query_poc_async(ctx: Context, statement: str) -> dict[str, Any]:
    """Start a possibly-large SQL++ query without waiting, returning one id for everything.

    Use for queries that may be slow and return many rows. Returns right away
    with a result_id and no rows. Call get_query_poc_async_results with that
    result_id to check whether it has finished and get a sample; then either
    read its resource_uri, or a page of it via the /rows resource. Call
    release_query_poc_result when done — that is the only cleanup needed, and
    it cancels the query if it is still running.

    Args:
        statement: The SQL++ statement to execute.

    Returns:
        {"success": True, "result_id": "...", "ready": false}, or
        {"success": False, "error": "..."} on failure.
    """
    cluster = get_cluster_connection(ctx)
    store = get_result_store(ctx)
    try:
        logger.debug("POC(async/unified): starting query")
        handle = cluster.start_query(statement)
        entry = store.store_pending(statement, handle)
        logger.info(f"POC(async/unified): started (result_id={entry.result_id})")
        return tool_success(
            result_id=entry.result_id,
            ready=False,
            message=(
                "Query submitted. Call get_query_poc_async_results with this "
                "result_id to check whether it has finished and get a sample "
                "of the rows."
            ),
        )
    except Exception as e:
        logger.error(f"POC(async/unified): error starting query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def get_query_poc_async_results(
    ctx: Context, result_id: str, preview_rows: int = DEFAULT_PREVIEW_ROWS
) -> dict[str, Any]:
    """Check a run_query_poc_async query and get a sample of its rows once finished.

    If the query is still running this returns ready: false and no rows; call
    it again later rather than looping tightly. Once ready it returns a sample
    plus both ways to reach the rest: a resource_uri holding every row, and a
    next_offset for reading a page via the /rows resource. Use whichever
    suits; they read the same buffered result.

    Safe to call repeatedly once ready — the rows stay buffered until
    release_query_poc_result.

    Args:
        result_id: The result_id returned by run_query_poc_async.
        preview_rows: How many rows to include inline (default 5).

    Returns:
        {"success": True, "ready": true, "rows": [...], "row_count": N,
        "result_id": "...", "resource_uri": "ea://...", "next_offset": K};
        or {"success": True, "ready": false}; or {"success": False,
        "error": "..."} on failure.
    """
    store = get_result_store(ctx)
    try:
        entry = store.get(result_id)

        if not entry.is_ready:
            if entry.handle is None:
                return tool_error(
                    "Result set has no rows and no live query handle.",
                    result_id=result_id,
                )
            status = entry.handle.fetch_status()
            if not status.results_ready():
                return tool_success(
                    result_id=result_id,
                    ready=False,
                    message=(
                        "Query is still running. Call this tool again later "
                        "to check for results."
                    ),
                )
            # Finished: buffer the rows and release the live handle, so the
            # entry behaves exactly like a sync-POC one from here on.
            result = status.result_handle().fetch_results()
            entry.attach_rows(result.get_all_rows())
            logger.info(
                f"POC(async/unified): buffered {entry.row_count} row(s) "
                f"(result_id={result_id})"
            )

        shown, truncated = _preview(entry, max(0, preview_rows))
        # Deliberately offers BOTH handoffs: with one id spanning both phases,
        # there is no reason to make the caller pick a shape at start time.
        payload = _resource_payload(entry, shown, truncated)
        payload["next_offset"] = len(shown) if truncated else None
        if truncated:
            payload["message"] = (
                f"Truncated result: showing {len(shown)} of {entry.row_count} "
                f"rows. Read the resource {payload['resource_uri']} for the "
                f"complete set, or read "
                f"{payload['resource_uri']}/rows?offset={len(shown)}&limit=100 "
                f"to page through it. Call release_query_poc_result when done."
            )
        return tool_success(ready=True, **payload)
    except Exception as e:
        logger.error(f"POC(async/unified): error fetching results: {e}", exc_info=True)
        return tool_error(e, result_id=result_id)


def read_query_poc_result_resource(result_id: str, store: Any) -> str:
    """Render a stored result set as the JSON body of the MCP resource.

    Kept here (rather than inline in the server module) so the resource body
    and the tools that advertise it stay in one place. Raises
    ``UnknownResultError`` for an unknown id, which FastMCP turns into a
    resource-read error.
    """
    entry = store.get(result_id)
    if not entry.is_ready:
        # The id is valid but the async query has not finished. Return a body
        # saying so rather than an empty row list, which would misread as a
        # query that matched nothing.
        return json.dumps(
            {
                "result_id": entry.result_id,
                "statement": entry.statement,
                "ready": False,
                "message": (
                    "Query is still running; no rows are available yet. Check "
                    "get_query_poc_async_results and read this resource again "
                    "once it reports ready."
                ),
            },
            indent=2,
            default=str,
        )
    return json.dumps(
        {
            "result_id": entry.result_id,
            "statement": entry.statement,
            "ready": True,
            "row_count": entry.row_count,
            "rows": entry.rows,
        },
        indent=2,
        default=str,
    )


def read_query_poc_result_page_resource(
    result_id: str,
    store: Any,
    offset: int = 0,
    limit: int = DEFAULT_RESOURCE_PAGE_ROWS,
) -> str:
    """Render one page of a stored result set as the JSON body of the MCP resource.

    Clamps like the whole-result reader (negative offset pinned to 0, limit
    capped at ``MAX_PAGE_ROWS``) so an out-of-range window degrades to an
    empty page rather than an error. Carries ``next_offset`` so a client can
    walk pages without a tool call.
    """
    entry = store.get(result_id)
    if not entry.is_ready:
        return json.dumps(
            {
                "result_id": entry.result_id,
                "statement": entry.statement,
                "ready": False,
                "message": (
                    "Query is still running; no rows are available yet. Check "
                    "get_query_poc_async_results and read this resource again "
                    "once it reports ready."
                ),
            },
            indent=2,
            default=str,
        )

    offset = max(0, offset)
    limit = max(1, min(limit, MAX_PAGE_ROWS))
    page = entry.page(offset, limit)
    next_offset = offset + len(page)
    has_more = next_offset < entry.row_count

    return json.dumps(
        {
            "result_id": entry.result_id,
            "statement": entry.statement,
            "ready": True,
            "offset": offset,
            "limit": limit,
            "returned_row_count": len(page),
            "row_count": entry.row_count,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "rows": page,
        },
        indent=2,
        default=str,
    )
