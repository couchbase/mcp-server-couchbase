"""Shared large-result handling: truncate, and optionally save for retrieval.

This is the one place that decides what a query tool hands back. Every query
tool routes its rows through :func:`build_result_payload`, so sync, async and
(later) the parent server's ``run_sql_plus_plus_query`` all behave the same.

Deliberately free of EA, FastMCP and cluster imports: it takes plain rows, a
``ResultConfig`` and a ``ResultStore``, and returns a plain dict. That keeps it
unit-testable with no server and portable into ``cb_mcp`` unchanged.

The decision table
------------------
========================  ==========  ===========  ==========================
server save_large_results tool opt-in size         behavior
========================  ==========  ===========  ==========================
off                       any         <= limit     full result
off                       any         >  limit     truncate only
on                        False       >  limit     truncate only
on                        True        <= limit     full result, nothing saved
on                        True        >  limit     save full, return truncated
                                                   + result_id + resource_uri
========================  ==========  ===========  ==========================

Truncation is row-wise, never byte-wise: rows accumulate until the byte budget
is reached and the last whole row that fits is the cutoff. Cutting mid-row
would hand the model a half-parsed JSON object, which is worse than fewer rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .result_config import ResultConfig
from .result_store import ResultStore

logger = logging.getLogger("ea-mcp-server.result_handling")

#: URI scheme for saved results. Kept here so the tools and the resource
#: template cannot disagree about the format.
RESULT_URI_PREFIX = "ea://results/"


def result_uri(result_id: str) -> str:
    """Build the MCP resource URI a client reads a saved result back from."""
    return f"{RESULT_URI_PREFIX}{result_id}"


def measure_rows(rows: list[dict[str, Any]]) -> int:
    """Return the JSON-encoded size of ``rows`` in bytes."""
    return len(json.dumps(rows, default=str).encode("utf-8"))


def truncate_rows(
    rows: list[dict[str, Any]], max_bytes: int
) -> tuple[list[dict[str, Any]], int]:
    """Return the longest whole-row prefix of ``rows`` fitting in ``max_bytes``.

    Returns ``(kept_rows, kept_bytes)``. Always keeps at least one row, even if
    that single row exceeds the budget: returning zero rows tells the caller
    nothing about the shape of the data, which defeats the point of a preview.
    """
    kept: list[dict[str, Any]] = []
    # Account for the enclosing "[]" and the ", " between rows so the measure
    # matches what measure_rows() would report for the kept slice.
    total = 2
    for row in rows:
        encoded = len(json.dumps(row, default=str).encode("utf-8"))
        separator = 2 if kept else 0
        if kept and total + separator + encoded > max_bytes:
            break
        kept.append(row)
        total += separator + encoded
    return kept, total


def build_result_payload(
    rows: list[dict[str, Any]],
    *,
    config: ResultConfig,
    store: ResultStore | None,
    save_if_large: bool,
    statement: str,
    metadata: dict[str, Any] | None = None,
    result_id: str | None = None,
) -> dict[str, Any]:
    """Apply the decision table above and return fields for ``tool_success``.

    Args:
        rows: The full result set, already materialized.
        config: The server's resolved large-result settings.
        store: Where to save an oversized result. None disables saving.
        save_if_large: The caller's per-call opt-in. Saving happens only when
            this AND ``config.save_large_results`` are both true.
        statement: The originating SQL++, recorded alongside a saved result.
        metadata: Optional query metadata to save with the result.
        result_id: Pre-chosen id to save under. Async queries pass their
            ``query_handle`` here so the model tracks a single id; sync passes
            None and the store mints one.

    Returns:
        A dict to splat into ``tool_success(...)``: always ``rows``,
        ``row_count`` and ``truncated``; plus ``total_row_count`` and
        ``result_size_bytes`` when truncated; plus ``result_id``,
        ``resource_uri`` and a ``message`` when saved.
    """
    total_rows = len(rows)
    size_bytes = measure_rows(rows)

    # Small enough: hand it back whole, regardless of any opt-in. Saving a
    # result the client already has in full would waste disk for nothing.
    if size_bytes <= config.truncate_bytes:
        return {"rows": rows, "row_count": total_rows, "truncated": False}

    kept, _ = truncate_rows(rows, config.truncate_bytes)
    payload: dict[str, Any] = {
        "rows": kept,
        "row_count": len(kept),
        "truncated": True,
        "total_row_count": total_rows,
        "result_size_bytes": size_bytes,
    }

    saving_enabled = config.save_large_results and save_if_large and store is not None
    if not saving_enabled:
        reason = (
            "Set save_result_if_large=true to save the full result and read "
            "it back as a resource."
            if config.save_large_results
            else "Saving large results is disabled on this server."
        )
        payload["message"] = (
            f"Showing the first {len(kept)} of {total_rows} row(s); the full "
            f"result is {size_bytes} bytes. {reason}"
        )
        return payload

    assert store is not None  # narrowed by saving_enabled
    try:
        if result_id is not None:
            # Async: the rows stay on the EA server; index the handle instead.
            store.save_handle(result_id, statement)
            saved_id = result_id
        else:
            entry = store.save_rows(rows, statement, metadata)
            if entry is None:
                payload["message"] = (
                    f"Showing the first {len(kept)} of {total_rows} row(s). "
                    f"The full result ({size_bytes} bytes) is larger than the "
                    "server's entire result-storage budget, so it was not "
                    "saved. Narrow the query with LIMIT or a WHERE clause."
                )
                return payload
            saved_id = entry.result_id
    except Exception as e:
        # A storage failure must not fail an otherwise successful query: the
        # caller still gets their truncated rows.
        logger.error(f"Failed to save large result: {e}", exc_info=True)
        payload["message"] = (
            f"Showing the first {len(kept)} of {total_rows} row(s). The full "
            f"result could not be saved: {e}"
        )
        return payload

    uri = result_uri(saved_id)
    payload["result_id"] = saved_id
    payload["resource_uri"] = uri
    payload["message"] = (
        f"Showing the first {len(kept)} of {total_rows} row(s). The full "
        f"result is available as the resource {uri} — read it whole, or a "
        f"page at a time with {uri}?offset=0&limit=100."
    )
    return payload
