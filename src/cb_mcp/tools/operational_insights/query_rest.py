"""REST-backed variants of the async query tools (``*_rest``).

Same four operations, arguments and response envelopes as the SDK-backed
tools in ``query.py``, implemented over the Server Async Request REST API by
``utils/operational_insights/rest_client.py`` instead of the SDK's handle
objects. Both sets are registered so they can be compared side by side.

What differs, deliberately:

* **No server-side state.** The ``query_handle`` is an encoded token carrying
  the server's request id and status path (plus a ``COPY ... TO``
  destination), not a key into ``QueryResultsRegistry``. It survives restarts
  and works on any worker. Tokens are not interchangeable between the two
  tool sets.
* **Read-only is sent as ``readonly``.** See
  ``OperationalInsightsRestClient.start_query``: the SDK tools' flag does not
  reach the server on the async path.
* **Errors read as ``code: message``** rather than an SDK exception repr.
* **The result timeout bounds each wait, not the whole download** — see the
  rest_client module docstring.

Everything else — the read-only rules, the ``COPY ... TO`` handling, the size
budget and its truncation fields, the messages — is shared with or copied
from ``query.py``, so a comparison isolates the transport.

Tools are plain ``def``: httpx's blocking client runs on FastMCP's thread pool.
"""

import logging
from typing import Any

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from ...utils.constants import SCOPE_WRITE
from ...utils.operational_insights.context import get_operational_insights_rest_client
from ...utils.operational_insights.rest_client import (
    QueryToken,
    UnknownQueryHandleError,
)
from ...utils.query_limits import max_query_result_size_for
from ...utils.responses import tool_error, tool_success
from .query import CopyToError, _is_copy_to_statement, _resolve_copy_to

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.tools.query_rest")


def _unknown_handle_message(query_handle: str) -> str:
    return (
        f"Unknown query_handle '{query_handle}'. It may be invalid, already "
        "discarded/cancelled, or expired on the server. Handles from the "
        "SDK-backed async tools (without the _rest suffix) are not accepted here."
    )


def _error(e: Exception, **fields: Any) -> dict[str, Any]:
    if isinstance(e, UnknownQueryHandleError) and "query_handle" in fields:
        return tool_error(_unknown_handle_message(fields["query_handle"]), **fields)
    return tool_error(e, **fields)


