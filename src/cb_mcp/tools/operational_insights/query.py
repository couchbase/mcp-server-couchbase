"""Query execution tools for Operational Insights.

``run_query_sync`` can carry DDL/DML, so — unlike the metadata tools in this
package — it follows the operational server's write-tool convention: catch
Exception, log, and return a ``{"success": False, "error": ...}`` envelope
instead of raising.

``explain_query`` follows the same envelope convention.

The Server Async Request API tools (``run_query_async``,
``get_async_query_results``, ``discard_async_query_results``,
``cancel_async_query``) expose the SDK's handle-based flow for long-running
queries: start -> check/fetch -> discard, or cancel.
``get_async_query_results`` does double duty as the readiness check, so there
is no separate status tool.

Design notes (kept out of the tool docstrings, which are sent to the model as
tool descriptions and are deliberately short):

* The live SDK handle objects hold an HTTP client and a thread pool, so they
  cannot be serialized to the client. They stay in a server-side
  ``HandleRegistry``, referenced by an opaque ``query_handle`` token. See
  ``utils/operational_insights/handle_registry.py`` for the design and its
  single-process caveat.
* Fetching results does NOT free them: the server serves the same buffers on
  repeated fetches. So a token stays valid after ``get_async_query_results``;
  only discard and cancel evict it. Callers that never discard leave buffers
  allocated until the server times them out.
* Tools are plain ``def`` (not ``async def``): the SDK's handle calls are
  blocking, so they run on FastMCP's thread pool rather than blocking the
  event loop inside a coroutine.
"""

import logging
import re
from typing import Any

from couchbase_operational_insights.options import QueryOptions
from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from ...utils.constants import SCOPE_WRITE
from ...utils.operational_insights.context import get_oi_cluster, get_oi_handle_registry
from ...utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.tools.query")


def _is_copy_to_statement(statement: str) -> bool:
    """True for a ``COPY ... TO`` statement.

    Covers both forms — export to external object storage and export to a
    KV collection — since both share the same leading keyword; there is no
    need to parse the rest of the grammar to tell them apart here.
    """
    normalized = statement.lstrip().upper()
    return re.match(r"^COPY\s", normalized) is not None


