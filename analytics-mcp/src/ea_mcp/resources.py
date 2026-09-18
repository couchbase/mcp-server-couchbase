"""MCP resource exposing saved query results.

One RFC 6570 template covers every read:

    ea://results/{result_id}               -> the whole result
    ea://results/{result_id}?offset=100    -> from row 100 to the end
    ea://results/{result_id}?offset=0&limit=50  -> a 50-row page

FastMCP expands ``{?offset,limit}`` into optional query parameters, so a bare
URI and a paged URI hit the same function; the defaults apply when a parameter
is absent. Verified against FastMCP 3.4.7.

Rows come back as JSON Lines (one JSON object per line), matching how sync
results are stored: a client can stream it, and a partial read still parses up
to the last complete line.

Two backing kinds, one URI shape
--------------------------------
*Disk-backed* (sync queries): sliced straight off the saved ``.jsonl`` file.
*Handle-backed* (async queries): nothing was saved locally, so the rows are
re-fetched from EA through the live SDK handle. ``result_id`` is the
``query_handle`` itself, so the model tracks a single id -- but the resource
therefore lives only as long as the handle does. Once the client calls
``discard_async_query_results`` or ``cancel_async_query``, EA frees the buffers
and this resource stops working.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .handle_registry import HandleRegistry, UnknownHandleError
from .result_store import DiskEntry, HandleEntry, ResultStore, UnknownResultError

logger = logging.getLogger("ea-mcp-server.resources")

RESULT_URI_TEMPLATE = "ea://results/{result_id}{?offset,limit}"
RESULT_MIME_TYPE = "application/x-ndjson"


def _to_jsonl(rows: list[dict[str, Any]]) -> str:
    """Encode rows as JSON Lines."""
    return "".join(json.dumps(row, default=str) + "\n" for row in rows)


def _slice(rows: list[dict[str, Any]], offset: int, limit: int | None) -> list[Any]:
    """Apply offset/limit to an in-memory row list."""
    end = None if limit is None else offset + limit
    return rows[offset:end]


def read_saved_result(
    store: ResultStore,
    registry: HandleRegistry,
    result_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Return a saved result as JSON Lines, optionally a slice of it.

    Raises ``UnknownResultError`` when the id is unknown, was evicted, or --
    for async results -- was discarded or cancelled on the EA server.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0 when provided")

    entry = store.get(result_id)

    if isinstance(entry, DiskEntry):
        rows = store.read_rows(result_id, offset=offset, limit=limit)
        logger.debug(
            f"Served {len(rows)} row(s) from saved result {result_id} "
            f"(offset={offset}, limit={limit})"
        )
        return _to_jsonl(rows)

    # Handle-backed: the rows live on the EA server, fetched fresh each read.
    assert isinstance(entry, HandleEntry)
    try:
        handle_entry = registry.get(result_id)
    except UnknownHandleError as e:
        # The handle is gone, so the entry is dead: drop it rather than leave
        # a URI that looks live and always fails.
        store.remove(result_id)
        raise UnknownResultError(
            f"Result '{result_id}' is no longer available. Its async query was "
            "discarded or cancelled, so the Enterprise Analytics server has "
            "freed the rows."
        ) from e

    status = handle_entry.handle.fetch_status()
    if not status.results_ready():
        raise UnknownResultError(
            f"Result '{result_id}' is not ready yet: the async query is still "
            "running. Call get_async_query_results to check on it."
        )

    result = status.result_handle().fetch_results()
    rows = _slice(result.get_all_rows(), offset, limit)
    logger.debug(
        f"Served {len(rows)} row(s) re-fetched from EA for {result_id} "
        f"(offset={offset}, limit={limit})"
    )
    return _to_jsonl(rows)