def operational_insights_run_query_async_rest(
    ctx: Context,
    statement: str,
    copy_to_link: str | None = None,
    copy_to_bucket: str | None = None,
    copy_to_path: str | None = None,
    copy_to_format: str | None = None,
) -> dict[str, Any]:
    """Start a SQL++ query without waiting for it to finish (REST-backed).

    Same behaviour as operational_insights_run_query_async, implemented over
    the REST API with no server-side state. Use the _rest tools together:
    a query_handle from this tool only works with
    operational_insights_get_async_query_results_rest,
    operational_insights_discard_async_query_results_rest and
    operational_insights_cancel_async_query_rest.

    Use for queries expected to take a while. Returns right away with a
    query_handle; it does not return rows. Keep that query_handle: it is
    needed for every follow-up call, and the query holds resources on the
    server until you finish with
    operational_insights_discard_async_query_results_rest or
    operational_insights_cancel_async_query_rest.

    Pass copy_to_link, copy_to_bucket and copy_to_path to export the results
    to object storage instead of fetching them back.
    operational_insights_get_async_query_results_rest then reports that the
    export finished and names the destination, rather than carrying rows.

    Args:
        statement: The SQL++ statement to execute.
        copy_to_link: Name of an existing external link (e.g. an S3 link
            created with CREATE LINK). Required to export.
        copy_to_bucket: Destination bucket in the external store. Required
            to export.
        copy_to_path: Path prefix within that bucket, e.g. "exports/run1".
            Required to export.
        copy_to_format: Output format — json (default) or parquet.

    Returns:
        {"success": True, "query_handle": "..."}, or
        {"success": False, "error": "..."} on failure. When exporting, the
        response also carries "exported": True and "destination": {...}, and
        the eventual results will be empty — the rows go to object storage.
    """
    app_context = ctx.request_context.lifespan_context
    token = get_access_token()
    lacks_write_scope = token is not None and SCOPE_WRITE not in (token.scopes or [])
    enforce_readonly = app_context.read_only_mode or lacks_write_scope

    try:
        statement_to_run, destination = _resolve_copy_to(
            statement,
            link=copy_to_link,
            bucket=copy_to_bucket,
            path=copy_to_path,
            output_format=copy_to_format,
        )
    except CopyToError as e:
        logger.debug(f"Rejecting export request: {e}")
        return tool_error(e, statement=statement)

    if enforce_readonly and _is_copy_to_statement(statement_to_run):
        logger.debug("Blocking COPY ... TO statement under read-only mode")
        return tool_error(
            "COPY ... TO is blocked under read-only mode: the server itself "
            "classifies it as read-only, but it writes its result to "
            "external storage or a KV collection.",
            statement=statement,
        )

    try:
        logger.debug(
            f"Starting async query over REST (readonly={enforce_readonly}, "
            f"export={destination is not None})"
        )
        client = get_operational_insights_rest_client(ctx)
        request_id, status_path = client.start_query(
            statement_to_run, readonly=enforce_readonly
        )
        query_handle = QueryToken(request_id, status_path, destination).encode()
        logger.info(
            f"Started async query over REST (request_id={request_id}, "
            f"export={destination is not None})"
        )
        if destination is not None:
            return tool_success(
                query_handle=query_handle,
                exported=True,
                destination=destination,
                message=(
                    f"Export submitted. Call operational_insights_get_async_query_results_rest "
                    f"with this query_handle to check whether it has finished; it will "
                    f"return no rows, as the results are written to "
                    f"{destination['bucket']}/{destination['path']}."
                ),
            )
        return tool_success(
            query_handle=query_handle,
            message=(
                "Query submitted. Call operational_insights_get_async_query_results_rest "
                "with this query_handle to check whether it has finished and "
                "retrieve the rows."
            ),
        )
    except Exception as e:
        logger.error(f"Error starting async query over REST: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def operational_insights_get_async_query_results_rest(
    ctx: Context, query_handle: str
) -> dict[str, Any]:
    """Check the status of an async query and get its results once it has finished (REST-backed).

    Same behaviour as operational_insights_get_async_query_results, for a
    query_handle from operational_insights_run_query_async_rest.

    This both reports progress and returns results. If the query is still
    running it returns ready: false and no rows, rather than waiting; call it
    again later to check. If it has finished it returns ready: true with the
    rows. If the operator has set a result-size limit
    (CB_MCP_OPERATIONAL_INSIGHTS_QUERY_RESULT_MAX_SIZE), a result that would
    exceed it is cut short and reported with truncated: true. With no limit
    set, the full result is returned in one response, however large, so add
    a LIMIT clause, or pass copy_to_* to
    operational_insights_run_query_async_rest to export, when a query may
    return many rows.

    Each call is a request to the server, so space out repeat calls instead of
    looping tightly — and prefer telling the user the query is still running
    over waiting indefinitely for it.

    Safe to call more than once after it is ready: it does not consume the
    results. When you no longer need them, call
    operational_insights_discard_async_query_results_rest to free them on the
    server.

    Args:
        query_handle: The query_handle returned by
            operational_insights_run_query_async_rest.

    Returns:
        {"success": True, "ready": true, "rows": [...], "row_count": N,
        "truncated": bool, "metadata": {"warnings": [...], "metrics":
        {"elapsed_time_ms", "execution_time_ms", "result_count",
        "result_size", "processed_objects"}}}; or {"success": True,
        "ready": false} if not finished; or {"success": False,
        "error": "..."} on failure. When truncated is true the rows are
        incomplete and metadata may be empty, since the server reports it
        only once every row has been read.
    """
    try:
        token = QueryToken.decode(query_handle)
        client = get_operational_insights_rest_client(ctx)
        status = client.fetch_status(token)
        if not status.ready:
            return tool_success(
                query_handle=query_handle,
                ready=False,
                message=(
                    "Query is still running. Call this tool again later "
                    "to check for results."
                ),
            )

        bounded, metadata = client.fetch_results(
            status.result_path,  # type: ignore[arg-type]  # set when ready
            limit_bytes=max_query_result_size_for(ctx),
            service="operational-insights async rest",
        )

        destination = token.destination
        if destination is not None:
            logger.info(
                f"Export complete for async query (request_id={token.request_id}) -> "
                f"{destination['bucket']}/{destination['path']}"
            )
            return tool_success(
                query_handle=query_handle,
                ready=True,
                exported=True,
                destination=destination,
                row_count=0,
                metadata=metadata,
                message=(
                    f"Export complete. Results were written to "
                    f"{destination['bucket']}/{destination['path']} as "
                    f"{destination['format']}. No rows are returned for an "
                    f"export; read them from object storage."
                ),
            )

        logger.info(
            f"Fetched {bounded.row_count} row(s) for async query over REST "
            f"(request_id={token.request_id}, truncated={bounded.truncated})"
        )
        return tool_success(
            query_handle=query_handle,
            ready=True,
            rows=bounded.rows,
            row_count=bounded.row_count,
            metadata=metadata,
            **bounded.as_envelope_fields(),
        )
    except Exception as e:
        logger.error(
            f"Error fetching async query results over REST: {e}", exc_info=True
        )
        return _error(e, query_handle=query_handle)