def run_query_sync(ctx: Context, statement: str) -> dict[str, Any]:
    """Run a SQL++ statement and buffer all result rows in memory.

    Can carry SELECT, DML, or DDL statements. Buffers the entire result set
    in client memory before returning.

    When the server is in read-only mode, or the caller's token lacks the
    write scope, the statement is executed with ``QueryOptions(readonly=True)``
    so the Operational Insights server itself rejects DML/DDL — there is no
    client-side SQL++ parser here (unlike the operational server's
    ``run_sql_plus_plus_query``), so enforcement is otherwise entirely
    server-side.

    One statement needs an extra, client-side check on top of that:
    ``COPY ... TO`` is classified as read-only by the server itself (it
    doesn't modify anything already stored), so ``readonly=True`` alone does
    not block it — but it writes its result to external object storage or a
    KV collection, which this server's read-only guarantee still treats as a
    write. Blocked here via a regex on the leading keyword rather than a
    full grammar parser, since read-only mode is the only thing that cares
    about the distinction.

    Returns {"success": True, "rows": [...], "row_count": N} on success, or
    {"success": False, "error": "..."} on failure.
    """
    cluster = get_oi_cluster(ctx)

    app_context = ctx.request_context.lifespan_context
    read_only_mode = app_context.read_only_mode

    # Mirrors run_sql_plus_plus_query's scope-gap closure: a token holding
    # only SCOPE_READ must not be able to mutate through this tool, since the
    # per-tool scope wrapper classifies it as a read tool (see
    # tools/operational_insights/__init__.py TOOL_SET). When no token is
    # present (stdio / OAuth disabled), lacks_write_scope is False, so the
    # historical read_only_mode-only behavior is preserved.
    token = get_access_token()
    lacks_write_scope = token is not None and SCOPE_WRITE not in (token.scopes or [])
    enforce_readonly = read_only_mode or lacks_write_scope

    if enforce_readonly and _is_copy_to_statement(statement):
        logger.debug("Blocking COPY ... TO statement under read-only mode")
        return tool_error(
            "COPY ... TO is blocked under read-only mode: the server itself "
            "classifies it as read-only, but it writes its result to "
            "external storage or a KV collection.",
            statement=statement,
        )

    try:
        logger.debug(
            f"Running SQL++ statement synchronously (readonly={enforce_readonly})"
        )
        result = (
            cluster.execute_query(statement, QueryOptions(readonly=True))
            if enforce_readonly
            else cluster.execute_query(statement)
        )
        rows = result.get_all_rows()
        logger.info(f"Query returned {len(rows)} row(s)")
        return tool_success(rows=rows, row_count=len(rows))
    except Exception as e:
        logger.error(f"Error running query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def explain_query(ctx: Context, statement: str) -> dict[str, Any]:
    """Generate the query plan for a SQL++ statement using EXPLAIN, without executing it.

    Whether the plan is cost-based or rule-based depends on whether the
    optimizer has collected samples for the target collection(s) — this tool
    just returns whatever plan the optimizer produces.

    Pass the statement without an EXPLAIN keyword; it is added automatically.

    Deliberately does not pass QueryOptions(readonly=True): EXPLAIN does not
    execute the statement it plans, and a readonly session may refuse to
    even plan a DDL/DML statement.

    Returns {"success": True, "plan": [...]} on success, or
    {"success": False, "error": "..."} on failure.
    """
    cluster = get_oi_cluster(ctx)
    try:
        logger.debug("Running EXPLAIN for SQL++ statement")
        result = cluster.execute_query(f"EXPLAIN {statement}")
        rows = result.get_all_rows()
        logger.info(f"EXPLAIN returned {len(rows)} row(s)")
        return tool_success(plan=rows)
    except Exception as e:
        logger.error(f"Error running EXPLAIN: {e}", exc_info=True)
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
    meta = _read(result.metadata, "metadata")
    if meta is None:
        return metadata

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

    When the server is in read-only mode, or the caller's token lacks the
    write scope, the query is started with ``QueryOptions(readonly=True)`` —
    same enforcement as run_query_sync, including the client-side
    ``COPY ... TO`` block, since the same read-only guarantee applies to
    async queries.

    Args:
        statement: The SQL++ statement to execute.

    Returns:
        {"success": True, "query_handle": "..."}, or
        {"success": False, "error": "..."} on failure.
    """
    cluster = get_oi_cluster(ctx)
    registry = get_oi_handle_registry(ctx)

    app_context = ctx.request_context.lifespan_context
    read_only_mode = app_context.read_only_mode

    token = get_access_token()
    lacks_write_scope = token is not None and SCOPE_WRITE not in (token.scopes or [])
    enforce_readonly = read_only_mode or lacks_write_scope

    if enforce_readonly and _is_copy_to_statement(statement):
        logger.debug("Blocking COPY ... TO statement under read-only mode")
        return tool_error(
            "COPY ... TO is blocked under read-only mode: the server itself "
            "classifies it as read-only, but it writes its result to "
            "external storage or a KV collection.",
            statement=statement,
        )

    try:
        logger.debug(f"Starting async query (readonly={enforce_readonly})")
        handle = (
            cluster.start_query(statement, QueryOptions(readonly=True))
            if enforce_readonly
            else cluster.start_query(statement)
        )
        query_handle = registry.register(handle, statement)
        logger.info(f"Started async query (token={query_handle})")
        return tool_success(
            query_handle=query_handle,
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
        "metadata": {"warnings": [...], "metrics":
        {"elapsed_time_ms", "execution_time_ms", "result_count",
        "result_size", "processed_objects"}}}; or {"success": True,
        "ready": false} if not finished; or {"success": False,
        "error": "..."} on failure.
    """
    registry = get_oi_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)
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

        result = status.result_handle().fetch_results()
        rows = result.get_all_rows()
        metadata = _extract_metadata(result, query_handle)

        # Deliberately NOT evicted: the server keeps the result buffers after
        # a fetch, so the token must stay valid for a re-fetch or an explicit
        # discard.
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
    registry = get_oi_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)
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

        status.result_handle().discard_results()
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
    registry = get_oi_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)

        # Check first: the server answers a cancel for an already-completed
        # query with a bare 404 (no message), and the SDK treats 404 as
        # success — so a blind cancel would report success, evict the token,
        # and strand the result buffers on the server with no handle left to
        # discard them.
        status = entry.handle.fetch_status()
        if status.results_ready():
            # Deliberately KEEP the entry, so the discard this message
            # recommends is still possible.
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