def operational_insights_discard_async_query_results_rest(
    ctx: Context, query_handle: str
) -> dict[str, Any]:
    """Free the results of a finished async query on the server (REST-backed).

    Same behaviour as operational_insights_discard_async_query_results, for
    a query_handle from operational_insights_run_query_async_rest.

    Call this when done with a query's results — whether or not you fetched
    them, since fetching does not free them. This is the normal cleanup step
    after operational_insights_get_async_query_results_rest, and the rows
    cannot be retrieved afterwards.

    If the query is still running there is nothing to discard: this returns
    discarded: false and the query_handle stays usable, so use
    operational_insights_cancel_async_query_rest to stop it instead.

    Args:
        query_handle: The query_handle returned by
            operational_insights_run_query_async_rest.

    Returns:
        {"success": True, "query_handle": "...", "discarded": true}; or
        {"success": True, "discarded": false, "ready": false} if the query has
        not finished; or {"success": False, "error": "..."} on failure.
    """
    try:
        token = QueryToken.decode(query_handle)
        client = get_operational_insights_rest_client(ctx)
        status = client.fetch_status(token)
        if not status.ready:
            return tool_success(
                query_handle=query_handle,
                discarded=False,
                ready=False,
                message=(
                    "Results are not ready yet; nothing to discard. Cancel "
                    "the query with operational_insights_cancel_async_query_rest to stop it."
                ),
            )

        client.discard_results(status.result_path)  # type: ignore[arg-type]
        logger.info(
            f"Discarded results for async query (request_id={token.request_id})"
        )
        return tool_success(query_handle=query_handle, discarded=True)
    except Exception as e:
        logger.error(
            f"Error discarding async query results over REST: {e}", exc_info=True
        )
        return _error(e, query_handle=query_handle)


def operational_insights_cancel_async_query_rest(
    ctx: Context, query_handle: str
) -> dict[str, Any]:
    """Stop an async query that is still running (REST-backed).

    Same behaviour as operational_insights_cancel_async_query, for a
    query_handle from operational_insights_run_query_async_rest.

    Use this to abandon a query you no longer want to wait for. On success the
    query stops and the query_handle is no longer usable.

    A query that has already finished cannot be cancelled: this returns
    cancelled: false and the query_handle stays usable, so call
    operational_insights_discard_async_query_results_rest to free its results.

    Args:
        query_handle: The query_handle returned by
            operational_insights_run_query_async_rest.

    Returns:
        {"success": True, "query_handle": "...", "cancelled": true}, or
        {"success": False, "error": "..."} on failure.
    """
    try:
        token = QueryToken.decode(query_handle)
        client = get_operational_insights_rest_client(ctx)

        # Check first, for the same reason as the SDK tool: the server answers
        # a cancel for an already-completed query with a bare 404, which reads
        # as success, so a blind cancel would report "cancelled" while the
        # result buffers stay allocated.
        status = client.fetch_status(token)
        if status.ready:
            logger.info(
                f"Cancel skipped, query already complete (request_id={token.request_id})"
            )
            return tool_success(
                query_handle=query_handle,
                cancelled=False,
                message=(
                    "Query has already completed, so it cannot be cancelled. "
                    "Call operational_insights_discard_async_query_results_rest to free its results."
                ),
            )

        client.cancel(token.request_id)
        logger.info(f"Cancelled async query over REST (request_id={token.request_id})")
        return tool_success(query_handle=query_handle, cancelled=True)
    except Exception as e:
        logger.error(f"Error cancelling async query over REST: {e}", exc_info=True)
        return _error(e, query_handle=query_handle)
